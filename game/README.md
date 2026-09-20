# zombie-ar

Phone-native AR game, Chrome on Android. Stage 0 = capability probe only.

    www/index.html   the probe page (Stage 0)
    serve.py         same-origin server: HTTPS for LAN, --http for adb reverse
    certs/           mkcert cert + key for 10.6.7.27, localhost, 127.0.0.1
    certs/rootCA.crt the CA to install on the phone (already pushed to /sdcard/Download)
    logs/            server logs + probe_result.json posted back from the phone

## Two ways to reach it from the phone

**A. USB (works right now, no certificate needed).** `http://localhost` is a
secure context, so WebXR is happy without TLS.

    python3 serve.py --http --port 8080
    adb reverse tcp:8080 tcp:8080
    adb shell am start -a android.intent.action.VIEW -d http://localhost:8080/ com.android.chrome

**B. WiFi + HTTPS (needed once the phone is walked around untethered).**
Requires the phone on the same network as this Mac and the CA installed once:

    python3 serve.py --port 8443
    # on the phone: Settings > Security > More security settings >
    #   Encryption & credentials > Install a certificate > CA certificate
    #   > pick Download/mkcert-rootCA.crt  (expect a "network may be monitored" warning)
    # then open https://10.6.7.27:8443/

The phone's WiFi was enabled but DISCONNECTED when this was set up (loopback
only, no IP), so route A is the one that currently works. Re-run
`ipconfig getifaddr en0` and regenerate the cert if this Mac's IP changes:

    mkcert -cert-file certs/cert.pem -key-file certs/key.pem <new-ip> localhost 127.0.0.1 ::1

## One origin on the Pi (2026-09-19) — the demo path

The phone no longer needs the USB cable. Everything is served from the Pi
that is also the controller's access point:

    https://10.42.0.1:8443/stage3.html        game, /world, /events, /led, /telemetry
    https://10.42.1.1:8443/dashboard.html     dashboard from the Mac over the wired link
    http://10.42.1.1:8080/dashboard.html      same, for a browser without the mkcert CA

Deploy with `../jen-zombie/controller-bridge/deploy.sh` (rsyncs `www/` and
`worlds.py`, restarts the `jzbridge` service, verifies). The Mac + adb
fallback below is unchanged and still works; on that origin `/events` 404s,
the handheld simply stays absent and touch input carries on.

### Handheld (ESP32) mapping — `www/js/controller.js`, `www/js/led.js`

The stick has no locomotion job: the player walks.

    atk       swing the weapon (same call as a tap; latched, so a tap shorter than a frame counts)
    sens      microphone preset: 2048 -> NORMAL, 4095 -> HIGH (the switch position is
              adopted when the link comes up and on every flip; the HUD buttons follow)
    sw        skip to the next world now: swaps if one is ready, else INCOMING and it lands
              the instant it arrives; the countdown restarts from the skip
    x flick   cycle the targeted mob (white, larger floor ring); hold past 0.6, re-arm inside 0.3
    LEDs      hostility 0..1 -> green, then yellow, then red climb; POST /led only on change
              and at most 10 Hz, one request in flight, lamps off on EXIT

`connected` means frames are arriving (bridge flag AND an event inside 3 s).
Silence or a bridge-reported drop neutralises the stick and buttons; the
`PAD` pill top-right exists only while the handheld is live and nothing is
shown when it is not. No controller => identical behaviour to before.

    node test_controller.mjs   # 65 assertions: edges, latching, flicks, staleness,
                               # EventSource lifecycle, LED map and rate limiting
    node test_worldcycle.mjs   # + 11 assertions for the operator skip

Measured on the phone against the Pi (2026-09-19): swaps 1.4–2.7 ms (one
11.5 ms outlier), 88–92 fps while stationary, mic alive inside the session,
every LED post written to the board's socket. The XR frame loop pauses for a
few seconds every ~15 s while the phone lies still on a desk; the Mac-path logs
from earlier today show the same gaps, so it predates this work (ARCore with
nothing to track is the likely cause — check while walking).

## Stage 3 — countdown + world swap

    https://10.6.7.27:8443/stage3.html                        30 s cycle
    https://10.6.7.27:8443/stage3.html?cycle=8000             fast cycles
    https://10.6.7.27:8443/stage3.html?cycle=8000&delay=12000 late worlds

`GET /world` returns a canned world (`worlds.py`) with the full schema:
name, biome, count_scale, speed_scale, detect_scale, brute_bias, hp_bonus,
barks, plus an `accent` colour so a swap reads instantly on the floor rings.
`?delay=ms` and `?jitter=ms` fake model think-time; `--world-delay` sets a
server-wide default. Stage 4 (below) sends a camera frame instead, and the Pi proxies it to the real pipeline.

The clock is a metronome: every deadline advances by exactly one cycle, and a
late world never extends, freezes or resets it — the page shows INCOMING and
swaps mid-cycle the moment the world lands. The next world is fetched AND its
geometry built during the countdown, so the swap frame only repositions and
toggles visibility; disposal of the old set is spread one mob per frame.

    node test_worldcycle.mjs   # timing contract: on-time, late, failing, dead server
    node test_mobs.mjs         # arm's-length invariant, floor plane, no NaN

## Stage 4 — the camera frame becomes the world (2026-09-19)

At the start of every countdown the page grabs the live camera image, sends
it as a JPEG to `POST /world`, and uses the answer for the next swap. The Pi
forwards it to the Mac's pipeline service; Gemini captions it and Nemotron
writes the world. `www/js/framegrab.js` and `www/js/worldsource.js`,
`node test_worldsource.mjs` (34 assertions).

How the frame is taken, and the three things that bit:

1. **The camera image is 886x1920 (portrait), not the 1920x886 Stage 0
   noted.** The long edge is capped at 800 px (`?grabw=N`), giving 369x800
   JPEGs of ~6 kB.
2. **`XRWebGLBinding.getCameraImage()` needs a viewer pose, and against
   `local-floor` there is none until ARCore has tracked** — a phone lying
   still never gets one, so the first build waited 6 s every cycle and fell
   back. The lookup now uses the `viewer` reference space, which always has a
   pose, and frames arrive from the first seconds of the session.
3. **The texture is sampled, never attached.** It is drawn through a
   two-line shader into a small offscreen target (the downscale is on the
   GPU) and only that target is read back: `readPixels` of 1.2 MB, not
   6.8 MB. The shader and target are compiled/allocated once at session
   start (`warm()`), so the compile never lands in a countdown frame.

Measured on the phone: capture 10–17 ms warm (34 ms when the compile was
still inside it), once per countdown; fps 89–92 throughout; swaps unchanged
at 0.6–1.5 ms; service round trip 2.3–7.5 s. `?noframe=1` disables capture
for an A/B.

Failure behaviour, all silent for the player: camera not ready for 6 s,
grab failure, empty JPEG, POST failure or non-2xx → the canned `GET`;
`GET` failing too → WorldCycle keeps the current world and retries. The
dashboard's **World → source** row says what you are looking at
(`UPSTREAM · real world`, `UPSTREAM · pipeline fallback`, `CANNED-NO-FRAME`,
`CANNED-FALLBACK` with the reason) and the **Camera frame → world** card
shows the capture cost, the last frame the phone actually sent (the Pi keeps
it at `/last-frame.jpg`) and the caption Gemini wrote for it.

Not yet checked by a human: whether the frame is the right way up
(`?flip=0` if the dashboard's frame is inverted) — every frame captured so
far was a featureless surface because the phone was lying on a desk.

## Stage 5 — the game (2026-09-19)

### Landscape, and why it is enforced

The AR session keeps whatever orientation the page had when it started:
Chrome runs the immersive activity `SCREEN_ORIENTATION_NOSENSOR` and
`screen.orientation.lock()` throws `NotSupportedError` inside a session
(measured on the device). So the launch screen hides ENTER AR and says TURN
THE PHONE SIDEWAYS until the page itself is landscape; auto-rotate has to be
on. `?anyorient=1` bypasses the gate. In landscape the viewport is 832x363
CSS px and the camera image is 1920x886; in portrait it is 886x1920.

### Thrown slashes

A swing no longer touches anything by itself. It throws a crescent
(`www/js/slash.js`) that flies out and damages what it reaches:

    walker / runner  1 hit + the world's hp_bonus
    brute            2 hits + hp_bonus
    one slash damages ONE mob and is consumed -- a kill always traces to a throw

It is thrown **at** something: the mob cycled with the stick flick if there is
one, otherwise the nearest mob inside a 42 degree forward cone within 8 m,
otherwise straight ahead. A hit flashes the sprite red; a death pops it upward
and shrinks it over 420 ms, then it is reaped into the same one-per-frame
disposal queue a world swap uses. The crescent is drawn into a canvas at
runtime, so there is no new asset and no licence question.

### Hearts, hits and mercy

Three hearts, top right (`www/js/player.js`). A hostile mob that has closed to
2 m swings every 1.6 s; a hit costs a heart, tints the whole view red for
650 ms, and grants **5 seconds of immunity** during which a whole crowd
swinging costs nothing. Walking into a room already surrounded gives 3 s of
grace first. At zero hearts the field clears (everything walks out) and the
player is back on full hearts 3.5 s later, because a demo that dead-ends in
front of a judge is worse than one that is easy.

### The room follows the room

Sustained quiet (hostility <= 0.18 for 2 s) and the hostile crowd walks out
while calm ones wander in; sustained noise (>= 0.45 for 2 s) and the calm ones
leave again (`www/js/population.js`). The two thresholds are far apart and the
change has to persist, so a cough cannot turn the room over. Calm mobs keep
3.2 m away, never swing, and use a different sprite table (`CALM_SPRITES`:
people and animals rather than armour and teeth). **`?mood=hostile` or
`?mood=calm` pins the room and stops it listening** -- for showing the hostile
half in a quiet corridor, or the calm half in a loud hall.

### The clock is a snapshot cooldown

The countdown no longer swaps worlds by itself. It is a cooldown on taking a
photo of the room: it counts down, stops at zero showing SCAN READY, and waits
as long as it has to. **Pressing the handheld's stick button takes the
snapshot**, which captures the camera frame at that instant, POSTs it, and
swaps when the world lands. The cooldown restarts from the press, not from the
world landing. Pressing early is refused and changes nothing. The opening
world is taken automatically, or the player would walk into an empty room.
`?auto=1` restores the old self-running metronome.

### Sensitivity: five presets, one button

`min low normal high max`, 6 dB apart (`PRESETS` in `mic.js`). The ESP32's
input is a momentary button, so **each press steps one preset and stays
there** -- holding it is not required and releasing it undoes nothing. The
on-screen SENS chip does the same thing per tap and shows five dots. Presses
within 150 ms are contact bounce and are ignored.

### Points

Next to the hearts, top right (`Score` in `player.js`):

    walker 10 · runner 15 · brute 25 · snapshot 50 · taking a hit -20

A snapshot pays more than any single kill because scanning the room is the
mechanic the whole game is built on. The score never goes below zero -- a
number that only falls is not worth looking at -- and the best score of the
run is kept even after it drops.

### What moved off the headset

TRIM and a preset picker and CALIBRATE now live on the **dashboard**, and EXIT
is gone (the system back gesture ends the session). The phone keeps only the
small SENS chip and CAL. Dashboard controls are queued on the server and ride
back to the headset on the reply to its 5 Hz telemetry POST, so there is no
second connection to the phone; they land within about 200 ms.

    node test_slash.mjs     # 59: aiming, flight, damage, death, disposal
    node test_player.mjs    # 68: hearts, the 5 s window, attacks, the room
                            #     mood, and the scoring rules
    node test_worldcycle.mjs# 48: the metronome AND the snapshot cooldown

`window.__jz` inside a session exposes the mobs, the slashes, the player, the
room and `snapshot()` for driving it from chrome://inspect; nothing in the
game reads it.

## Stage 6 — the peaceful half, voices, and the stalls (2026-09-19)

### Green auras, and peaceful mobs everywhere

Anything calm wears a drift of green pixel motes (`www/js/aura.js`), because
the sprite swap alone is too subtle at 30 px across a room. One `THREE.Points`
for the whole room, one draw call, a pool allocated once at startup and
written in place every frame -- no allocation in the render loop. A mote
belongs to a mob until it expires and is recycled; when its mob leaves, dies
or turns hostile, the aura goes out that frame.

All ten biomes have a full calm row (`CALM_SPRITES`), nothing armed or fanged
appears in one even in the brute slot, and no calm sprite matches the hostile
sprite for the same biome and kind. Calm spawns are never brutes whatever the
world asks for.

### The mobs talk

Nemotron now writes **four** dialogue lists per world: `walker`, `runner`,
`brute` and `calm` -- the last spoken by the peaceful locals who live in that
place, wary or weary rather than threatening. Measured live from a cave world:

    walker  "Where is the light?"        calm  "Watch your step in here"
    runner  "Something breathes near me"       "Air feels thick and old"
    brute   "I crush what I can't see"         "Nothing grows in this black"

A line appears in a bubble over the speaker's head, positioned by projecting
that mob into screen space each frame, and hidden when they are behind you or
off the edge. ElevenLabs says it (`GET /say` on the Pi, passed through to the
Mac service), one voice at a time, on a 5-9 s gap.

**The microphone is deafened while the game talks.** This is the part that
matters: hostility drives everything, the speaker is 15 cm from the mic and
echo cancellation is deliberately off, so a zombie shouting would raise
hostility, turn the room hostile and make more zombies shout. `mic.suppress()`
pins the level below the floor from 120 ms before playback to 400 ms after,
for exactly the audio's real length, and calibration refuses to run while it
is held. The dashboard shows `DEAFENED` rather than pretending the room went
quiet.

**No audio is still dialogue.** Missing key, spent budget or an unreachable
Mac leaves the line on screen with the reason attached, and the failure on the
dashboard.

    node test_voices.mjs    # 36: who says what, one at a time, and the mic
                            #     being deaf for exactly the right window
    node test_aura.mjs      # 25: the pool, the recycling, following the mob

### The voices, cast from the real library

The roles are matched against the account's own voices by first name, because
ElevenLabs names carry a description ("Callum - Husky Trickster"). On this
account:

    walker   Callum - Husky Trickster
    runner   Liam - Energetic, Social Media Creator
    brute    Harry - Fierce Warrior
    calm     Sarah - Mature, Reassuring, Confident

A library without those falls back down a preference list, then to the public
default ids. `GET /voices` shows the casting and `?force=1` re-reads the
library immediately after a key's permissions change.

Measured: 0.2-0.6 s for a new line, 31 ms once cached.

### Cost control

ElevenLabs Flash bills half a credit per character. Every line is cached on
disk by (voice, model, text), so a repeat is free, and the service refuses
past `JZ_TTS_BUDGET_CHARS` (default 12,000 characters, about 6,000 of the
account's 10,000 credits). `GET /voices` shows the mapping, the spend and the
estimate.

### The freezing

Measured before touching anything, from `logs/stage3.jsonl`: worst frames of
**8.3 s**, a p90 worst frame of 2.2 s, and 159 of 874 telemetry samples under
30 fps. So this was real and frequent, not an impression.

What was done about it:

  * **MSAA off and the XR framebuffer at 0.8** (`?aa=1`, `?fb=1.0` to put them
    back). That is 36% fewer pixels per frame plus no multisample resolve, on
    art made of hard-edged pixel sprites that gains nothing from either.
  * **A 60 Hz target was attempted and is not available**: this device reports
    an empty `session.supportedFrameRates`, so `updateTargetFrameRate` has
    nothing to ask for. The code requests it where it exists.
  * **The telemetry object is only built when it will be sent** -- it was
    being assembled 90 times a second to send 5.
  * **The microphone's rolling peak is a ring buffer**, not an array of
    objects with a `shift()` per frame.
  * **Tracking loss is now said out loud.** Losing tracking looks exactly like
    a crash: the picture stops moving. After 450 ms the screen says FINDING
    THE ROOM and the dashboard counts the losses and their durations.
  * **The lamps can no longer stick on.** The board holds the last `LED` line
    for ever (PROTOCOL.md section 4), so a game that went away -- a reload, a
    locked phone, a closed tab -- left them lit at whatever the meter last
    read, which looks like a stuck meter at any sensitivity. Three fixes: the
    page sends zeros on every way out it can see (`pagehide`, `freeze`,
    hidden), the meter now sends a keepalive every 2 s so silence on that
    channel means the game is gone rather than the meter being steady, and the
    bridge darkens the lamps once when nothing has driven them for 5 s
    (`JZ_LED_IDLE_S`). A board that connects is also cleared to a known dark
    state.
  * **The lamps stopped responding entirely at one point, and it was not the
    meter.** Every LED post carried `keepalive: true`. A browser allows only a
    handful of keepalive requests in flight, so at 10 Hz most of them were
    dropped before they left the phone: 3715 failures against 2390 successes,
    with the meter reading perfectly the whole time. Keepalive now marks only
    the single post on the way out, which is what it is for. Measured after:
    0 failures, the board updated every 100 ms.
  * **Stalls are recorded with their context.** Any frame over 120 ms is kept
    with what was happening -- tracking state, whether the camera capture ran,
    whether a world was in flight, how many mobs -- and the worst six reach
    the dashboard. That is what will say whether a remaining stall is ARCore,
    the capture or the game.

A short stationary session after the change held 87-94 fps with a worst frame
of 56 ms and **no stalls over 120 ms**. That is not proof against the fast
camera movement that provokes it -- check the dashboard's worst-frames list
after a session that stutters, and it will say which of the three it was.

## Microphone metering (Stage 3 page)

Metering and calibration ONLY — hostility is displayed, not yet wired to enemy
behaviour. Added to the existing metrics block: `mic`, `level`, `peak 2s`,
`hostility`, `sensitivity`, `window`.

    dBFS <= floor    -> hostility 0.00
    dBFS >= ceiling  -> hostility 1.00
    between          -> linear

`CALIBRATE` samples 3 s of ambient (countdown on screen), puts the floor 3 dB
above the p95 of what it heard, and the ceiling a nominal 27.7 dB above that —
capped at -8 dBFS, because a noisy room otherwise yields a ceiling near 0 dBFS
that no voice can reach. Calibration, preset and trim persist in localStorage
and are re-clamped on load.

Sensitivity is TWO discrete presets, because the ESP32 handheld's input is a
two-position digital toggle reporting 2048/4095 — not a potentiometer:

    normal -> ceilingShift   0 dB      (ESP32 2048)
    high   -> ceilingShift -12 dB      (ESP32 4095)
    mic.applyEsp32Toggle(raw)          maps the raw value onto a preset

TRIM (+/-1.5 dB) is screen-only finer adjustment; nothing assumes a continuous
hardware knob. Higher sensitivity LOWERS the ceiling — it shifts the window, it
does not amplify the signal.

Mic failures are shown as `mic: DENIED` / `mic: LOST` with the level blanked to
`—`, so a broken microphone never looks like a quiet room.

    node test_mic.mjs          # 51 assertions: dBFS, window, presets,
                               # calibration (quiet + noisy), failure states,
                               # rolling peak, persistence

**Confirmed on device: the microphone keeps working during an immersive-ar
session.** ARCore holds the camera but not the audio input — track `live`,
AudioContext `running`, not muted, measured 1.5 s after session entry.

## Sprites (DOOM-style billboards)

Flat 2D sprites standing in the 3D world. Four details make or break it, all
enforced in `www/js/sprites.js` and covered by `node test_sprites.mjs`:

1. **Billboard on Y only** — a `PlaneGeometry` rotated about Y, never a
   `THREE.Sprite`. Camera height cannot affect orientation, so the mob stays
   upright when you crouch or hold the phone low.
2. **NearestFilter on mag AND min, `generateMipmaps = false`** — crisp up close
   and at distance.
3. **`alphaTest 0.5`, `transparent = false`** — hard cutout edges, no
   depth-sort artifacts between overlapping mobs.
4. **Bottom-anchored** — geometry translated so the pivot is at the feet;
   humanoids are 1.7 m, width follows each sprite's trimmed aspect ratio.

Assets: **Kenney Tiny Dungeon 1.0, CC0**, downloaded fresh from kenney.nl into
`vendor-src/` and cropped to content bounds into `www/assets/kenney/`
(`LICENSE.txt` and `sprites.json` alongside). Nothing is copied from any other
project. Biome -> [walker, runner, brute] is a plain table in `sprites.js`;
desert gets a scorpion, snow gets a ghost, street and farm get the skull-faced
zombie. Textures preload once before AR so a world swap never pays for image
decoding.

## Weapons

One weapon per biome, held in the right hand as a DOOM-style viewmodel
(`www/js/weapon.js`). Same cutout treatment as the mobs: NearestFilter,
`alphaTest 0.5`, no blending — plus `depthTest:false` and a high renderOrder so
it always draws over the world.

    lab staff_arc · rocky hammer · cave torch · desert pickaxe · forest axe
    meadow sword · snow staff_ice · water cleaver · street bat · farm battleaxe

Tap anywhere that is not a control to swing (320 ms arc, no queueing). Note:
**WebXR `select` does not fire while tracking is lost**, so there is a DOM
`pointerdown` fallback — measured on device, `selectEvents=0` but
`domPointers=3` produced 3 swings. The viewmodel is positioned as
`cameraMatrixWorld * localOffset` rather than being reparented to the XR
camera, which three rebuilds.

Mobs now stop at **2.0 m**, not 0.70 m — arm's length put a 1.7 m sprite in
your face. Retune live with `?stop=<metres>`; the spawn ring moved out to 3.6 m
to match.

## Dashboard

Open it on a laptop, from whichever origin is reachable:

    https://10.42.1.1:8443/dashboard.html    the Pi, over the wired link
    http://10.42.1.1:8080/dashboard.html     same, no certificate needed
    https://10.42.0.1:8443/dashboard.html    the Pi, from the phone's AP

It shows everything the game knows -- hearts, score, kills, the room's mood
and which way it is turning, the snapshot cooldown, the handheld, the lamps,
the frame the phone last sent -- plus **the model traffic as a conversation**.

### The game talking to the models

The bottom panel is a message thread. The game's prompts are on the right,
whichever model it is talking to answers on the left, oldest at the top, so a
prompt and its answer read together. **The photo is in the thread**: each
vision message carries the frame that exchange was actually built from, not
whatever is newest, matched by time against a short history the pipeline
service keeps (`/frames`, `/frame?id=`, the last twelve). Tap a photo to
enlarge it; long system prompts are clipped with a "show the whole thing".

It reads `GET /trace`, which **all three servers now expose** -- the Pi and
both Mac servers -- so the panel works whichever origin the dashboard was
opened from. It was previously only on the Pi, which is why it looked empty
from the Mac. Underneath, it is `pipeline.py`'s own `logs/model_chat.jsonl`,
so nothing is recorded twice and the prompts are the exact bytes that were
sent (the logger already redacts the image to a size note). Refreshes every
3 s, with a pause button. A model failure appears in the thread as a failed
reply -- an NVIDIA "Service temporarily overloaded" 503 shows up exactly
where you would want to see it.

The headset shows **no diagnostics at all** — only the countdown, the world
banner, the mic controls and EXIT. Everything else streams to the dashboard:

    https://10.6.7.27:8443/dashboard.html      (or http://localhost:8080/... over adb)

The AR page POSTs telemetry to `/telemetry` at 5 Hz; the dashboard polls it at
5 Hz and shows audio (with a level meter marked with the floor and ceiling),
world/cycle, session and mob state, plus a LIVE / stale / SILENT indicator so a
dead headset is obvious. Telemetry is **file-backed** (`logs/telemetry.json`),
not process memory, because the HTTPS and HTTP servers are separate processes
and the dashboard must work from either.

Telemetry is pushed *before* the render loop's early returns, so the dashboard
keeps reporting when tracking is lost — which is exactly when you are looking
at it.

## Reading results

The page POSTs its findings to `/probe-result`, which the server writes to
`logs/probe_result.json` (and appends to `logs/probe_result.jsonl`). Static
checks post automatically on load; the session half posts after you tap
**RUN AR SESSION TESTS**. Console logs are prefixed `[PROBE]`, server logs
`[SRV]`, so `chrome://inspect` filtering works too.

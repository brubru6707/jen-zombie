# Session report: integration, Stage 4, gameplay, and what broke along the way

Written 2026-09-20. Handover for whoever picks this up next. Covers everything
done in one long session, what was verified on the device versus inferred, and
the state the hardware and the repo are in now.

The power bank problem has its own report: `REPORT-power-bank.md`. It is not
solved.

---

## 1. Where things started and where they are now

The brief was to join two halves that had never spoken: an AR game served from
the Mac over a USB cable, and an ESP32 handheld on a Raspberry Pi access point.

**Now:** the phone loads everything from `https://10.42.0.1:8443` on the Pi with
no cable and no certificate warning, the handheld drives the game, the camera
frame becomes the world through Gemini and Nemotron, the mobs speak with
ElevenLabs voices, and the dashboard shows the whole model conversation.

The test suite went from **139 assertions to 491**, all passing. None of the
original 139 were weakened; two were updated where a product decision changed
the contract (the preset count going from two to five, and `ready` being renamed
`prepared` when a second meaning was added).

---

## 2. Housekeeping first

Both projects were moved out of `~/Documents` because macOS TCC kept revoking
access mid-run, once making the server 404 its own files. They went to `~/dev`,
and the two Mac servers were restarted from the new location with their adb
tunnels intact.

Later in the session someone reorganised again: the game now lives at
`~/dev/jen-zombie/game`, inside the pipeline repo. The running servers followed
the directory by inode, `deploy.sh` had already been updated, and all 491
assertions pass from the new path. Worth knowing because tooling that still
says `~/dev/zombie-ar` is stale.

---

## 3. One origin on the Pi

### The certificate

Regenerated with mkcert so the SANs cover the Pi's AP address, and installed on
the Pi in place of its self-signed certificate:

```
mkcert -cert-file cert.pem -key-file key.pem 10.42.0.1 10.42.1.1 jenzombies localhost 127.0.0.1
CERTS=certs ./deploy.sh
```

**Verified on the phone** through Chrome DevTools: document `securityState =
secure`, issuer the mkcert CA, TLS 1.3, no interstitial. That matters beyond
tidiness — a click-through certificate exception makes WebXR and `getUserMedia`
behave unpredictably, and this demo needs both.

### The server

`controller-bridge/gameweb.py` is new. It is imported by `bridge.py` and serves
the game plus every route from one origin on ports 80, 8080 and 8443:

```
/                     the game (www/, an rsync mirror of game/www)
/world                canned worlds, or a proxy to the Mac pipeline
/events               controller state as SSE          /ws  the same over WebSocket
POST /led             drive the handheld's three lamps
/status               bridge health, counters
/telemetry            headset to dashboard, and the command channel back
/trace /voices /say /frames /frame     passthrough to the Mac pipeline service
generate_204 and friends               captive-portal answers
```

TLS handshakes happen on each connection's own thread rather than in `accept()`,
so a client that connects and stalls cannot freeze the listener for everyone.

### Changes to bridge.py, all additive

- Ports overridable by environment, so a bench instance can run beside the live
  service without touching it.
- A board that goes silent for 2 s is reported as disconnected with **neutral**
  input, not last-known, per PROTOCOL.md section 5. A stick that was pushed when
  the link died must not keep the player running.
- `send_led` writes straight to the socket instead of waiting for the reader
  loop, removing up to 500 ms of lag on a live meter.
- The legacy single-page server is still there as a fallback if `www/` or
  `gameweb` is missing, so the service always comes up with the controller
  endpoints intact.

`deploy.sh` rsyncs, restarts, and then **verifies**: it fails loudly if the
service does not come back or `/status` does not answer. It ran clean on every
deploy this session.

### The Mac fallback was never broken

`serve.py` and the adb tunnels still work, and the trace/voices/frames routes
were added there too so the dashboard behaves identically from either origin.

---

## 4. The handheld in the game

`game/www/js/controller.js` (SSE client) and `led.js` (the lamp meter). The
mapping, after two rounds of correction from the owner:

| input | does |
|---|---|
| `atk` | throws a slash |
| `sens` | **one press steps one sensitivity preset and stays there** |
| `sw` | takes the room snapshot, if the cooldown has run out |
| stick flick | cycles which mob is targeted |
| lamps | hostility, green then yellow then red |

`connected` means frames are arriving: the bridge's flag *and* an event inside
3 s. Buttons are latched between polls so a tap shorter than a frame is never
lost. With no controller the game is unchanged and nothing is drawn for it.

**The sensitivity button is momentary, not a switch.** The first build followed
its level, which meant holding it down to stay sensitive. It now acts on the
press edge, with a 150 ms debounce so contact bounce cannot step the preset
several times.

---

## 5. Stage 4: the camera frame becomes the world

`framegrab.js` and `worldsource.js`. At the start of each cooldown the page
grabs the camera image, encodes a JPEG and POSTs it; the Pi forwards it to the
pipeline service on the Mac.

Three things cost real time and are worth recording:

1. **The camera image is 886x1920 portrait**, not the 1920x886 that Stage 0
   recorded. The long edge is now capped at 800 px, giving ~6 kB JPEGs.
2. **`getCameraImage()` needs a viewer pose, and against `local-floor` there
   isn't one until ARCore has tracked.** A phone lying still never gets one, so
   the first build waited 6 s every cycle and fell back to canned worlds. The
   lookup now uses the `viewer` reference space, which always has a pose.
3. **The texture is sampled, never attached.** It is drawn through a two-line
   shader into a small offscreen target so the downscale happens on the GPU, and
   only that target is read back: 1.2 MB instead of 6.8 MB. The shader and
   target are built at session start so the compile never lands in a countdown
   frame.

Measured on the phone: capture 10–17 ms warm, 34 ms when the compile was still
inside it. Round trip to a real world 2.3–7.5 s.

### Timeouts, which were wrong in two places

The pipeline budgets 25 s per world and retries NVIDIA 503 storms inside that;
worst measured success was 24.2 s. The Pi's proxy timeout was 20 s and **the
game's own fetch abort was 12 s**. Both would have discarded slow-but-successful
calls and made the pipeline look broken. They are now 32 s and 40 s.

---

## 6. Gameplay, built to the owner's requests

- **Thrown slashes.** A swing launches a crescent that flies out and damages
  what it reaches. It is thrown *at* something: the cycled target, else the
  nearest mob in a 42° forward cone. One slash damages one mob and is consumed.
- **Hearts, damage, mercy.** Three hearts top right, 5 s immunity after a hit,
  a red wash over the view, 3 s of grace on entering. At zero the field clears
  and the player revives after 3.5 s, because a demo that dead-ends in front of
  a judge is worse than an easy one.
- **Points.** walker 10, runner 15, brute 25, snapshot 50, taking a hit −20,
  never below zero.
- **The room follows the noise.** Quiet for 2 s and the hostile crowd walks out
  while peaceful ones arrive; noise for 2 s and the reverse. Hysteresis and a
  dwell so a cough cannot flip it. Calm mobs keep their distance, never swing,
  wear green pixel auras, and have a full sprite row in every biome.
- **The clock is a snapshot cooldown**, not a metronome. It stops at zero and
  waits for the stick button. The opening world is automatic so the player never
  walks into an empty room.
- **Five sensitivity presets**, min through max, 6 dB apart.
- **Dialogue.** Nemotron now writes a fourth list, `calm`, for the peaceful
  locals. ElevenLabs speaks it, cast from the account's own library. A bubble
  follows the speaker's head.
- **The microphone is deafened while the game talks.** This is the one that
  matters: hostility drives everything, the speaker is centimetres from the mic
  with echo cancellation off, so a zombie shouting would raise hostility, turn
  the room hostile and make more zombies shout.

### Moved off the headset

TRIM, a preset picker and CALIBRATE now live on the dashboard and reach the
phone on the reply to its 5 Hz telemetry POST — no second connection. EXIT is
gone. The phone keeps a small SENS chip, CAL, and a scan button.

---

## 7. The dashboard

Open it on a laptop, never the phone (it is refused to handheld user agents):

```
http://10.42.1.1:8080/dashboard.html     no certificate needed
https://10.42.1.1:8443/dashboard.html    over the wired link
https://10.42.0.1:8443/dashboard.html    from the AP
```

It shows hearts, score, kills, the room's mood, the cooldown, the handheld, the
lamps, render settings, stalls with their context, tracking losses — and **the
model traffic as a message thread**: the game's prompts on the right, Gemini
and Nemotron answering on the left, oldest first, with the photo each exchange
was actually built from attached to it. That reads `pipeline.py`'s own
`logs/model_chat.jsonl` through a `/trace` endpoint, so nothing is recorded
twice and the prompts shown are the exact bytes that were sent.

---

## 8. Bugs found and fixed

| Bug | Cause | Evidence |
|---|---|---|
| Lamps dead while the meter worked | every LED post used `keepalive: true`; the browser allows only a handful in flight | 3715 failures against 2390 successes; 0 failures after |
| Lamps stuck on | the board holds the last `LED` line forever and nothing cleared it when the game went away | page now sends zeros on exit, meter sends a 2 s keepalive, bridge darkens after 5 s idle, board cleared on connect |
| "No good mobs ever" | a leftover `?mood=hostile` in the URL from my own testing pinned the room and stopped it listening | a pinned room now says so on screen |
| Room empty between crowds | arrivals spawned at 6.1 m and took ~9 s to appear while departures all left at once | arrivals at 3.6 m, departures staggered |
| No dialogue audible | Do Not Disturb was on, which mutes the media stream and silently ignores volume changes | 26 lines, 26 spoken, 0 failures the whole time |
| Game unplayable without the handheld | the snapshot could only be triggered by the stick button, and it is the only thing that repopulates the room | scan is now a button on screen |
| Sprite collisions, a flaky test, a TDZ error, a stretched button | ordinary mistakes | all caught and fixed in-session |

---

## 9. Performance and the stalls

Measured **before** changing anything, from `logs/stage3.jsonl`: worst frames of
**8.3 s**, a p90 worst frame of 2.2 s, and 159 of 874 telemetry samples under
30 fps. This predates the session's work — the same gaps are in the pre-change
logs.

Done about it: MSAA off and the XR framebuffer at 0.8 (36% fewer pixels, on
hard-edged pixel art that gains nothing from either); telemetry built only when
it will be sent rather than 90 times a second to send 5; the mic's rolling peak
moved to a ring buffer; a screen wake lock so a session cannot die with the
screen.

A 60 Hz target was attempted and **is not available**: this device reports an
empty `session.supportedFrameRates`.

Two things now make the remaining stalls diagnosable rather than mysterious.
Tracking loss says **FINDING THE ROOM** on screen after 450 ms, because losing
tracking looks exactly like a crash. And every frame over 120 ms is recorded
with what was happening — tracking state, whether the camera capture ran,
whether a world was in flight, how many mobs — with the worst six on the
dashboard.

A short stationary session after the changes held 87–94 fps with a worst frame
of 56 ms and no stalls. **That is not proof against the fast camera movement
that provokes it.** Next time it stutters, read the worst-frames list.

---

## 10. Infrastructure incidents, none of them the code

These ate a large part of the session and will recur at the venue.

1. **The phone will not rejoin JZCTRL by itself.** Android deprioritises a
   network it has scored as having no internet and sits next to a strong AP
   without associating. Happened three times. Fix: tap it in the WiFi list, or
   `adb shell cmd wifi connect-network JZCTRL wpa2 '<psk>'`. On the phone,
   long-press JZCTRL and enable auto-reconnect / keep-when-no-internet.
2. **Chrome held a dead network handle.** ICMP and shell TCP reached the Pi
   perfectly while every Chrome request timed out and nothing arrived at the
   server. Force-stopping Chrome fixed it instantly. This looks exactly like a
   server outage; it is not.
3. **The Mac-to-Pi USB Ethernet adapter was unplugged**, removing `en8`. The
   game keeps running on canned worlds, but SSH, deploys and real worlds all
   stop. Check `ifconfig en8` before blaming anything else.
4. **The Pi itself recovered from a power cycle unaided** — service enabled and
   started at boot, AP up, ESP32 redialled, about 30 seconds end to end.

---

## 11. State you are inheriting

**Repo** (`~/dev/jen-zombie`, commit `6b64211` merged the game in):

```
 M controller-bridge/bridge.py      lamp watchdog, radio keepalive
 M controller-bridge/gameweb.py     /trace /say /voices /frames passthrough, keepalive counter
 M game/www/stage3.html             scan button, overlapped changeover, room label
?? REPORT-power-bank.md
```

Uncommitted. `server.py`, `game/` and the bridge are otherwise in version
control.

**Secrets**: `ELEVENLABS_API_KEY` was added to `.env` (gitignored, mode 600).
The key was pasted into the session transcript, so rotating it after the demo is
worth doing. Its permissions had to be granted in the ElevenLabs dashboard —
the key initially had no scopes at all.

**Pipeline service** (`server.py`, another agent's file): I added read-only
`/trace`, `/say`, `/voices`, `/frames`, `/frame` and restarted it. `/world` is
untouched. Spend so far is about 115 characters, roughly 58 of 10,000 credits,
with a 12,000-character ceiling in the code.

**The board's NVS** now holds `keepalive=1 period=500 burst=499`, so core 0
spins almost continuously and it runs warmer. That was for the power bank and
did not work; see the other report. Consider restoring `KEEP 1 6000 250`.

**The phone**, settings I changed and you may want back:

- Do Not Disturb turned **off** (it was muting all game audio)
- Media volume set to a quarter
- Auto-rotate toggled during testing, left **on**
- A USB "allow access to phone data" dialog **denied** — grants nothing, but the
  USB mode may flip to charge-only and drop adb

---

## 12. What I verified myself, and what I did not

**Verified on the device**, through adb and Chrome DevTools: the certificate
with no warning; AR sessions starting and running at 87–94 fps; worlds arriving
`source: upstream, fallback: false` with real captions; the camera capture cost;
slashes hitting, damaging and killing; hearts falling to zero with immunity
between hits and the red flash peaking at 0.94–1.0; the room turning over both
ways; auras on calm mobs; a brute speaking "Stand still." aloud with the mic
reading **deafened** for exactly that line; the snapshot cooldown holding at
READY and a press producing a new upstream world; LED delivery going from 60%
failures to none; the dashboard thread rendering prompts, replies and photos.

**Not verified.** Nobody has held the phone and walked around with it. Tracking
was false for almost every measurement because the phone sat on a desk, which
means the mob-in-the-room feel, the aim, the bubble placement while moving, and
above all the fast-motion stalls are all unconfirmed. The LEDs were proven to
reach the board's socket, not to physically light. And the power bank is
unsolved.

If you do one thing before the demo: pick the phone up, walk around a room with
it for five minutes, and read the dashboard afterwards.

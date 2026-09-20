# jen-zombie — demo-day session report

For the manager agent. Written 2026-09-20, the morning of judging. Covers the
work done in this session only; the earlier integration work is in
`REPORT-session.md` and the unsolved hardware problem is in
`REPORT-power-bank.md`.

Everything below is measured unless it says otherwise. The last section is an
explicit split of what was verified on the device against what was not, because
the phone came off USB partway through and three deploys have not been seen on
it.

---

## 1. Summary

Six pieces of work, in the order they were asked for:

| # | Ask | Outcome |
|---|---|---|
| 1 | "ENTER AR is greyed out and I can't click it" | Root-caused; fixed; made impossible to hit silently again |
| 2 | "fix it for me, you have my Android" | Driven over adb: rejoined the AP, forced landscape, entered AR, found volume at 0 |
| 3 | Dialogue must never repeat; voices less robotic | Lines retired permanently; model, bitrate and prosody all changed |
| 4 | Mute button; popups invisible; lower minimum sensitivity; an OFF | All four; the popup problem was a real bug |
| 5 | A 2D mode so the joystick can drive | New mode, new module, 31 assertions, run end-to-end |
| 6 | On-chain score settlement on Solana | Live on devnet, one real transaction verified on chain |

Test suite went from **491 assertions to 607** across 13 files. Nothing was
deleted; three existing mic assertions were rewritten because the six-preset
change made the old five-preset contract wrong.

---

## 2. The greyed-out button

**Reported:** ENTER AR grey, unclickable.

**Found:** the button starts enabled. The only line in the codebase that
disables it is inside `run()`, and `button:disabled` is painted grey. So a grey
button meant it had already been tapped and `run()` never got past:

```js
session = await startAR({ overlayRoot: hud });
```

`requestSession()` stays pending for as long as Chrome's "create a 3D map of
your surroundings" prompt is unanswered — and if that prompt is swiped away, or
the origin holds a Block, it never settles at all. Nothing after that line runs,
so the button sat at `STARTING…` for the life of the page with no error.

A screenshot of the phone then showed the real trigger: the tab said **Offline**
in the omnibox. The phone had dropped off JZCTRL entirely (`cmd wifi status`:
DISCONNECTED, no IP). It was a stale tab from an earlier session.

**Fixed:** a 12-second watchdog alongside the session request. If it has not
settled, the button returns as `RELOAD & RETRY` and the page explains where the
Allow prompt is. Any rejection thrown outside the old try block now re-enables
the button, and a desktop browser says so on load instead of looking live.

**Verified** by stubbing `requestSession` to a promise that never resolves: grey
`STARTING…` at 600 ms, `RELOAD & RETRY` at 1.5 s.

---

## 3. Driving the phone

Done over `adb` + Chrome DevTools Protocol.

- Rejoined JZCTRL (`cmd wifi connect-network`, PSK read from the Pi's
  NetworkManager). Phone came up at `10.42.0.226`, RSSI −30.
- Forced landscape (`accelerometer_rotation 0`, `user_rotation 1`) so the
  session cannot inherit portrait.
- Opened a fresh tab and tapped ENTER AR. It entered immediately — no
  permission prompt; the origin already held the grants. TLS `secure`, mkcert
  issuer, TLS 1.3, no warning.
- **Media volume was 0.** Not Do Not Disturb this time — the volume itself. The
  7 dialogue lines that had played were inaudible. Set to 4/15.

Live session read at that moment: 89 fps, controller connected with **0 drops
and 0 bounces** over 885 frames, world `Void Depths` from `source: upstream`
`via: frame` (so the camera-capture path was working, not canned), capture
17.4 ms, LEDs 248 sent / **0 failed**, 7 voice lines spoken / 0 failures.

**Operational note for whoever inherits this:** the phone drops off JZCTRL
repeatedly because Android deprioritises an AP with no internet. A stale tab
from a drop shows `Offline` and its ENTER AR is dead forever. Always open a
fresh tab. This is now in the project memory file.

---

## 4. Dialogue

### Never repeating

`voices.js` retires a line the moment it is spoken — permanently, for the
session, across mobs, roles and worlds. When it is time for someone to talk it
walks the mobs in random order and takes the first still holding an unused line;
a mob that has run dry is skipped rather than silencing the frame. If everyone
is out, they go quiet until the next world. **Silence, never repetition** — that
was a deliberate choice and it is worth defending: a repeated line is what makes
a crowd read as a loop instead of a place.

Retirement is keyed on the line *before* truncation and case-folded, so a long
line cannot return through the 140-character cut and two spellings of the same
sentence count once.

That rule only works if worlds carry enough dialogue, and they did not:

- **Nemotron** is now asked for **six genuinely different lines per role**
  (was 2–3) — 24 per world — and told why. `clamp_barks` caps at six and drops
  case-insensitive duplicates.
- **The eight canned worlds** carried one or two lines per role and would have
  gone silent in about fifteen seconds. All rewritten to six per role:
  **192 lines, all 192 distinct**, none shared between worlds, because a shared
  line would only ever be heard once.

### Less robotic

Four causes, three of which were not the model:

1. **The model was the cheap one.** `eleven_flash_v2_5` is distilled for latency
   and reads a bark flat. Probed the account against every model and bitrate —
   all nine combinations work — and switched to `eleven_multilingual_v2`
   (830 ms vs 210 ms, which does not matter: the bubble shows instantly and
   audio is cached to disk forever).
2. **The audio was telephone quality**: `mp3_22050_32`. Now `mp3_44100_128`.
   Same line went 4.7 kB → 22.6 kB. Bitrate is not billed; characters are.
3. **`use_speaker_boost` was missing**, and stability was set low on the
   assumption that low means expressive. It means wobbly, and wobble reads as
   synthetic just as much as monotone. Settings now sit mid-range.
4. **The lines had no punctuation.** `"Mind the cabling"` is a fragment with no
   sentence to shape. Terminated before synthesis (runners get `!`), while the
   bubble still shows what the model wrote.

Budget is now expressed in **credits** rather than characters, since multilingual
bills double — capped at 6,000 of the 10,000 in the account. The cache digest
covers model, format and settings, so a retune re-synthesises rather than
serving stale audio.

---

## 5. Mute, popups, sensitivity

### Mute

`DIALOGUE → MUTE` on the dashboard. It mutes the **speaker**, not the dialogue:
bubbles still appear and lines are still retired. A mute that quietly changed
what the game was doing would be a worse bug than the noise. The button reads
its state from telemetry, not from what was last clicked, so a command that
never arrived cannot leave it lying.

### The popups were a real bug

The bubble was hidden whenever the speaker was outside roughly the 60° being
looked at. Mobs stand in a ring, so most speakers are to the side or behind —
nearly every line was drawn and then culled.

Two fixes: voices now offers the line to a mob **in view** first (falling back to
anyone rather than going quiet), and an off-screen speaker's bubble is **clamped
to the edge with an arrow** instead of vanishing. Behind-camera projection
mirrors, so x is flipped back before use or the arrow points the wrong way.

Added `bubble: {shown, atEdge, hidden, errors}` to telemetry so "I never see the
popups" is answerable from data next time.

While checking this, the live session read **140 lines, 140 spoken, 0 silent** —
the voices had been working the whole time; the speaker was just never visible.

### Sensitivity

`OFF` is now the first stop. It is not "very quiet": the meter stops reading the
room and holds hostility at 0.30, deliberately *between* the population's calm
and hostile thresholds, so OFF freezes the mix rather than handing the player an
all-peaceful or all-zombie field. The chip turns amber.

MIN went from +12 dB to +24 — it was still reaching 0.99 on a shout, which is
what "the LEDs all came on at min" was:

| room | before | after |
|---|---|---|
| −30 dBFS | 0.49 | 0.37 |
| −20 dBFS | 0.74 | 0.57 |
| −10 dBFS | 0.99 | 0.76 |

Spacing is now 12 dB below normal and 6 dB above: the quiet end never needed
more room, the loud end did.

**This direction was confirmed with the user before shipping**, because "lower
the min threshold" has two opposite readings and their phone was reading −54.4
dBFS at hostility 0.00 at the time. They chose less sensitive.

**For the hall:** press CAL once on site, in the real noise. The presets only
shift the ceiling; calibration sets the floor, and that is the actual fix.

---

## 6. 2D mode

New `game/www/js/flatmode.js` plus a branch in the page. Same scene, same mobs,
same dialogue, same snapshot, same lamps — only the camera differs.

In AR the player WALKS, so the stick deliberately was not movement; it aimed. On
a flat screen nobody walks, so the stick becomes the legs:

- **Y — walk** forward and back along the facing
- **X — turn**, not strafe. Strafing leaves you unable to face anything, and
  facing is what the throw needs. The X-flick target cycling is disabled in 2D
  for the same reason: a hard turn would cycle a target on every sweep.

Velocity is integrated towards the stick, so a shove moves 4.5 mm on the first
frame instead of lurching. A 14 m leash is slid along, not stopped dead at, so
the backdrop edge is never a visible seam. Without the controller, drag to turn
and tap to throw.

The dashboard gets a `MODE` row. **One honest limit:** it can put the game into
2D from anywhere but cannot put it back into AR, because `requestSession()`
requires a user gesture and an HTTP command is not one. Asking for AR tears 2D
down and says on the phone that the last step is a tap. Faking that would have
failed silently.

**Verified by running it** — 2D needs no WebXR, so unlike AR it is testable on
the Mac. Two farm zombies in front, the ground tinted to the world accent, a
bubble reading *"Through the rows"*, countdown at SCAN READY. Two bugs were
caught doing so: `session.requestReferenceSpace` and `session.addEventListener`
both still assumed a session, and the second killed the run outright.

---

## 7. On-chain settlement (Solana devnet)

New `chain/` directory. **Strictly additive**: it is a read-only poller. It GETs
the same `/telemetry` the dashboard reads, notices a run ended, and mints. No
change to `game/`, none to `controller-bridge/`, none to the `/world` path.

### Why it is shaped this way

A transaction inside the combat loop would cost frames, so settlement happens on
the boundaries the game already has: **death**, and an **explicit trigger**.
Batching is only economically sane because a signature is ~5000 lamports — the
verified transaction cost **0.000005 SOL for a whole run**. That is the real
answer to "why this chain": at dollar-scale fees you are forced into an
off-chain ledger and a periodic bridge, at which point the chain is decoration.

### The risk verification, which was the point

Ran the service against the live session, then `kill -9`:

| | before | during | after kill −9 |
|---|---|---|---|
| telemetry seq | 11637 | 11698 | 11750 |
| session clock | 5237563 | 5250454 | 5261404 |
| fps | 89 | 89 | 89 |
| SSE clients | 1 | 1 | 1 |
| bridge uptime | 2451 | 2463 | 2474 |

The session clock never reset and bridge uptime never dropped — same session,
same service, straight through the kill.

### What is on chain

- **Mint** `9fn5gZCJpeyHmNc2PLxx2SR2zK9DfdadzsnvSHNt42FD`, 0 decimals, supply 250
- **Transaction** `W2azPScy6kc8sWXn58a7dA5EeUJomuiB7QsPF9BcSrB5AdmPYyVbrFggKCTnGU7fb7ppUhyFfJ4AacbXge2vF93`
- Verified against devnet directly, not from the service's own log:
  `mintToChecked`, token balance 0 → 250, no on-chain error, fee 5000 lamports.

Solscan: `https://solscan.io/tx/W2azPScy6kc8sWXn58a7dA5EeUJomuiB7QsPF9BcSrB5AdmPYyVbrFggKCTnGU7fb7ppUhyFfJ4AacbXge2vF93?cluster=devnet`

### Failure behaviour

Never raises. Every RPC call retries with exponential backoff and jitter then
gives up quietly. A settlement that did not land is marked `failed`, **kept in
the queue**, retried with backoff capped at two minutes, and shown as failed.
`last_confirmed()` ignores failures by construction, so a failure can never be
rendered as the last successful transaction. The queue is persisted, so a crash
cannot lose a run. The process **refuses to start** against a non-devnet RPC.

### Two bugs found while checking my own work

1. The panel read **"already settled: 525"** when 250 tokens existed — that was
   the watermark from attaching mid-run, not tokens. Exactly the number a judge
   is right to distrust. `minted`, `watermark` and `pre_existing` are now
   separate and the panel shows "predates this service: N not claimed".
2. A test written for that caught a second: on a session boundary the flushed
   points were credited to the **new** run, because the caller did the
   bookkeeping after the tracker had reset. The tracker now claims at the point
   the amount is decided. Session detection was also too lax — a reload
   restarting at `t=10` a second into a run was silently merged into it.

### Keys

`chain/keys/*.json`, mode 600, gitignored. `.gitignore` was extended **first**,
as instructed, and proven by creating real files at each path and confirming git
could not see them. Devnet only; nothing runs in a browser.

---

## 8. Verified on the device vs not

**Verified on the phone:** AR entry with no certificate warning and no prompt;
89 fps; controller 885 frames / 0 drops / 0 bounces; camera capture 17.4 ms
posting a real frame and getting an upstream world back; LEDs 248 sent / 0
failed; 140 dialogue lines spoken with 0 failures; volume found at 0 and fixed.

**Verified on the Mac only:** the ENTER AR watchdog, 2D mode end-to-end, the
new canned dialogue, the TTS model and bitrate change (bytes and latency, *not*
listened to), the Solana settlement.

**Not verified anywhere:** the phone came off USB after the third deploy and its
telemetry showed an older build (no `mode`, no `retired` fields). **Nobody has
seen the no-repeat dialogue, the new voices, the edge-clamped bubbles, the OFF
sensitivity stop, or 2D mode on the actual phone.** The first thing to do is
reload the page on it and confirm those five.

Also still unverified from the previous session: nobody has walked a room with
the phone, so fast-motion stalls, aim feel and bubble placement while moving
remain untested in motion.

---

## 9. State being handed over

- **Uncommitted:** 12 modified files, plus `chain/`, `game/www/js/flatmode.js`,
  `game/test_flatmode.mjs` and three reports untracked. Nothing has been
  committed this session.
- **Deployed to the Pi:** everything except the last `flatmode` fixes were
  deployed and `jzbridge` verified active after each one. Deploy restarts the
  service and drops any live session — worth knowing before judging.
- **Running on the Mac:** `server.py` on 8099 (world pipeline, restarted for the
  TTS change) and `chain/jzchain.py serve` on 8100. Neither is supervised; both
  die with the terminal.
- **Not applied, deliberately:** `chain/add_to_dashboard.py` puts the on-chain
  card on the game dashboard. It touches `game/` and needs a deploy, so it was
  left for after judging. `--check` and `--revert` both work.
- **Secrets:** the ElevenLabs key is in `.env` and was pasted into a transcript
  in an earlier session; rotating it after the demo is still recommended.
- **Unsolved:** the ESP32 power bank still cuts out. See
  `REPORT-power-bank.md`. During this session `esp_connected` was observed
  flapping false→true, which is that same problem, not a regression.

## 10. Test suite

607 assertions across 13 files, all passing.

| file | assertions |
|---|---|
| test_controller.mjs | 82 |
| test_player.mjs | 68 |
| test_mic.mjs | 67 |
| test_jzchain.py | 59 |
| test_slash.mjs | 59 |
| test_voices.mjs | 55 |
| test_worldcycle.mjs | 48 |
| test_sprites.mjs | 35 |
| test_worldsource.mjs | 34 |
| test_weapon.mjs | 33 |
| test_flatmode.mjs | 31 |
| test_aura.mjs | 25 |
| test_mobs.mjs | 11 |

Run with `cd game && for f in test_*.mjs; do node $f; done` and
`cd chain && ./.venv/bin/python test_jzchain.py`.

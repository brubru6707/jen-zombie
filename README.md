# jen-zombie pipeline

The photo -> caption -> biome pipeline: as an HTTP service the game calls, as a
terminal harness, and as a benchmark rig for choosing the model/config.
Stdlib + Pillow, nothing else.

    cp .env.example .env        # add NVIDIA_API_KEY and GEMINI_API_KEY
    python3 server.py           # the service the game calls
    python3 pipeline.py         # random image every 30s, Ctrl-C for the tally
    python3 pipeline.py --once
    python3 pipeline.py --image images/water_beach_surf.jpg

## The world service

    python3 server.py                      # 0.0.0.0:8099
    python3 server.py --port 8099 --budget 25

`server.py` wraps `pipeline.py` without reimplementing it: the model calls, the
retries, the water rule, the clamping and the jsonl logging all still live in
`pipeline.py`. It ships with the measured configuration already applied
(`JZ_VISION_PROVIDER=gemini`, `JZ_NEMOTRON_MODEL=nvidia/nemotron-3-super-120b-a12b`,
`JZ_REASONING_EFFORT=none`); a real environment variable still overrides any of
them.

    POST /world    body: a JPEG, raw bytes, Content-Type: image/jpeg
                   200:  {"name","biome","count_scale","speed_scale",
                          "detect_scale","brute_bias","hp_bonus","barks",
                          "caption","fallback","ms":{"vision","nemotron"}}
    GET  /health   200: both keys, present and reachable, plus config and cache

    curl -X POST --data-binary @images/water_beach_surf.jpg \
         -H "Content-Type: image/jpeg" http://10.42.1.71:8099/world

It binds all interfaces, because the Pi reaches this Mac over the wired link
and reverse-proxies to it:

    JZ_WORLD_UPSTREAM=http://10.42.1.71:8099/world    # on the Pi
    JZ_WORLD_TIMEOUT_S=30                             # must exceed our budget

Three rules the game depends on:

  * **A request always ends in a world.** If the models fail, time out or the
    keys are missing, the reply is still 200 with a playable world -- the
    pipeline defaults, biome `meadow`, every scale 1.0 -- carrying
    `"fallback": true` and named "Fallback Level". A fallback is never
    disguised as a real answer, is logged between `!!!!` banners with the
    reason, and is never cached. There is no 500 with an empty body anywhere
    in the `/world` path.
  * **25 seconds of wall clock, hard.** `pipeline.py`'s deadline is
    cooperative -- each HTTP attempt and each backoff checks it -- and on top
    of that the work runs on a worker thread the handler stops waiting on when
    the budget expires. A slow answer is survivable for the game; a hang is
    not.
  * **Repeated frames are free.** Worlds are cached on the sha256 of the JPEG
    bytes (128 entries, LRU), so a re-sent frame re-bills neither model and
    comes back in 0 ms.

Measured on this Mac, five distinct images, cold cache: median 4.6 s, worst
24.2 s. The worst case was three NVIDIA 503s ("Service temporarily
overloaded") and the pipeline's own backoff recovering on the fourth attempt,
inside the budget. `logs/model_chat.jsonl` has the traffic either way.

## Recommended configuration (measured)

    export JZ_NEMOTRON_MODEL=nvidia/nemotron-3-super-120b-a12b
    export JZ_REASONING_EFFORT=none
    export JZ_VISION_PROVIDER=gemini      # needs GEMINI_API_KEY in .env

End-to-end median 3.5 s / p95 11.3 s, versus 6.1 s / 21.7 s with the llama
vision leg and 25 s+ before any of this work. Set JZ_VISION_PROVIDER=nvidia to
flip the vision leg back; leave all four unset for the original control.

Gemini auth: generativelanguage.googleapis.com/v1beta with the key as an
x-goog-api-key header (a ?key= query parameter also works; OAuth bearer and
both Vertex AI paths do not).

Median Nemotron latency 1.6 s / p95 2.1 s, versus 25.4 s / 44.7 s for the
default. Biome accuracy is equal or better. Leave both unset to get the
original benchmarked control.

## Benchmark rig

    python3 bench.py models       # which nemotron ids this account can reach
    python3 bench.py configs --n 5 --alt-model nvidia/nemotron-3-super-120b-a12b
    python3 bench.py race  --config G3 --alt-model nvidia/nemotron-3-super-120b-a12b
    python3 bench.py accuracy --only A,G,G3 --alt-model nvidia/nemotron-3-super-120b-a12b
    python3 bench_waterrule.py    # A/B on the water tie-break rule

Results land in logs/bench_*.json; raw traffic in logs/bench_chat.jsonl and
logs/model_chat.jsonl.

## Files

    server.py           the HTTP service: POST /world, GET /health
    pipeline.py         the harness (was harness.py -- see note)
    bench.py            benchmark rig: models / configs / race / accuracy
    bench_waterrule.py  focused A/B on the water tie-break rule
    images/             test photos, filename prefix = expected biome
    images/manifest.txt fallback list used when the OS denies directory listing
    harness.py          STALE. Superseded by pipeline.py; safe to delete.

`harness.py` is the pre-benchmark version. macOS TCC revoked this machine's
read/execute/rename access to that inode mid-session, so it was rebuilt as
`pipeline.py` with the same code plus the fixes. If `ls` or reads in this
folder ever fail with "Operation not permitted" despite correct file modes,
that is TCC on ~/Documents -- grant Full Disk Access to your terminal, or move
this folder outside ~/Documents.

See images/README.md for the filename convention.

## Tools and equipment

### Hardware

| Item | Role |
|---|---|
| Samsung Galaxy A15 5G (SM-S156V), Android 15 | AR device. 6DoF tracking, camera, microphone |
| ESP32-D0WD-V3 handheld | Controller: analog joystick, action + stick buttons, sensitivity toggle, 3-LED meter |
| CP2102 USB-to-serial bridge | Firmware flashing and the wired controller transport |
| External USB battery | Untethered power for the handheld |
| Raspberry Pi 4 | Dedicated 2.4 GHz access point and controller bridge. The ESP32 is 2.4 GHz-only and venue WiFi is 5 GHz |
| macOS laptop | HTTPS server, world service host, ADB host |

### Models and APIs

| Service | Model | Role |
|---|---|---|
| NVIDIA | `nemotron-3-super-120b-a12b` | Turns a caption into a world: biome, difficulty scales, per-mob dialogue |
| Google | `gemini-3.5-flash-lite` | Captions the camera frame |
| ElevenLabs | `eleven_multilingual_v2` | Speaks the mob dialogue Nemotron writes for each world |
| NVIDIA | `llama-3.2-11b-vision-instruct` | Benchmarked alternative vision leg, selectable |

### Software

| Tool | Role |
|---|---|
| WebXR Device API + ARCore 1.56 | AR session, hit-test, anchors, camera access, `local-floor` |
| three.js | Rendering |
| Chrome 153 (Android) | WebXR host |
| Web Audio API (`AnalyserNode`) | Microphone loudness for the hostility mechanic |
| Python 3 | Pipeline and server. Stdlib `urllib` + Pillow, no HTTP dependency |
| mkcert | Local CA for the HTTPS origin WebXR requires |
| adb / platform-tools | Device probing, port tunnelling, firmware host |
| arduino-cli + ESP32 Arduino core | Controller firmware |
| Solana (devnet) + SPL Token | On-chain score settlement. `solders` / `solana-py` |
| DigitalOcean | Droplet hosting the public live-view site |
| .tech domain | jenzombie.tech, the public site |
| Kenney sprite packs (CC0) | Sprite art |

### AI tools

Disclosed per SteelHacks rules. AI coding assistants were used heavily
throughout this project.

| Tool | What it was used for |
|---|---|
| Claude (Claude Code) | Pair-programming across the whole build: the model benchmark rigs, the WebXR client, the ESP32 firmware rewrite from Bluetooth SPP to WiFi TCP, the Raspberry Pi bridge, the world HTTP service, the Solana settlement poller, and the test suites. Also used for planning, code review, and debugging sessions. |
| Claude (Fable) | The AR client build and the Pi integration work. |

Design decisions, the hardware build, all measurements and every verification
on the physical device were done by me. The benchmark results quoted in this
README were produced by running the rigs in `bench*.py`, which are in this
repository and re-runnable.


# jen-zombie pipeline harness

Terminal-only test harness for the photo -> caption -> biome pipeline, plus a
benchmark rig for choosing the model/config. Stdlib + Pillow, nothing else.

    cp .env.example .env        # add NVIDIA_API_KEY
    python3 pipeline.py         # random image every 30s, Ctrl-C for the tally
    python3 pipeline.py --once
    python3 pipeline.py --image images/water_beach_surf.jpg

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
| macOS laptop | HTTPS server, pipeline host, ADB host |

### Models and APIs

| Service | Model | Role |
|---|---|---|
| NVIDIA | `nemotron-3-super-120b-a12b` | Turns a caption into a world: biome, difficulty scales, per-mob dialogue |
| Google | `gemini-3.5-flash-lite` | Captions the camera frame |
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
| Kenney sprite packs (CC0) | Sprite art |

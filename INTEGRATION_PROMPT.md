# Integration prompt (paths corrected after the move out of ~/Documents)

Both folders now live outside ~/Documents (macOS TCC kept revoking access):

    ~/dev/zombie-ar     the game (was ~/Documents/FUQ_WINDOWS/bat_cave/zombie-ar)
    ~/dev/jen-zombie    the pipeline + controller bridge (was ~/Documents/FUQ_WINDOWS/bat_cave/jen-zombie)

Start Claude Code (or any agent) from ~/dev/jen-zombie. The original prompt,
with only the paths changed:

---

Connect the AR game to the physical ESP32 controller. Two halves were built
separately and neither talks to the other. Your job is to make them one system.

WHAT EXISTS -- read both before changing anything

GAME (Mac): ~/dev/zombie-ar
  serve.py serves the game over HTTPS 8443 (mkcert) and HTTP 8080.
  Stages 0-3 complete plus sprites, weapons, mic metering, dashboard.
  139 passing assertions across test_weapon / test_sprites / test_mic /
  test_worldcycle / test_mobs. Do not break them.
  Currently reaches the phone over an adb USB tunnel.

PIPELINE (Mac): ~/dev/jen-zombie
  Gemini vision + Nemotron. API keys in .env. The Mac is the ONLY machine with
  internet, so the pipeline stays here. Do not move it.

CONTROLLER (Raspberry Pi, 10.42.0.1): bridge.py, systemd service jzbridge
  Read controller-test/PROTOCOL.md first -- it is authoritative.
  SSH from the Mac: pi@10.42.1.1, key installed, passwordless sudo.
  The Pi is a 2.4 GHz access point, SSID JZCTRL. The ESP32 is 2.4 GHz-only and
  the venue is 5 GHz, which is why the Pi exists.
  Live now: TCP 3333 (ESP32 dials in), HTTPS 8443, SSE /events, HTTP 80/8080.

(The rest of the prompt is unchanged; see the session that did the work for
what was built: controller-bridge/README.md and zombie-ar/README.md.)

# controller-bridge — the Pi, one origin for the whole demo

Everything the phone touches is served from **https://10.42.0.1:8443** on the
Raspberry Pi (AP `JZCTRL`): the AR game, the controller stream, the world data
and the LED endpoint. Same origin means no CORS, no mixed content, one
certificate, and the browser never loses state changing URLs. The Mac is only
needed for the vision/Nemotron pipeline (later) and as the adb fallback.

    Mac (10.42.1.71, wired) ──ssh/rsync──▶ Pi eth0 10.42.1.1
                                            Pi wlan0 10.42.0.1  ◀──WiFi── phone 10.42.0.226
                                            TCP :3333            ◀──WiFi── ESP32 10.42.0.244

    bridge.py     systemd `jzbridge`. ESP32 TCP :3333 (PROTOCOL.md), web on :80 :8080 :8443
    gameweb.py    imported by bridge.py: static game (./www), /world, /telemetry, /result,
                  /events (SSE), /ws, POST /led, /status, captive-portal answers
    worlds.py     canned worlds, copied from zombie-ar (identical behaviour to serve.py)
    www/          rsync mirror of zombie-ar/www -- never edit here, edit the game and deploy
    controller.html, game.html   the older visualiser (/test) and 2D game (/game.html)
    certs/        mkcert cert for 10.42.0.1 10.42.1.1 jenzombies localhost (gitignored)
    deploy.sh     rsync + restart + verify

## Deploy

    ./deploy.sh                # game + bridge -> Pi, restart jzbridge, verify routes
    CERTS=certs ./deploy.sh    # also install the certificate (old one kept as *.selfsigned)
    ./deploy.sh --dry-run

The verify step fails loudly if the service does not come back or /status
does not answer. `journalctl -u jzbridge -f` on the Pi for live logs.

## After a power cycle

The Pi recovers on its own: `jzbridge` is enabled, so it starts at boot, the
`jzap` access point autoconnects, and the ESP32 redials with its own backoff.
Verified after an unplug: service active, all four listeners bound, AP up,
controller streaming, about 30 seconds end to end.

**The phone is the part that does not come back by itself.** Android scores a
network with no internet poorly and will sit next to a broadcasting JZCTRL
without rejoining, which looks exactly like the Pi being broken. The bridge
already answers the connectivity probes so the phone does not abandon the AP
while it is up, but after the AP disappears entirely the phone usually needs a
nudge: toggle WiFi, or tap JZCTRL in the network list. Over adb:

    adb shell cmd wifi connect-network JZCTRL wpa2 '<the jzap psk>'

The key lives on the Pi: `sudo nmcli -s -g 802-11-wireless-security.psk con show jzap`.

## Certificate

Generated on the Mac with the same mkcert CA the phone already trusts
(`Settings > Security > Install a certificate > CA` was done for the Mac
origin). The Pi's cert needs `10.42.0.1` in its SANs or Chrome shows the
interstitial, and a click-through exception makes WebXR and getUserMedia
unpredictable -- so regenerate rather than click through:

    mkcert -cert-file certs/cert.pem -key-file certs/key.pem 10.42.0.1 10.42.1.1 jenzombies localhost 127.0.0.1
    CERTS=certs ./deploy.sh

Verified 2026-09-19 from the phone's Chrome via DevTools: document
`securityState=secure`, issuer = the mkcert CA, TLS 1.3, no warning.

## Controller endpoints (used by zombie-ar/www/js/controller.js and led.js)

    GET  /events             SSE, `data: {"t":"state","x","y","sw","atk","sens","connected","seq","ts"}`
                             x,y -1..1 deadzoned; sens 2048|4095; ~2 Hz idle, up to ~125 Hz
    POST /led {"led":[g,y,r]}   0..1 each -> "LED g y r" to the board, written immediately
    GET  /status             ESP link, SSE/WS client counts, last LED, counters
    GET  /trace?n=12         read-only passthrough to the Mac pipeline service:
                             the last model exchanges as text, for the dashboard
    GET  /pipeline-health    passthrough to the same service's /health
    GET  /say?role=&text=    the line spoken, as audio/mpeg, from ElevenLabs via
                             the Mac service. Cached there by (voice, model,
                             text) and cacheable here for a day: a line never
                             changes. A failure passes the service's own reason
                             through so the game can show why it fell silent.
    GET  /voices             which voice plays which role, and the spend so far
    GET  /frames, /frame?id= the last few camera frames the pipeline was given,
                             so the dashboard can show the photo that belongs to
                             a particular exchange
    GET  /world?seq=&delay=  canned worlds, or a proxy (below)
    POST /world              a JPEG body: proxied to the Mac pipeline, kept at
                             /last-frame.jpg for the dashboard
    POST /command {"cmd":…}  queued for the headset: trim, preset, calibrate,
                             resetcal. It is handed over on the reply to the
                             headset's next telemetry POST (5 Hz), so the
                             dashboard can drive the phone without a second
                             connection. Commands older than 25 s are dropped
                             rather than fired at a phone that just reconnected.

`bridge.py` now also reports a board that has gone silent for 2 s
(`JZ_STALE_S`) as `connected:false` with neutral input (PROTOCOL.md 5), and
`send_led` writes straight to the socket instead of waiting for the next
heartbeat (was up to 500 ms of lag).

## Pointing /world at the Mac pipeline (the one-line change)

    # /etc/systemd/system/jzbridge.service, [Service]
    Environment=JZ_WORLD_UPSTREAM=http://10.42.1.71:8099/world
    Environment=JZ_WORLD_TIMEOUT_S=32

then `sudo systemctl daemon-reload && sudo systemctl restart jzbridge` (set on
2026-09-19). The service (`jen-zombie/server.py`, 8080 is taken by serve.py)
wants **POST + a JPEG** and answers 405 to a bare GET, so only a request
carrying a frame is forwarded; the game's frame-less GET is served canned
(`source: canned-no-frame`). Method, body and query string are forwarded, the
response is returned as-is, and any failure or timeout falls back to the
canned set (`canned-fallback`). The service budgets 25 s per world, with
NVIDIA 503 storms retried inside that, so the Pi timeout is 32 s and the
game's own fetch abort is 40 s -- a 20 s or 12 s cutoff would have thrown
away a world that was about to land.

## Bench testing on the Mac without touching the Pi

    JZ_TCP_PORT=13333 JZ_HTTP_PORTS=18080 JZ_HTTPS_PORT=18443 python3 -u bridge.py

with `www/`, `worlds.py`, `cert.pem`, `key.pem` beside it (real copies, not
symlinks: Python resolves a symlinked script and the module then looks for
`www` next to the real file).

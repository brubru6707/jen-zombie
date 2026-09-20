"""
Same-origin game server for the Pi. Imported by bridge.py, which hands over
its Hub and socket-level helpers via install(); nothing here talks to the
ESP32 directly and nothing here runs until bridge.py calls web_server().

One origin, https://10.42.0.1:8443, serves all of it, so the phone sees no
CORS, no mixed content and one certificate:

    /                          the AR game: ./www, a mirror of zombie-ar/www
    /world?seq=&delay=&jitter= canned worlds from worlds.py -- or, once
                               WORLD_UPSTREAM is set, a proxy to the Mac's
                               pipeline with the canned set as the fallback
    /events                    controller state as Server-Sent Events
    /ws                        the same over WebSocket (legacy controller page)
    POST /led {"led":[g,y,r]}  drive the handheld's three lamps, 0..1 each
    /status                    bridge health: ESP link, clients, last LED
    GET|POST /telemetry        headset -> dashboard, in memory
    POST /result/<name>, /probe-result      logs/<name>.json + .jsonl
    /test, /game.html          the older controller visualiser and 2D game
    generate_204 & co.         captive-portal answers so the phone stays on
                               the AP (see bridge.py for why this matters)

Static files, /world, /telemetry and /result behave exactly as zombie-ar's
serve.py on the Mac, so the game cannot tell which server it came from.
"""
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
WWW = os.path.join(HERE, "www")
LOGS = os.path.join(HERE, "logs")

# ---- the one line to change when the Mac pipeline is ready -------------------
# Live: JZ_WORLD_UPSTREAM=http://10.42.1.71:8099/world   (jen-zombie/server.py on the
# Mac over the wired link; 8080 there is zombie-ar/serve.py). The service budgets
# 25 s per world (NVIDIA 503 storms retried inside it), so the timeout must be
# above that: JZ_WORLD_TIMEOUT_S=32.
# Set it in the systemd unit (Environment=...) or edit the default here. Any
# failure or timeout upstream falls back to the canned set, so the countdown
# never waits on the Mac.
WORLD_UPSTREAM = os.environ.get("JZ_WORLD_UPSTREAM", "").strip()
# Everything else on the pipeline service hangs off the same base, so the
# dashboard can read the model traffic from this origin instead of reaching
# across to the Mac (which it cannot do: it is served from the Pi).
UPSTREAM_BASE = WORLD_UPSTREAM.rsplit("/world", 1)[0] if WORLD_UPSTREAM else ""
UPSTREAM_TIMEOUT_S = float(os.environ.get("JZ_WORLD_TIMEOUT_S", "20"))
WORLD_DELAY_MS = int(os.environ.get("JZ_WORLD_DELAY_MS", "0"))

sys.path.insert(0, HERE)
try:
    import worlds as WORLDS
except Exception as _e:                    # the game must still be served
    WORLDS = None
    print(f"[web] worlds module unavailable: {_e}", flush=True)

# Filled in by bridge.py; every controller route answers 503 until then.
BRIDGE = {"hub": None, "send_led": None, "sse_serve": None,
          "ws_handshake": None, "ws_read_loop": None,
          "https_port": 8443, "started": time.time()}


def install(**kw):
    BRIDGE.update(kw)


TELEMETRY = {"data": None, "at": 0.0, "seq": 0}
TELEMETRY_LOCK = threading.Lock()

# Commands the dashboard queues for the headset. They ride back on the reply to
# the headset's 5 Hz telemetry POST: no second connection to the phone, nothing
# to reconnect, and nothing on the wire while the queue is empty.
COMMANDS = {"items": [], "seq": 0}
COMMANDS_LOCK = threading.Lock()
COMMAND_KINDS = ("trim", "preset", "calibrate", "resetcal")
COMMAND_TTL_S = 25.0
COMMAND_MAX = 16


def command_push(cmd):
    with COMMANDS_LOCK:
        COMMANDS["seq"] += 1
        cmd["id"] = COMMANDS["seq"]
        cmd["at"] = time.time()
        COMMANDS["items"] = (COMMANDS["items"] + [cmd])[-COMMAND_MAX:]
        return len(COMMANDS["items"])


def command_drain():
    with COMMANDS_LOCK:
        if not COMMANDS["items"]:
            return []
        now = time.time()
        fresh = [c for c in COMMANDS["items"] if now - c.get("at", 0) <= COMMAND_TTL_S]
        COMMANDS["items"] = []
        return fresh


def command_parse(payload):
    """Validate a dashboard command. Returns the command, or None."""
    if not isinstance(payload, dict):
        return None
    kind = payload.get("cmd")
    if kind not in COMMAND_KINDS:
        return None
    out = {"cmd": kind}
    if kind == "trim":
        try:
            out["db"] = max(-24.0, min(24.0, float(payload.get("db", 0))))
        except Exception:
            return None
    elif kind == "preset":
        name = payload.get("preset")
        if not isinstance(name, str) or not name.isalpha() or len(name) > 12:
            return None
        out["preset"] = name
    return out

# Polled at 5-10 Hz by the dashboard and the LED meter; keep them out of the journal.
QUIET = ("/telemetry", "/led", "/status", "/last-frame.jpg", "/trace", "/say")
# On the plain-HTTP ports these navigations go to HTTPS: AR needs a secure
# context. The dashboard is deliberately NOT redirected (a laptop without the
# mkcert CA reads it over plain HTTP on 8080).
REDIRECT_PAGES = ("/", "/index.html", "/probe.html", "/stage1.html", "/stage2.html", "/stage3.html")


# The dashboard is for the laptop. The phone is the game, and a page of
# diagnostics on the device you are holding to your face is worse than
# useless -- it is what you see instead of the room. Refused by user agent
# rather than by address, so a laptop on the AP still gets it.
def is_handheld(ua):
    ua = (ua or "").lower()
    return ("android" in ua) or ("iphone" in ua) or ("ipad" in ua) or ("mobile" in ua)


DASHBOARD_ELSEWHERE = (b"<!doctype html><meta name=viewport content='width=device-width'>"
                       b"<body style='background:#0b0d10;color:#e8ecf1;font:16px/1.6 ui-monospace,monospace;padding:28px'>"
                       b"<h2 style='margin:0 0 10px'>The dashboard lives on the laptop</h2>"
                       b"<p style='color:#8b96a5'>This phone runs the game. Open "
                       b"<b>http://10.42.1.1:8080/dashboard.html</b> on the computer.</p>"
                       b"<p><a style='color:#4aa3ff' href='/stage3.html'>back to the game</a></p>")


class GameServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    tls = False
    port = 0

    _err_seen = {}
    _err_lock = threading.Lock()

    def handle_error(self, request, client_address):
        # Client went away mid-response, TLS handshake refused, plain HTTP on
        # the TLS port, and so on: one line per client and error type per
        # minute, never a traceback, so a stale tab cannot bury the journal.
        et, ev = sys.exc_info()[:2]
        key = (client_address[0], et.__name__ if et else "?")
        now = time.time()
        with self._err_lock:
            last = self._err_seen.get(key, 0)
            if now - last < 60:
                return
            self._err_seen[key] = now
        print(f"[web] {key[0]} {key[1]}: {ev}  (repeats muted for 60 s)", flush=True)


class GameHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive: ~40 assets per page load
    timeout = 60                           # idle keep-alive connections give the thread back
    server_version = "jzgame/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WWW, **kw)

    def setup(self):
        # The TLS handshake happens HERE, on the connection's own thread, never
        # in accept(): a client that connects and stalls cannot freeze the
        # listener for everyone else.
        if getattr(self.server, "tls", False):
            self.request.settimeout(10)
            self.request.do_handshake()
        super().setup()

    # ------------------------------------------------------------ plumbing --
    def log_message(self, fmt, *args):
        if any((self.path or "").startswith(q) for q in QUIET):
            return
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)

    def log_error(self, fmt, *args):
        # An idle keep-alive connection reaching `timeout` is normal (Chrome
        # parks connections); it is not an error worth a journal line.
        if args and isinstance(args[0], TimeoutError):
            return
        if "timed out" in (fmt % args if args else fmt):
            return
        self.log_message(fmt, *args)

    def end_headers(self):
        # No caching, as on the Mac: the phone must never run a stale page.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _json(self, code, obj):
        try:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            print(f"[web] failed to write response: {e}", flush=True)

    def _bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_error(404, "File not found")
        return self._bytes(200, body, ctype)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        n = max(0, min(n, 1 << 20))
        return self.rfile.read(n) if n else b""

    # ------------------------------------------------------------- routes --
    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            p, low = u.path, u.path.lower()
            if p == "/events":
                return self._sse()
            if p.startswith("/ws"):
                key = self.headers.get("Sec-WebSocket-Key")
                if key:
                    return self._ws(key)
            # Android / Samsung "do I have internet?" probes. A 204 makes the
            # phone treat JZCTRL as validated and stop abandoning it for having
            # no upstream. Windows/Apple variants kept for completeness.
            if "generate_204" in low or "gen_204" in low or "/generate204" in low:
                return self._empty(204)
            if "connecttest" in low or "ncsi" in low:
                return self._bytes(200, b"Microsoft Connect Test", "text/plain")
            pr = p.rstrip("/") or "/"
            if pr in ("/dashboard.html", "/dashboard") and is_handheld(self.headers.get("User-Agent")):
                return self._bytes(200, DASHBOARD_ELSEWHERE, "text/html; charset=utf-8")
            if pr == "/world":
                return self._world(u)
            if pr == "/telemetry":
                return self._telemetry_get()
            if pr == "/status":
                return self._status()
            if pr == "/frames":
                return self._upstream_get("/frames", u.query)
            if pr == "/frame":
                # The photo a particular exchange was built from.
                return self._upstream_get("/frame", u.query, binary=True, cache_s=86400)
            if pr in ("/trace", "/pipeline-health", "/voices"):
                # Read-only passthrough to the Mac pipeline service.
                return self._upstream_get({"/trace": "/trace", "/voices": "/voices"}
                                          .get(pr, "/health"), u.query)
            if pr == "/say":
                # The spoken line, as audio. Binary, and cacheable: a given
                # line never changes, so the phone keeps it after the first
                # time and the Mac is not asked again.
                return self._upstream_get("/say", u.query, binary=True, cache_s=86400)
            if pr == "/last-frame.jpg":
                # The most recent camera frame the phone POSTed, for the
                # dashboard: what the world was actually generated from.
                return self._file(os.path.join(LOGS, "last_frame.jpg"), "image/jpeg")
            if pr == "/test":
                return self._file(os.path.join(HERE, "controller.html"), "text/html; charset=utf-8")
            if pr == "/game.html":
                return self._file(os.path.join(HERE, "game.html"), "text/html; charset=utf-8")
            if not self.server.tls and pr in REDIRECT_PAGES:
                host = (self.headers.get("Host") or "10.42.0.1").split(":")[0]
                return self._redirect(f"https://{host}:{BRIDGE['https_port']}{self.path}")
            # Static. An unknown extension-less path (a captive-portal "sign in"
            # tap, a typo) lands on the game rather than a 404; a missing asset
            # stays a 404 so a broken reference is visible.
            fs = self.translate_path(p)
            if p != "/" and not os.path.exists(fs) and "." not in os.path.basename(p):
                return self._redirect("/")
        except Exception as e:
            print(f"[web] GET handler error: {type(e).__name__}: {e}", flush=True)
            return self._json(500, {"error": "handler failed"})
        return super().do_GET()

    def do_POST(self):
        try:
            p = urllib.parse.urlparse(self.path).path.rstrip("/")
            raw = self._body()
            if p == "/led":
                return self._led(raw)
            if p == "/world":
                # The Mac pipeline service (jen-zombie/server.py) takes POST
                # /world with a JPEG body; forward it as-is when proxying.
                return self._world(urllib.parse.urlparse(self.path), body=raw)
            if p == "/telemetry":
                try:
                    payload = json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    return self._json(400, {"error": "bad json"})
                with TELEMETRY_LOCK:
                    TELEMETRY["data"] = payload
                    TELEMETRY["at"] = time.time()
                    TELEMETRY["seq"] += 1
                # The reply carries anything the dashboard queued for the headset.
                return self._json(200, {"ok": True, "commands": command_drain()})
            if p == "/command":
                try:
                    cmd = command_parse(json.loads(raw.decode("utf-8", "replace") or "{}"))
                except Exception:
                    return self._json(400, {"error": "bad json"})
                if cmd is None:
                    return self._json(400, {"error": "unknown command"})
                depth = command_push(cmd)
                print(f"[web] command {cmd}", flush=True)
                return self._json(200, {"ok": True, "queued": depth, "cmd": cmd})
            if p == "/probe-result":
                return self._result("probe_result", raw)
            if p.startswith("/result/"):
                slug = p[len("/result/"):]
                if not slug or not all(c.isalnum() or c in "-_" for c in slug):
                    return self._json(400, {"error": "bad result name"})
                return self._result(slug, raw)
            return self._json(404, {"error": "no such endpoint"})
        except Exception as e:
            print(f"[web] POST handler error: {type(e).__name__}: {e}", flush=True)
            return self._json(500, {"error": "handler failed"})

    # ---------------------------------------------------------- handlers --
    def _sse(self):
        if not BRIDGE["sse_serve"]:
            return self._json(503, {"error": "bridge not attached"})
        self.close_connection = True
        # sse_serve writes its own headers and blocks until the client leaves.
        BRIDGE["sse_serve"](self.connection)

    def _ws(self, key):
        if not BRIDGE["ws_handshake"]:
            return self._json(503, {"error": "bridge not attached"})
        self.close_connection = True
        BRIDGE["ws_handshake"](self.connection, key)
        BRIDGE["ws_read_loop"](self.connection)

    def _led(self, raw):
        if not BRIDGE["send_led"]:
            return self._json(503, {"error": "bridge not attached"})
        try:
            m = json.loads(raw.decode("utf-8", "replace") or "{}")
            led = m.get("led")
            if not isinstance(led, list) or len(led) != 3:
                raise ValueError("led must be [g,y,r]")
            g, y, r = (max(0.0, min(1.0, float(v))) for v in led)
        except Exception as e:
            return self._json(400, {"error": f"bad led payload: {e}"})
        written = BRIDGE["send_led"](g, y, r)
        hub = BRIDGE["hub"]
        return self._json(200, {"ok": True, "led": [g, y, r], "written": bool(written),
                                "esp": bool(hub and hub.snapshot().get("connected"))})

    def _world(self, u, body=None):
        q = urllib.parse.parse_qs(u.query or "")

        def qint(name, default=0):
            try:
                return int(q.get(name, [default])[0])
            except Exception:
                return default

        # The Mac service takes POST + a JPEG and answers 405 to a bare GET, so
        # only a request carrying a frame is forwarded; the game's frame-less
        # GET (Stage 3) is served canned without a wasted round trip.
        if body:
            self._keep_frame(body)
        if WORLD_UPSTREAM and body:
            url = WORLD_UPSTREAM + (("?" + u.query) if u.query else "")
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    url, data=body if body else None, method=self.command,
                    headers={"Content-Type": self.headers.get("Content-Type") or "application/octet-stream"}
                    if body else {})
                with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as r:
                    w = json.loads(r.read().decode("utf-8"))
                w.setdefault("source", "upstream")
                w["upstream_ms"] = int((time.time() - t0) * 1000)
                print(f"[web] world <- upstream {w.get('biome')}/{w.get('name')} ({w['upstream_ms']} ms)", flush=True)
                return self._json(200, w)
            except Exception as e:
                print(f"[web] world upstream failed ({type(e).__name__}: {e}); serving canned", flush=True)
        # Canned: identical to zombie-ar/serve.py so the client cannot tell.
        delay = qint("delay", WORLD_DELAY_MS)
        if qint("jitter", 0):
            delay += random.randint(0, qint("jitter", 0))
        if delay > 0:
            time.sleep(min(delay, 30000) / 1000.0)
        if WORLDS is None:
            return self._json(503, {"error": "worlds unavailable"})
        try:
            w = WORLDS.pick_world(seq=qint("seq", 0) or None)
        except Exception as e:
            print(f"[web] pick_world failed: {e}", flush=True)
            return self._json(500, {"error": "world generation failed"})
        w["served_ms"] = delay
        if WORLD_UPSTREAM:
            w["source"] = "canned-fallback" if body else "canned-no-frame"
        print(f"[web] world -> {w['biome']}/{w['name']} (delay {delay} ms)", flush=True)
        return self._json(200, w)

    def _upstream_get(self, path, query="", binary=False, cache_s=0, timeout=6):
        if not UPSTREAM_BASE:
            return self._json(503, {"ok": False, "error": "no pipeline configured",
                                    "hint": "set JZ_WORLD_UPSTREAM on the jzbridge service",
                                    "exchanges": []})
        url = UPSTREAM_BASE + path + (("?" + query) if query else "")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type") or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype if binary else "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cache_s:
                self.send_header("Cache-Control", f"max-age={cache_s}")
            self.end_headers()
            self.wfile.write(body)
        except urllib.error.HTTPError as e:
            # Pass the service's own refusal through verbatim: the game needs
            # to know WHY a line has no audio (no key, budget spent, upstream
            # error) so it can say so rather than just falling silent.
            try:
                body = e.read()
            except Exception:
                body = b'{"error":"upstream error"}'
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            # The Mac being off is a normal state on the floor, not an error
            # worth breaking the dashboard over.
            return self._json(503, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                    "upstream": url, "exchanges": []})

    def _keep_frame(self, body):
        """Atomic write of the last frame so /last-frame.jpg never serves a torn file."""
        try:
            os.makedirs(LOGS, exist_ok=True)
            tmp = os.path.join(LOGS, "last_frame.jpg.tmp")
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, os.path.join(LOGS, "last_frame.jpg"))
        except OSError as e:
            print(f"[web] could not keep frame: {e}", flush=True)

    def _telemetry_get(self):
        with TELEMETRY_LOCK:
            snap = dict(TELEMETRY)
        age = (time.time() - snap["at"]) if snap["at"] else None
        return self._json(200, {"data": snap["data"], "seq": snap["seq"],
                                "ageMs": None if age is None else int(age * 1000),
                                "serverTime": time.time()})

    def _result(self, name, raw):
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            payload = {"unparsed": raw[:4000].decode("utf-8", "replace")}
        os.makedirs(LOGS, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        payload["_received"] = stamp
        payload["_client"] = self.address_string()
        with open(os.path.join(LOGS, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        with open(os.path.join(LOGS, name + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
        return self._json(200, {"ok": True})

    def _status(self):
        hub = BRIDGE["hub"]
        st = hub.snapshot() if hub else {}
        now = time.time()
        with TELEMETRY_LOCK:
            tseq, tat = TELEMETRY["seq"], TELEMETRY["at"]
        return self._json(200, {
            "ok": True,
            "uptime_s": round(now - BRIDGE["started"], 1),
            "tls": bool(self.server.tls), "port": self.server.port,
            "esp": {"connected": bool(st.get("connected")), "seq": st.get("seq"),
                    "peer": getattr(hub, "esp_peer", None),
                    "age_ms": int((now - st["ts"]) * 1000) if st.get("ts") else None,
                    "stale_events": getattr(hub, "stale_events", 0)},
            "state": {k: st.get(k) for k in ("x", "y", "sw", "atk", "sens")},
            "clients": {"sse": len(hub.sse_clients) if hub else 0,
                        "ws": len(hub.ws_clients) if hub else 0},
            "led": {"last": getattr(hub, "led_last", None),
                    "idle_cleared": getattr(hub, "led_idle_cleared", False),
                    "age_ms": int((now - hub.led_at) * 1000) if hub and getattr(hub, "led_at", 0) else None,
                    "requests": getattr(hub, "led_requests", 0),
                    "written": getattr(hub, "led_written", 0)},
            "world": {"upstream": WORLD_UPSTREAM or None, "canned": WORLDS is not None},
            "telemetry": {"seq": tseq, "age_ms": int((now - tat) * 1000) if tat else None},
            "commands": {"queued": len(COMMANDS["items"]), "seq": COMMANDS["seq"]},
            "www": WWW,
        })


def web_server(port, ssl_ctx=None):
    """One listener; called on its own thread per port by bridge.py."""
    try:
        httpd = GameServer((HOST, port), GameHandler)
    except PermissionError:
        print(f"[web] cannot bind :{port} (needs CAP_NET_BIND_SERVICE); skipping", flush=True)
        return
    except OSError as e:
        print(f"[web] cannot bind :{port} ({e}); skipping", flush=True)
        return
    httpd.tls = ssl_ctx is not None
    httpd.port = port
    if ssl_ctx is not None:
        httpd.socket = ssl_ctx.wrap_socket(httpd.socket, server_side=True,
                                           do_handshake_on_connect=False)
    print(f"[web] {'https' if ssl_ctx else 'http'} game+events on {HOST}:{port}", flush=True)
    httpd.serve_forever()

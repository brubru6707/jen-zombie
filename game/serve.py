#!/usr/bin/env python3
"""
HTTPS static server for the AR client.  Same-origin for everything, because an
immersive-ar page must be a secure context and an HTTPS page cannot talk to
ws:// or http:// on a LAN IP.

  python3 serve.py            # https://<lan-ip>:8443/
  python3 serve.py --port N

Also accepts POST /probe-result, which writes logs/probe_result.json, so Stage 0
results come back off the phone without anyone transcribing them by hand.
Nothing here raises into the request path: a handler error is logged and turned
into a 500 rather than killing the server.
"""
import argparse, datetime, json, os, ssl, socket, sys, threading, time, random
import urllib.parse, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WWW = os.path.join(HERE, "www")
sys.path.insert(0, HERE)
try:
    import worlds as WORLDS
except Exception as _e:                      # server must still serve the app
    WORLDS = None
    sys.stderr.write(f"[SRV] worlds module unavailable: {_e}\n")
WORLD_DELAY_MS = 0                           # set by --world-delay, for testing
                                             # the 'not ready at zero' path

# Live telemetry from the headset. Backed by a FILE, not process memory: the
# HTTPS server (LAN) and the HTTP server (adb reverse) are separate processes,
# and the dashboard must show the same data whichever one it is opened from.
TELEMETRY_PATH = os.path.join(HERE, "logs", "telemetry.json")
TELEMETRY_LOCK = threading.Lock()


def telemetry_write(payload):
    """Atomic replace so a reader never sees a half-written file."""
    try:
        os.makedirs(os.path.dirname(TELEMETRY_PATH), exist_ok=True)
        with TELEMETRY_LOCK:
            prev = telemetry_read()
            rec = {"data": payload, "at": time.time(), "seq": (prev.get("seq") or 0) + 1}
            tmp = TELEMETRY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f)
            os.replace(tmp, TELEMETRY_PATH)
        return True
    except Exception as e:
        sys.stderr.write(f"{PREFIX} telemetry write failed: {e}\n")
        return False


# Commands queued by the dashboard for the headset. The headset has no server
# and no socket of its own, so they ride back on the reply to its 5 Hz
# telemetry POST -- no second connection, nothing to reconnect, and it costs
# nothing while the queue is empty. File-backed for the same reason telemetry
# is: the HTTPS and HTTP servers are separate processes.
COMMANDS_PATH = os.path.join(HERE, "logs", "commands.json")
COMMANDS_LOCK = threading.Lock()
COMMAND_KINDS = ("trim", "preset", "calibrate", "resetcal")
COMMAND_TTL_S = 25.0            # a command nobody collected is stale, not pending
COMMAND_MAX = 16


def command_push(cmd):
    """Queue one command. Returns the queue depth, or -1 on failure."""
    try:
        with COMMANDS_LOCK:
            q = _command_load()
            cmd["at"] = time.time()
            cmd["id"] = (q["seq"] + 1)
            q["seq"] = cmd["id"]
            q["items"] = (q["items"] + [cmd])[-COMMAND_MAX:]
            _command_store(q)
            return len(q["items"])
    except Exception as e:
        sys.stderr.write(f"{PREFIX} command push failed: {e}\n")
        return -1


def command_drain():
    """Take every fresh command; drop anything stale. Never raises."""
    try:
        with COMMANDS_LOCK:
            q = _command_load()
            if not q["items"]:
                return []
            now = time.time()
            fresh = [c for c in q["items"] if now - c.get("at", 0) <= COMMAND_TTL_S]
            q["items"] = []
            _command_store(q)
            return fresh
    except Exception:
        return []


def _command_load():
    try:
        with open(COMMANDS_PATH, "r", encoding="utf-8") as f:
            q = json.load(f)
            if isinstance(q, dict) and isinstance(q.get("items"), list):
                q.setdefault("seq", 0)
                return q
    except Exception:
        pass
    return {"items": [], "seq": 0}


def _command_store(q):
    os.makedirs(os.path.dirname(COMMANDS_PATH), exist_ok=True)
    tmp = COMMANDS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f)
    os.replace(tmp, COMMANDS_PATH)


def command_parse(payload):
    """Validate a dashboard command. Returns the command or None."""
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


def telemetry_read():
    try:
        with open(TELEMETRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"data": None, "at": 0.0, "seq": 0}
LOGS = os.path.join(HERE, "logs")
PREFIX = "[SRV]"
# The pipeline service runs on this same Mac. Proxying its read-only endpoints
# means the dashboard behaves identically whether it was opened from here (the
# adb fallback) or from the Pi -- the model conversation was invisible on this
# origin before, which looked like the pipeline being down.
PIPELINE_BASE = os.environ.get("JZ_PIPELINE_BASE", "http://127.0.0.1:8099").rstrip("/")


def lan_ip():
    """Best-effort primary LAN address; never raises."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WWW, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{PREFIX} {self.address_string()} {fmt % args}\n")

    def end_headers(self):
        # No caching: the phone must never serve a stale probe page.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        # Needed if we later use SharedArrayBuffer; harmless now.
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
            sys.stderr.write(f"{PREFIX} failed to write response: {e}\n")

    def _pipeline(self, path, query="", binary=False):
        """Read-only passthrough to the pipeline service on this machine."""
        url = PIPELINE_BASE + path + (("?" + query) if query else "")
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type") or "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype if binary else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        except Exception as e:
            return self._json(503, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                    "upstream": url, "exchanges": []})

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.rstrip("/") in ("/dashboard.html", "/dashboard") \
                    and is_handheld(self.headers.get("User-Agent")):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(DASHBOARD_ELSEWHERE)))
                self.end_headers()
                return self.wfile.write(DASHBOARD_ELSEWHERE)
            if parsed.path.rstrip("/") == "/world":
                return self._world(parsed)
            if parsed.path.rstrip("/") in ("/trace", "/voices", "/frames"):
                return self._pipeline(parsed.path.rstrip("/"), parsed.query)
            if parsed.path.rstrip("/") == "/frame":
                return self._pipeline("/frame", parsed.query, binary=True)
            if parsed.path.rstrip("/") == "/say":
                return self._pipeline("/say", parsed.query, binary=True)
            if parsed.path.rstrip("/") == "/telemetry":
                snap = telemetry_read()
                age = (time.time() - snap["at"]) if snap.get("at") else None
                return self._json(200, {
                    "data": snap.get("data"), "seq": snap.get("seq", 0),
                    "ageMs": None if age is None else int(age * 1000),
                    "serverTime": time.time(),
                })
        except Exception as e:
            sys.stderr.write(f"{PREFIX} GET handler error: {type(e).__name__}: {e}\n")
            return self._json(500, {"error": "handler failed"})
        return super().do_GET()

    def _world(self, parsed):
        """Stage 3: canned worlds. Stage 4 swaps pick_world() for the pipeline."""
        q = urllib.parse.parse_qs(parsed.query or "")
        def qint(name, default=0):
            try:
                return int(q.get(name, [default])[0])
            except Exception:
                return default
        # Simulated think-time, so the client's prefetch/"incoming" behaviour can
        # be tested against something slower than an instant local response.
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
            sys.stderr.write(f"{PREFIX} pick_world failed: {e}\n")
            return self._json(500, {"error": "world generation failed"})
        w["served_ms"] = delay
        sys.stderr.write(f"{PREFIX} world -> {w['biome']}/{w['name']} (delay {delay} ms)\n")
        return self._json(200, w)

    def do_POST(self):
        try:
            path = self.path.rstrip("/")
            if path == "/telemetry":
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace") if n else "{}"
                try:
                    payload = json.loads(raw)
                except Exception:
                    return self._json(400, {"error": "bad json"})
                ok = telemetry_write(payload)
                # The reply carries anything the dashboard queued for the headset.
                return self._json(200 if ok else 500, {"ok": ok, "commands": command_drain()})
            if path == "/command":
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace") if n else "{}"
                try:
                    payload = json.loads(raw)
                except Exception:
                    return self._json(400, {"error": "bad json"})
                cmd = command_parse(payload)
                if cmd is None:
                    return self._json(400, {"error": "unknown command"})
                depth = command_push(cmd)
                return self._json(200 if depth >= 0 else 500,
                                  {"ok": depth >= 0, "queued": depth, "cmd": cmd})
            if path == "/probe-result":
                name = "probe_result"
            elif path.startswith("/result/"):
                slug = path[len("/result/"):]
                if not slug or not all(c.isalnum() or c in "-_" for c in slug):
                    return self._json(400, {"error": "bad result name"})
                name = slug
            else:
                return self._json(404, {"error": "no such endpoint"})
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else "{}"
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"unparsed": raw[:4000]}
            os.makedirs(LOGS, exist_ok=True)
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            payload["_received"] = stamp
            payload["_client"] = self.address_string()
            with open(os.path.join(LOGS, name + ".json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            with open(os.path.join(LOGS, name + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")
            sys.stderr.write(f"{PREFIX} {name} received at {stamp}\n")
            return self._json(200, {"ok": True})
        except Exception as e:
            sys.stderr.write(f"{PREFIX} POST handler error: {type(e).__name__}: {e}\n")
            return self._json(500, {"error": "handler failed"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--world-delay", type=int, default=0,
                    help="ms of fake think-time on /world, to exercise the "
                         "client's prefetch and 'incoming' path")
    ap.add_argument("--http", action="store_true",
                    help="plain HTTP, for use with: adb reverse tcp:8443 tcp:8443 "
                         "-- http://localhost IS a secure context, so WebXR works "
                         "with no certificate at all (USB-tethered only)")
    args = ap.parse_args()

    if args.http:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
        print(f"{PREFIX} PLAIN HTTP on :{args.port} (adb reverse mode)")
        print(f"{PREFIX} run:  adb reverse tcp:{args.port} tcp:{args.port}")
        print(f"{PREFIX} then open on the phone:  http://localhost:{args.port}/")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print(f"\n{PREFIX} stopped")
        finally:
            try:
                httpd.server_close()
            except Exception:
                pass
        return 0

    cert = os.path.join(HERE, "certs", "cert.pem")
    key = os.path.join(HERE, "certs", "key.pem")
    for p in (cert, key):
        if not os.path.isfile(p):
            print(f"missing {p} -- run mkcert first (see README.md)")
            return 2
    if not os.path.isdir(WWW):
        print(f"missing {WWW}")
        return 2

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(cert, key)
    except Exception as e:
        print(f"could not load certificate: {e}")
        return 2

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    ip = lan_ip()
    print(f"{PREFIX} serving {WWW}")
    print(f"{PREFIX} on the phone open:  https://{ip}:{args.port}/")
    print(f"{PREFIX} on this Mac:        https://localhost:{args.port}/")
    print(f"{PREFIX} probe results land in logs/probe_result.json")
    print(f"{PREFIX} Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{PREFIX} stopped")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Controller bridge: ESP32 (raw TCP) <-> phone browser (WebSocket).

Runs on the Raspberry Pi, which is also the WiFi access point, so the whole
demo is self contained: the ESP32 and the phone both join JZCTRL and the Pi
serves everything. No Mac in the loop, no internet, stdlib only (the Pi has no
package access on the venue network).

Three listeners, all on background threads:

  * TCP :3333   the ESP32 dials in and streams "JZ1 ax ay sw atk sens" lines,
                exactly the protocol in controller-test/PROTOCOL.md. The bridge
                parses them, applies the deadzone, and pushes state to browsers.
                It also writes "LED g y r" back to the board.
  * HTTP :8080  serves controller.html and its assets to the phone.
  * WS   :8080  /ws upgrades to a WebSocket. Browsers receive controller state
                as JSON at frame rate and send {led:[g,y,r]} to drive the lamps.

The wire protocol to the board is unchanged. This bridge is the new Mac-side
"game server" seam, rebuilt for the browser instead of pygame.

2026-09-19: the AR game itself is now served from here too (gameweb.py, when
./www exists), so game, /events, /world and /led share ONE origin,
https://10.42.0.1:8443. The legacy single-page server below is the fallback
when that module or directory is missing. Ports are overridable through
JZ_TCP_PORT / JZ_HTTP_PORTS / JZ_HTTPS_PORT for bench testing beside a live
instance.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time

HOST = "0.0.0.0"
TCP_PORT = int(os.environ.get("JZ_TCP_PORT", 3333))      # ESP32 dials here (PROTOCOL.md)
WEB_PORT = int(os.environ.get("JZ_HTTP_PORT", 8080))     # phone browser, plain
HTTP_PORTS = [int(x) for x in os.environ.get("JZ_HTTP_PORTS", f"80,{WEB_PORT}").split(",") if x]
HTTPS_PORT = int(os.environ.get("JZ_HTTPS_PORT", 8443))
# Silence longer than this is a dead link (PROTOCOL.md 3 and 5: the 2 Hz
# heartbeat is the liveness signal; two missed beats is the recommendation,
# four is what we use so a WiFi hiccup does not flicker the game).
STALE_S = float(os.environ.get("JZ_STALE_S", "2.0"))
# The board applies an LED line and HOLDS it until the next one (PROTOCOL.md
# section 4), so a game that goes away -- a reload, a crash, a locked phone,
# a closed tab -- leaves the lamps lit at whatever the last reading was, for
# ever. That looks exactly like a stuck meter. The game sends a keepalive
# every couple of seconds; if nothing has driven the lamps for this long,
# nobody is driving them and they go dark, once.
LED_IDLE_S = float(os.environ.get("JZ_LED_IDLE_S", "5.0"))
# Keeping the power bank awake.
#
# The board's own KEEP burst is a CPU spin worth a few tens of milliamps, and
# the lamp that goes with it is lit ONLY while TCP is down -- so the draw
# collapses at the exact moment the controller connects, which is why a bank
# drops it about thirty seconds in. The radio is the big consumer on an ESP32
# (tens of mA associated and idle, around a hundred while actually receiving),
# so the strongest lever left without touching the firmware is to keep talking
# to it. These writes are deliberately redundant: they repeat the value the
# board already has, so nothing changes on the lamps, but the radio has to stay
# out of modem sleep to receive them.
LED_KEEPALIVE_HZ = float(os.environ.get("JZ_LED_KEEPALIVE_HZ", "25"))
# A floor under every lamp, in case the radio alone is not enough: three LEDs
# through 220 ohm are worth roughly 6 mA each at full. Off by default, because
# lamps that are never dark look like a stuck meter.
LED_FLOOR = max(0.0, min(1.0, float(os.environ.get("JZ_LED_FLOOR", "0"))))
ADC_MAX = 4095
DEADZONE = 0.05          # matches game/inputs.py ADC_DEADZONE

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def axis(rawv):
    v = (rawv / ADC_MAX) * 2.0 - 1.0
    if abs(v) < DEADZONE:
        return 0.0
    return round((v - DEADZONE * (1 if v > 0 else -1)) / (1 - DEADZONE), 3)


class Hub:
    """Shared state between the ESP32 side and the browser side."""
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"x": 0.0, "y": 0.0, "sw": 0, "atk": 0, "sens": 2048,
                      "raw": [2048, 2048], "connected": False, "seq": 0, "ts": 0.0}
        self.esp_sock = None           # current ESP32 connection
        self.ws_clients = set()        # set of WSConn
        self.sse_clients = set()       # set of SSEConn (used by https/self-signed)
        self.pending_led = None        # latest LED command to push to the board
        self.send_lock = threading.Lock()   # serialises writes to the board socket
        self.esp_peer = None
        self.stale_events = 0
        self.led_last = None           # last [g, y, r] requested, for /status
        self.led_at = 0.0
        self.led_requests = 0
        self.led_written = 0
        self.led_idle_cleared = False   # lamps already darkened for want of a driver
        self.keepalive_writes = 0       # redundant writes, purely to keep the radio up

    def set_from_jz1(self, ax, ay, sw, atk, sens):
        with self.lock:
            self.state.update(x=axis(ax), y=axis(ay), sw=int(sw), atk=int(atk),
                              sens=int(sens), raw=[ax, ay], connected=True,
                              seq=self.state["seq"] + 1, ts=time.time())
            return dict(self.state)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def mark_disconnected(self):
        with self.lock:
            self.state["connected"] = False
            return dict(self.state)

    def mark_stale(self):
        """Silence past STALE_S: neutral stick, released buttons (PROTOCOL.md 5:
        "neutral, not last known"). sens is a physical switch and is kept."""
        with self.lock:
            self.state.update(x=0.0, y=0.0, sw=0, atk=0, connected=False)
            self.stale_events += 1
            return dict(self.state)


HUB = Hub()


# ----------------------------------------------------------------- ESP32 side

def esp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, TCP_PORT))
    srv.listen(2)
    print(f"[esp] listening on {HOST}:{TCP_PORT}")
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[esp] controller connected from {addr[0]}")
        # A board that just booted (or just came back) inherits nothing: start
        # it from a known dark state rather than whatever the last game left.
        send_led(0.0, 0.0, 0.0)
        with HUB.lock:
            old = HUB.esp_sock
            HUB.esp_sock = conn
            HUB.esp_peer = addr[0]
        if old:
            try: old.close()
            except OSError: pass
        threading.Thread(target=esp_reader, args=(conn,), daemon=True).start()


def esp_reader(conn):
    buf = b""
    conn.settimeout(STALE_S)
    try:
        while True:
            # push any pending LED command to the board first (the fallback
            # path; send_led writes directly when it can)
            with HUB.lock:
                led = HUB.pending_led
                HUB.pending_led = None
            if led is not None:
                try:
                    with HUB.send_lock:
                        conn.sendall(("LED %.2f %.2f %.2f\n" % tuple(led)).encode())
                    with HUB.lock:
                        HUB.led_written += 1
                except OSError:
                    break
            try:
                data = conn.recv(1024)
            except socket.timeout:
                # No line for STALE_S: out of range, asleep, or a half-open
                # socket after a WiFi drop. Report neutral input so a stick
                # that was pushed when the link died is not acted on. The
                # socket stays open: the board redials on its own and the
                # newest connection wins.
                with HUB.lock:
                    was = HUB.state["connected"]
                if was:
                    broadcast(HUB.mark_stale())
                    print(f"[esp] controller silent for {STALE_S:.1f}s -> stale")
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                txt = line.decode("ascii", "ignore").strip()
                if txt.startswith("JZ1"):
                    p = txt.split()
                    if len(p) >= 6:
                        try:
                            with HUB.lock:
                                was = HUB.state["connected"]
                            st = HUB.set_from_jz1(*(int(x) for x in p[1:6]))
                            if not was:
                                print("[esp] controller streaming")
                            broadcast(st)
                        except ValueError:
                            pass
    finally:
        try: conn.close()
        except OSError: pass
        with HUB.lock:
            if HUB.esp_sock is conn:
                HUB.esp_sock = None
        broadcast(HUB.mark_disconnected())
        print("[esp] controller disconnected")


def _write_led(sock, led):
    """One LED line onto the board's socket. Returns True if it went out."""
    if sock is None:
        return False
    try:
        with HUB.send_lock:
            sock.sendall(("LED %.2f %.2f %.2f\n" % tuple(led)).encode())
        return True
    except OSError:
        return False


def radio_keepalive():
    """
    Re-send the board's CURRENT lamp value over and over. It changes nothing on
    the hardware and is invisible to the game, but the radio cannot sleep
    through it, which is where the current actually goes. Deliberately does not
    touch led_at or led_last, so the idle watchdog still measures real traffic.
    """
    if LED_KEEPALIVE_HZ <= 0:
        print("[esp] radio keepalive disabled")
        return
    gap = 1.0 / LED_KEEPALIVE_HZ
    print(f"[esp] radio keepalive at {LED_KEEPALIVE_HZ:g} Hz"
          + (f", lamp floor {LED_FLOOR:.2f}" if LED_FLOOR > 0 else ""))
    while True:
        time.sleep(gap)
        try:
            with HUB.lock:
                sock = HUB.esp_sock
                last = HUB.led_last or [0.0, 0.0, 0.0]
            if sock is None:
                continue
            led = tuple(max(LED_FLOOR, v) for v in last)
            if _write_led(sock, led):
                with HUB.lock:
                    HUB.keepalive_writes += 1
        except Exception as e:
            print(f"[esp] radio keepalive: {e}")


def led_watchdog():
    """Darken the lamps when nothing is driving them. One line, not a stream."""
    while True:
        time.sleep(1.0)
        try:
            with HUB.lock:
                last = HUB.led_last
                at = HUB.led_at
                cleared = HUB.led_idle_cleared
                connected = HUB.state["connected"]
            if not connected or cleared or not at:
                continue
            if time.time() - at < LED_IDLE_S:
                continue
            if last and max(last) <= 0.0:
                with HUB.lock:
                    HUB.led_idle_cleared = True     # already dark, nothing to do
                continue
            print(f"[esp] nothing has driven the lamps for {LED_IDLE_S:g}s -> off")
            send_led(0.0, 0.0, 0.0)
            with HUB.lock:
                HUB.led_idle_cleared = True
        except Exception as e:
            print(f"[esp] led watchdog: {e}")


def send_led(g, y, r):
    """Write a LED line to the board NOW if it is connected, so the meter does
    not wait up to a heartbeat (500 ms) for the reader loop to wake; otherwise
    leave it queued for the reader loop (the original path). Returns True when
    the line went out. Latest value always wins."""
    led = (max(0.0, min(1.0, g)), max(0.0, min(1.0, y)), max(0.0, min(1.0, r)))
    with HUB.lock:
        HUB.pending_led = led
        HUB.led_last = list(led)
        HUB.led_at = time.time()
        HUB.led_requests += 1
        HUB.led_idle_cleared = False    # somebody is driving them again
        sock = HUB.esp_sock
    if sock is None:
        return False
    try:
        with HUB.send_lock:
            sock.sendall(("LED %.2f %.2f %.2f\n" % led).encode())
    except OSError:
        return False                    # the reader loop notices the dead socket
    with HUB.lock:
        if HUB.pending_led == led:
            HUB.pending_led = None
        HUB.led_written += 1
    return True


# ------------------------------------------------------------- WebSocket side

class WSConn:
    def __init__(self, sock):
        self.sock = sock
        self.alive = True

    def send_text(self, text):
        data = text.encode("utf-8")
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 127, n)
        try:
            self.sock.sendall(hdr + data)
        except OSError:
            self.alive = False

    def close(self):
        self.alive = False
        try: self.sock.close()
        except OSError: pass


def broadcast(state):
    msg = json.dumps({"t": "state", **state})
    dead = []
    with HUB.lock:
        clients = list(HUB.ws_clients)
        sse = list(HUB.sse_clients)
    for c in clients:
        c.send_text(msg)
        if not c.alive:
            dead.append(c)
    for c in sse:
        c.send_event(msg)
        if not c.alive:
            dead.append(c)
    if dead:
        with HUB.lock:
            for c in dead:
                HUB.ws_clients.discard(c)
                HUB.sse_clients.discard(c)


class SSEConn:
    """One Server-Sent Events client. Text frames as `data: <json>\\n\\n`."""
    def __init__(self, sock):
        self.sock = sock
        self.alive = True
        self._lock = threading.Lock()

    def send_event(self, text):
        with self._lock:
            try:
                self.sock.sendall(b"data: " + text.encode("utf-8") + b"\n\n")
            except OSError:
                self.alive = False

    def close(self):
        self.alive = False
        try: self.sock.close()
        except OSError: pass


def sse_serve(sock):
    hdr = ("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
           "Cache-Control: no-cache\r\nConnection: keep-alive\r\n"
           "X-Accel-Buffering: no\r\n\r\n")
    try:
        sock.sendall(hdr.encode())
    except OSError:
        return
    conn = SSEConn(sock)
    with HUB.lock:
        HUB.sse_clients.add(conn)
    conn.send_event(json.dumps({"t": "state", **HUB.snapshot()}))
    # keep the connection open; heartbeat comments detect a dropped client
    try:
        while conn.alive:
            time.sleep(10)
            with conn._lock:
                try:
                    sock.sendall(b": ping\n\n")
                except OSError:
                    conn.alive = False
    finally:
        with HUB.lock:
            HUB.sse_clients.discard(conn)
        conn.close()


def ws_handshake(sock, key):
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    resp = ("HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n")
    sock.sendall(resp.encode())


def ws_read_loop(sock):
    conn = WSConn(sock)
    with HUB.lock:
        HUB.ws_clients.add(conn)
    conn.send_text(json.dumps({"t": "state", **HUB.snapshot()}))
    try:
        sock.settimeout(60)
        while conn.alive:
            hdr = recv_exact(sock, 2)
            if not hdr:
                break
            b1, b2 = hdr[0], hdr[1]
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", recv_exact(sock, 2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", recv_exact(sock, 8))[0]
            mask = recv_exact(sock, 4) if masked else b"\x00\x00\x00\x00"
            payload = recv_exact(sock, ln) if ln else b""
            if opcode == 0x8:      # close
                break
            data = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
            if opcode == 0x1:      # text
                handle_ws_msg(data.decode("utf-8", "ignore"))
    except (OSError, socket.timeout):
        pass
    finally:
        with HUB.lock:
            HUB.ws_clients.discard(conn)
        conn.close()


def handle_ws_msg(text):
    try:
        m = json.loads(text)
    except ValueError:
        return
    if "led" in m and isinstance(m["led"], list) and len(m["led"]) == 3:
        send_led(*(float(x) for x in m["led"]))


def recv_exact(sock, n):
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            return None
        out += chunk
    return out


# --------------------------------------------------------------- HTTP + WS mux

def web_server(page_path, port, ssl_ctx=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, port))
    except PermissionError:
        print(f"[web] cannot bind :{port} (needs CAP_NET_BIND_SERVICE); skipping")
        return
    srv.listen(8)
    print(f"[web] {'https' if ssl_ctx else 'http'}+ws on {HOST}:{port}")
    while True:
        raw, _ = srv.accept()
        conn = raw
        if ssl_ctx is not None:
            try:
                conn = ssl_ctx.wrap_socket(raw, server_side=True)
            except Exception:
                try: raw.close()
                except OSError: pass
                continue
        # re-read the page each connection so edits show up without a restart
        try:
            with open(page_path, "rb") as f:
                page = f.read()
        except OSError:
            page = b"<h1>page missing</h1>"
        threading.Thread(target=handle_http, args=(conn, page), daemon=True).start()


def handle_http(conn, page):
    try:
        conn.settimeout(5)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(1024)
            if not chunk:
                conn.close(); return
            req += chunk
        head = req.decode("latin1")
        line0 = head.split("\r\n", 1)[0]
        path = line0.split(" ")[1] if " " in line0 else "/"
        # WebSocket upgrade?
        key = None
        for h in head.split("\r\n"):
            if h.lower().startswith("sec-websocket-key:"):
                key = h.split(":", 1)[1].strip()
        if path.startswith("/ws") and key:
            ws_handshake(conn, key)
            ws_read_loop(conn)
            return
        if path.startswith("/events"):
            sse_serve(conn)
            return
        # Android / Samsung "do I have internet?" probes. Answering them the way
        # the OS expects (204 for generate_204, or a 200 with the exact body it
        # wants) makes the phone treat JZCTRL as a validated network and STOP
        # auto-abandoning it for having no upstream internet. Without this the
        # phone bounces off the AP within a minute, which kills the demo.
        low = path.lower()
        if "generate_204" in low or "gen_204" in low or "/generate204" in low:
            conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        elif "connecttest" in low or "ncsi" in low:      # Windows, harmless to keep
            body = b"Microsoft Connect Test"
            conn.sendall(("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                          f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        elif path.startswith("/test"):
            # the plain controller visualizer, handy for debugging input
            import os as _os
            tp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "controller.html")
            try:
                with open(tp, "rb") as _f:
                    body = _f.read()
            except OSError:
                body = page
            conn.sendall(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                          f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        elif path == "/" or path.startswith("/index") or path.startswith("/?"):
            body = page
            hdr = ("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                   f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
            conn.sendall(hdr.encode() + body)
        else:
            # Any other path: serve the controller page too, so a captive-portal
            # "sign in" tap lands somewhere useful instead of a 404.
            body = page
            conn.sendall(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                          f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
    except (OSError, socket.timeout):
        pass
    finally:
        try: conn.close()
        except OSError: pass


if __name__ == "__main__":
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    # game.html is the main experience; controller.html stays available at /test
    page = os.path.join(here, "game.html")
    if not os.path.exists(page):
        page = os.path.join(here, "controller.html")
    threading.Thread(target=esp_server, daemon=True).start()
    threading.Thread(target=led_watchdog, daemon=True).start()
    threading.Thread(target=radio_keepalive, daemon=True).start()
    import ssl
    cert = os.path.join(here, "cert.pem")
    keyf = os.path.join(here, "key.pem")
    sctx = None
    if os.path.exists(cert) and os.path.exists(keyf):
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(cert, keyf)
    # The AR game (zombie-ar/www mirrored into ./www) is served by gameweb.py
    # on every web port, same origin as /events and /led. If that module or the
    # www directory is missing, the legacy single-page server above still runs,
    # so the service always comes up with the controller endpoints intact.
    www = os.path.join(here, "www")
    gameweb = None
    if os.path.isdir(www):
        try:
            import gameweb as _gw
            _gw.install(hub=HUB, send_led=send_led, sse_serve=sse_serve,
                        ws_handshake=ws_handshake, ws_read_loop=ws_read_loop,
                        https_port=HTTPS_PORT)
            gameweb = _gw
            print(f"[web] serving the AR game from {www}")
        except Exception as e:
            print(f"[web] gameweb unavailable ({type(e).__name__}: {e}); legacy pages only")
    else:
        print(f"[web] no {www}; legacy pages only")
    if gameweb is not None:
        serve = lambda port, ctx=None: gameweb.web_server(port, ssl_ctx=ctx)
    else:
        serve = lambda port, ctx=None: web_server(page, port, ssl_ctx=ctx)
    # Port 80 answers the phone's captive-portal / connectivity probe so it stays
    # on the AP; 8080 is the plain page. AR needs the camera, and getUserMedia
    # only works in a secure context, so the real game is served over HTTPS on
    # 8443 -- with the mkcert certificate the phone already trusts, so there is
    # no warning to click through (a click-through exception makes WebXR and
    # getUserMedia unpredictable).
    for p in HTTP_PORTS:
        threading.Thread(target=serve, args=(p,), daemon=True).start()
    if sctx is not None:
        print(f"[web] open https://<pi-ip>:{HTTPS_PORT}/")
        serve(HTTPS_PORT, sctx)
    else:
        print("[web] no cert.pem/key.pem -> HTTPS off (camera/AR needs it); http only")
        while True:
            time.sleep(3600)

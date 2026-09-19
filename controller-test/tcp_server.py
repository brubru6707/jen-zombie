"""
Throwaway test server for the jenzombies controller, protocol v2.

This exists only to prove the firmware works. It is NOT the game's server and
must not grow into one. See PROTOCOL.md for the contract the real Mac side
implements.

  python3 tcp_server.py serve   <seconds>
  python3 tcp_server.py latency <samples>
  python3 tcp_server.py drops   <seconds>

Every mode has a wall clock deadline and closes its sockets in a finally block.
"""
import socket, statistics as st, sys, threading, time

HOST, PORT = "0.0.0.0", 3333


class Server:
    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((HOST, PORT))
        self.srv.listen(4)
        self.srv.settimeout(0.3)
        self.conn = None
        self.peer = None
        self.buf = b""
        self.lines = []
        self.events = []          # (t, "connect"/"disconnect", peer)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.t0 = time.time()
        self.acceptor = threading.Thread(target=self._accept_loop, daemon=True)
        self.acceptor.start()

    def _accept_loop(self):
        while not self.stop.is_set():
            try:
                c, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.settimeout(0.2)
            with self.lock:
                # Newest wins. A board that lost its link without us noticing
                # will redial while the stale socket still looks open, and
                # refusing it would strand the controller.
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except OSError:
                        pass
                    self.events.append((time.time() - self.t0, "replaced", self.peer))
                self.conn = c
                self.peer = f"{addr[0]}:{addr[1]}"
                self.buf = b""
                self.events.append((time.time() - self.t0, "connect", self.peer))

    def pump(self):
        """Read whatever is available, split on newline, return complete lines."""
        with self.lock:
            c = self.conn
        if c is None:
            return []
        try:
            data = c.recv(4096)
        except socket.timeout:
            return []
        except OSError:
            data = b""
        if data == b"":
            with self.lock:
                if self.conn is c:
                    self.events.append((time.time() - self.t0, "disconnect", self.peer))
                    try:
                        c.close()
                    except OSError:
                        pass
                    self.conn = None
            return []
        out = []
        with self.lock:
            self.buf += data
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                txt = raw.decode("ascii", "ignore").strip()
                if txt:
                    out.append(txt)
                    self.lines.append(txt)
        return out

    def send(self, text):
        with self.lock:
            c = self.conn
        if c is None:
            return False
        try:
            c.sendall(text.encode("ascii"))
            return True
        except OSError:
            return False

    @property
    def connected(self):
        with self.lock:
            return self.conn is not None

    def close(self):
        self.stop.set()
        with self.lock:
            for s in (self.conn, self.srv):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
            self.conn = None


def mode_serve(srv, secs):
    print(f"listening on {HOST}:{PORT} for {secs:.0f} s")
    deadline = time.time() + secs
    first_frame_at = None
    jz1 = 0
    shown = 0
    while time.time() < deadline:
        for ln in srv.pump():
            if ln.startswith("JZ1"):
                jz1 += 1
                if first_frame_at is None:
                    first_frame_at = time.time() - srv.t0
                    print(f"[{first_frame_at:6.2f}s] FIRST FRAME: {ln}")
                elif shown < 6:
                    shown += 1
                    print(f"[{time.time()-srv.t0:6.2f}s] {ln}")
        time.sleep(0.01)
    el = time.time() - srv.t0
    print(f"\nJZ1 frames: {jz1} in {el:.1f} s  ({jz1/el:.1f}/s)")
    print(f"first frame at: {first_frame_at:.2f} s" if first_frame_at else "NO FRAMES")
    for t, kind, peer in srv.events:
        print(f"  event {t:6.2f}s  {kind}  {peer}")


def mode_latency(srv, samples):
    print("waiting for the board to connect (max 60 s)")
    t_wait = time.time() + 60
    while not srv.connected and time.time() < t_wait:
        srv.pump()
        time.sleep(0.02)
    if not srv.connected:
        print("board never connected")
        return
    print(f"connected: {srv.peer}. measuring {samples} RAW round trips over TCP")
    rtt = []
    for i in range(samples):
        val = i % 2
        t_send = time.time()
        if not srv.send(f"RAW 19 {val}\n"):
            print("send failed, link gone")
            break
        deadline = t_send + 2.0
        got = False
        while time.time() < deadline and not got:
            for ln in srv.pump():
                if ln.startswith("RAW pin 19"):
                    rtt.append((time.time() - t_send) * 1000.0)
                    got = True
                    break
            if not got:
                time.sleep(0.001)
        time.sleep(0.05)
    srv.send("LED 0.00 0.00 0.00\n")
    if not rtt:
        print("no acknowledgements received")
        return
    rtt.sort()
    p90 = rtt[max(0, int(len(rtt) * 0.9) - 1)]
    print(f"\nn={len(rtt)}  median={st.median(rtt):.1f} ms  p90={p90:.1f} ms  "
          f"worst={max(rtt):.1f} ms  best={min(rtt):.1f} ms")
    print("(USB baseline for the same measurement was median 3.6 ms, worst 7.0 ms)")


def mode_drops(srv, secs):
    print(f"watching link for {secs:.0f} s. Walk out of range and back.")
    deadline = time.time() + secs
    last_frame = None
    gaps = []
    prev_conn = False
    while time.time() < deadline:
        for ln in srv.pump():
            if ln.startswith("JZ1"):
                now = time.time()
                if last_frame is not None and now - last_frame > 1.5:
                    gaps.append((last_frame - srv.t0, now - srv.t0, now - last_frame))
                    print(f"[{now-srv.t0:6.1f}s] GAP of {now-last_frame:.1f} s ended, frames resumed")
                last_frame = now
        c = srv.connected
        if c != prev_conn:
            prev_conn = c
            print(f"[{time.time()-srv.t0:6.1f}s] socket {'UP' if c else 'DOWN'}")
        time.sleep(0.01)
    print("\n===== EVENTS =====")
    for t, kind, peer in srv.events:
        print(f"  {t:6.1f}s  {kind}  {peer}")
    print("\n===== OUTAGES =====")
    if not gaps:
        print("  no frame gap longer than 1.5 s observed")
    for a, b, d in gaps:
        print(f"  frames stopped {a:.1f}s, resumed {b:.1f}s, outage {d:.1f} s")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    arg = float(sys.argv[2]) if len(sys.argv) > 2 else 30
    srv = None
    try:
        srv = Server()
        {"serve": mode_serve, "latency": mode_latency, "drops": mode_drops}[mode](
            srv, int(arg) if mode == "latency" else arg)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if srv is not None:
            srv.close()
        print("sockets closed")

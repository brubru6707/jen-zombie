"""Step 7 measured: does the game's own source recover from a real drop?"""
import sys, time
sys.path.insert(0, "/Users/brrodrig/documents/fuq_windows/bat_cave/jenzombies")
from game.spp import SPPControllerSource

src = SPPControllerSource()
t0 = time.time()
log = []
prev = None
try:
    while time.time() - t0 < 75.0:
        now = time.time() - t0
        c = src.connected
        if c != prev:
            log.append((now, c, src.port_path, src.reconnects, src.last_error))
            print(f"[{now:5.1f}s] connected={c}  port={src.port_path} "
                  f"reconnects={src.reconnects} err={src.last_error}", flush=True)
            prev = c
        time.sleep(0.05)
finally:
    src.close()

print("\n===== TIMELINE =====")
for t, c, p, r, e in log:
    print(f"  {t:5.1f}s  connected={str(c):5s}  reconnects={r}  {e or ''}")

drops = [i for i in range(1, len(log)) if log[i][1] is False]
print("\n===== VERDICT =====")
if not drops:
    print("  no drop observed: either the cable was not pulled, or the link never broke")
else:
    d = drops[0]
    print(f"  link dropped at {log[d][0]:.1f}s")
    rec = [l for l in log[d+1:] if l[1] is True]
    if rec:
        print(f"  RECOVERED ON ITS OWN at {rec[0][0]:.1f}s")
        print(f"  outage duration: {rec[0][0] - log[d][0]:.1f}s")
        print(f"  no manual reconnect was needed")
    else:
        print(f"  DID NOT RECOVER within the test window")
        print(f"  final status: {src.status()}")

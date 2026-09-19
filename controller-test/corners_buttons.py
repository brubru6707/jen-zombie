"""Steps 3b + 4: diagonal reach, then every button the protocol exposes."""
import re, sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

RAWRE = re.compile(rb"RAW x=\s*(\d+) y=\s*(\d+) \(centre\s*(\d+)/\s*(\d+)\) -> ax=\s*(-?\d+) ay=\s*(-?\d+) sw=(\d) atk=(\d) sens=(\d+) bt=(\d)")
PHASES = [("corners", 0, 22), ("joystick switch (sw)", 22, 32),
          ("attack button (atk)", 32, 42), ("sens toggle", 42, 52)]

ser = None
rows = []
try:
    ser, _ = link.open_port("/dev/cu.usbserial-0001", 5.0)
    ser.reset_input_buffer()
    t0 = time.time()
    while time.time() - t0 < 52.0:
        ln = ser.readline()
        if not ln:
            continue
        m = RAWRE.search(ln)
        if not m:
            continue
        g = [int(v) for v in m.groups()]
        rows.append((time.time() - t0, g[4], g[5], g[6], g[7], g[8]))
finally:
    if ser is not None:
        link.close(ser)

print(f"samples: {len(rows)}\n")

# ---- diagonal reach across the whole run
print("DIAGONAL REACH  (how far both axes get AT THE SAME TIME)")
print("  a perfectly square gate gives 1.00; a circular gate caps near 0.71")
quads = {"up-right": (1, 1), "up-left": (-1, 1), "down-right": (1, -1), "down-left": (-1, -1)}
for name, (sx, sy) in quads.items():
    best = 0.0
    for _, ax, ay, *_ in rows:
        dx = (ax - 2048) / 2048.0
        dy = (ay - 2048) / 2048.0
        if dx * sx > 0 and dy * sy > 0:
            best = max(best, min(abs(dx), abs(dy)))
    print(f"  {name:11s} best simultaneous deflection = {best:.2f}")
print()

# ---- per phase button reporting
for name, a, b in PHASES:
    seg = [r for r in rows if a <= r[0] < b]
    if not seg:
        print(f"{name}: no samples")
        continue
    sw = set(r[3] for r in seg)
    atk = set(r[4] for r in seg)
    sens = sorted(set(r[5] for r in seg))
    # count transitions
    def trans(idx):
        n = 0
        for p, q in zip(seg, seg[1:]):
            if p[idx] != q[idx]:
                n += 1
        return n
    print(f"{name}  ({len(seg)} samples)")
    print(f"   sw   values={sorted(sw)}  transitions={trans(3)}")
    print(f"   atk  values={sorted(atk)}  transitions={trans(4)}")
    print(f"   sens values={sens}  transitions={trans(5)}")

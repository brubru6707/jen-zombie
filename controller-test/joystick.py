"""Step 3: live joystick range capture."""
import re, statistics as st, sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
RAWRE = re.compile(rb"RAW x=\s*(\d+) y=\s*(\d+) \(centre\s*(\d+)/\s*(\d+)\) -> ax=\s*(-?\d+) ay=\s*(-?\d+) sw=(\d) atk=(\d) sens=(\d+) bt=(\d)")
DEADZONE = 0.05   # game/inputs.py ADC_DEADZONE
ADC_MAX = 4095

def to_axis(rawv):
    v = (rawv / ADC_MAX) * 2.0 - 1.0
    if abs(v) < DEADZONE:
        return 0.0
    return (v - DEADZONE * (1 if v > 0 else -1)) / (1 - DEADZONE)

ser = None
rows = []
try:
    ser, _ = link.open_port("/dev/cu.usbserial-0001", 5.0)
    ser.reset_input_buffer()
    t0 = time.time()
    while time.time() - t0 < DUR:
        ln = ser.readline()
        if not ln:
            continue
        m = RAWRE.search(ln)
        if not m:
            continue
        g = [int(v) for v in m.groups()]
        rows.append((time.time() - t0, g[0], g[1], g[4], g[5], g[6], g[7], g[8]))
finally:
    if ser is not None:
        link.close(ser)

if not rows:
    print("NO DATA")
    raise SystemExit(1)

rx = [r[1] for r in rows]; ry = [r[2] for r in rows]
nx = [r[3] for r in rows]; ny = [r[4] for r in rows]
print(f"samples: {len(rows)} over {rows[-1][0]:.1f} s")
print()
print("RAW ADC (what the stick actually produces)")
print(f"  X  min={min(rx):5d}  max={max(rx):5d}  span={max(rx)-min(rx):5d}")
print(f"  Y  min={min(ry):5d}  max={max(ry):5d}  span={max(ry)-min(ry):5d}")
print()
print("NORMALISED ax/ay (0..4095, what goes on the wire)")
print(f"  ax min={min(nx):5d}  max={max(nx):5d}")
print(f"  ay min={min(ny):5d}  max={max(ny):5d}")
print()
print("GAME AXIS -1..1 after ADC_DEADZONE=0.05")
gx = [to_axis(v) for v in nx]; gy = [to_axis(v) for v in ny]
print(f"  x  min={min(gx):+.3f}  max={max(gx):+.3f}")
print(f"  y  min={min(gy):+.3f}  max={max(gy):+.3f}")
print()
# corners: count samples where both axes are well off centre
corners = {"up-left":0, "up-right":0, "down-left":0, "down-right":0}
for a, b in zip(nx, ny):
    if a < 1000 and b < 1000: corners["down-left"] += 1
    if a > 3095 and b < 1000: corners["down-right"] += 1
    if a < 1000 and b > 3095: corners["up-left"] += 1
    if a > 3095 and b > 3095: corners["up-right"] += 1
print("CORNER COVERAGE (samples with both axes past 75% travel)")
for k, v in corners.items():
    print(f"  {k:11s} {v:4d}  {'reached' if v else 'NOT REACHED'}")
print()
# resting segments: find stretches where movement is tiny
rest = [(a, b) for a, b in zip(rx, ry) if abs(a - st.median(rx)) < 40 and abs(b - st.median(ry)) < 40]
print(f"near-centre samples: {len(rest)}")
# deadzone in raw counts
dz_counts = DEADZONE * 2048
print(f"deadzone: {DEADZONE} of full scale = +/-{dz_counts:.0f} ADC counts around 2048")
print(f"resting jitter this run: X={max(rx[:20])-min(rx[:20]) if len(rx)>20 else 0} "
      f"Y={max(ry[:20])-min(ry[:20]) if len(ry)>20 else 0} (first 20 samples)")

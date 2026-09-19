"""Steps 3 + 4 in one guided session: range, centre, drift, diagonals, buttons."""
import re, statistics as st, sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

RAWRE = re.compile(rb"RAW x=\s*(\d+) y=\s*(\d+) \(centre\s*(\d+)/\s*(\d+)\) -> ax=\s*(-?\d+) ay=\s*(-?\d+) sw=(\d) atk=(\d) sens=(\d+) bt=(\d)")
LEAD = 6.0
PHASES = [("sweep L-R then D-U", 0, 16), ("four corners", 16, 32),
          ("joystick switch", 32, 41), ("attack button", 41, 50),
          ("sens toggle", 50, 59), ("rest, hands off", 59, 67)]
DEADZONE, ADC_MAX = 0.05, 4095

def to_axis(v):
    a = (v / ADC_MAX) * 2.0 - 1.0
    return 0.0 if abs(a) < DEADZONE else (a - DEADZONE * (1 if a > 0 else -1)) / (1 - DEADZONE)

ser = None
rows = []
try:
    ser, _ = link.open_port("/dev/cu.usbserial-0001", 5.0)
    time.sleep(LEAD)
    ser.reset_input_buffer()
    t0 = time.time()
    while time.time() - t0 < 67.0:
        ln = ser.readline()
        if not ln:
            continue
        m = RAWRE.search(ln)
        if m:
            g = [int(v) for v in m.groups()]
            rows.append((time.time() - t0, g[0], g[1], g[4], g[5], g[6], g[7], g[8]))
finally:
    if ser is not None:
        link.close(ser)

if not rows:
    print("NO DATA"); raise SystemExit(1)
rx=[r[1] for r in rows]; ry=[r[2] for r in rows]
nx=[r[3] for r in rows]; ny=[r[4] for r in rows]
print(f"samples: {len(rows)} over {rows[-1][0]:.1f} s\n")
print("RANGE")
print(f"  raw X {min(rx):5d} .. {max(rx):5d}      raw Y {min(ry):5d} .. {max(ry):5d}")
print(f"  wire ax {min(nx):5d} .. {max(nx):5d}    wire ay {min(ny):5d} .. {max(ny):5d}")
gx=[to_axis(v) for v in nx]; gy=[to_axis(v) for v in ny]
print(f"  game x {min(gx):+.3f} .. {max(gx):+.3f}   game y {min(gy):+.3f} .. {max(gy):+.3f}\n")
print("DIAGONAL REACH (both axes at once; square gate=1.00, circular gate~0.71)")
for name,(sx,sy) in {"up-right":(1,1),"up-left":(-1,1),"down-right":(1,-1),"down-left":(-1,-1)}.items():
    best=0.0
    for _,_,_,ax,ay,*_ in rows:
        dx=(ax-2048)/2048.0; dy=(ay-2048)/2048.0
        if dx*sx>0 and dy*sy>0: best=max(best,min(abs(dx),abs(dy)))
    print(f"  {name:11s} {best:.2f}")
print()
for name,a,b in PHASES:
    seg=[r for r in rows if a<=r[0]<b]
    if not seg:
        print(f"{name:22s} no samples"); continue
    def tr(i):
        return sum(1 for p,q in zip(seg,seg[1:]) if p[i]!=q[i])
    print(f"{name:22s} n={len(seg):3d}  sw={sorted(set(r[5] for r in seg))} x{tr(5)}  "
          f"atk={sorted(set(r[6] for r in seg))} x{tr(6)}  sens={sorted(set(r[7] for r in seg))} x{tr(7)}")
print()
rest=[r for r in rows if r[0]>=59]
if len(rest)>10:
    rrx=[r[1] for r in rest]; rry=[r[2] for r in rest]
    print("RESTING CENTRE (final hands-off window)")
    print(f"  X mean={st.mean(rrx):.1f} min={min(rrx)} max={max(rrx)} jitter={max(rrx)-min(rrx)}")
    print(f"  Y mean={st.mean(rry):.1f} min={min(rry)} max={max(rry)} jitter={max(rry)-min(rry)}")
    print(f"  as game axis: x={to_axis(int(st.mean([r[3] for r in rest]))):+.3f} "
          f"y={to_axis(int(st.mean([r[4] for r in rest]))):+.3f}  (0.000 = no creep)")
print(f"\ndeadzone {DEADZONE} = +/-{DEADZONE*2048:.0f} ADC counts around 2048")

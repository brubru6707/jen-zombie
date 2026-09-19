"""Step 4, timing free: log every input transition as an event."""
import re, sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

RAWRE = re.compile(rb"-> ax=\s*(-?\d+) ay=\s*(-?\d+) sw=(\d) atk=(\d) sens=(\d+)")
ser = None
events = []
try:
    ser, _ = link.open_port("/dev/cu.usbserial-0001", 5.0)
    time.sleep(6.0)
    ser.reset_input_buffer()
    t0 = time.time()
    prev = None
    while time.time() - t0 < 60.0:
        ln = ser.readline()
        if not ln:
            continue
        m = RAWRE.search(ln)
        if not m:
            continue
        sw, atk, sens = int(m.group(3)), int(m.group(4)), int(m.group(5))
        cur = (sw, atk, sens)
        if prev is not None and cur != prev:
            changed = []
            if cur[0] != prev[0]: changed.append(f"sw {prev[0]}->{cur[0]}")
            if cur[1] != prev[1]: changed.append(f"atk {prev[1]}->{cur[1]}")
            if cur[2] != prev[2]: changed.append(f"sens {prev[2]}->{cur[2]}")
            events.append((time.time() - t0, changed, cur))
        prev = cur
finally:
    if ser is not None:
        link.close(ser)

print(f"{len(events)} transitions in 60 s\n")
print(f"{'t(s)':>6}  {'what changed':<34} {'state after (sw,atk,sens)'}")
for t, ch, cur in events:
    print(f"{t:6.1f}  {', '.join(ch):<34} {cur}")

print("\nCOUPLING CHECK")
multi = [e for e in events if len(e[1]) > 1]
print(f"  transitions where more than one input moved in the same sample: {len(multi)}")
for t, ch, _ in multi:
    print(f"    t={t:.1f}  {', '.join(ch)}")
if not multi:
    print("  none: each input moves independently, no electrical coupling")
# per input tally
for i, name in enumerate(("sw", "atk", "sens")):
    n = sum(1 for _, ch, _ in events if any(c.startswith(name + " ") for c in ch))
    print(f"  {name:5s} transitions: {n}  ({'REGISTERS' if n else 'NEVER MOVED'})")

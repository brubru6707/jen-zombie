"""Resting baseline + drift, and two latency measures. Non-interactive."""
import re, statistics as st, sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

PATH = "/dev/cu.usbserial-0001"
RAWRE = re.compile(rb"RAW x=\s*(\d+) y=\s*(\d+) \(centre\s*(\d+)/\s*(\d+)\) -> ax=\s*(-?\d+) ay=\s*(-?\d+) sw=(\d) atk=(\d) sens=(\d+) bt=(\d)")

ser = None
try:
    ser, _ = link.open_port(PATH, open_timeout=5.0)

    # ---- 1. resting baseline, 12 s
    print("== RESTING BASELINE (12 s, do not touch the stick) ==")
    t0 = time.time()
    xs, ys, axs, ays, bts = [], [], [], [], []
    jz1_t = []
    while time.time() - t0 < 12.0:
        ln = ser.readline()
        if not ln:
            continue
        if ln.startswith(b"JZ1"):
            jz1_t.append(time.time())
            continue
        m = RAWRE.search(ln)
        if m:
            g = [int(v) for v in m.groups()]
            xs.append(g[0]); ys.append(g[1]); axs.append(g[4]); ays.append(g[5])
            bts.append(g[9])
    def stats(name, v):
        if not v: return f"{name}: no data"
        return (f"{name}: n={len(v)} mean={st.mean(v):.1f} min={min(v)} max={max(v)} "
                f"jitter={max(v)-min(v)} stdev={st.pstdev(v):.2f}")
    print("  raw " + stats("x", xs))
    print("  raw " + stats("y", ys))
    print("  nrm " + stats("ax", axs))
    print("  nrm " + stats("ay", ays))
    print(f"  bt flag values seen: {sorted(set(bts))}")
    # drift: compare first third vs last third
    if len(xs) > 30:
        k = len(xs)//3
        print(f"  drift x: first-third mean {st.mean(xs[:k]):.1f} -> last-third {st.mean(xs[-k:]):.1f}")
        print(f"  drift y: first-third mean {st.mean(ys[:k]):.1f} -> last-third {st.mean(ys[-k:]):.1f}")
    if len(jz1_t) > 2:
        gaps = [(b-a)*1000 for a, b in zip(jz1_t, jz1_t[1:])]
        print(f"  JZ1 idle cadence: n={len(gaps)} median={st.median(gaps):.0f} ms "
              f"min={min(gaps):.0f} max={max(gaps):.0f}  (HEARTBEAT_MS=500)")

    # ---- 2. true command round trip, via the RAW pin echo the firmware prints
    print("\n== LATENCY A: command -> firmware acknowledgement (RAW pin echo) ==")
    rtt = []
    for i in range(20):
        ser.reset_input_buffer()
        val = i % 2
        t_send = time.time()
        ser.write(f"RAW 19 {val}\n".encode())
        ser.flush()
        deadline = t_send + 1.0
        while time.time() < deadline:
            ln = ser.readline()
            if ln and ln.startswith(b"RAW pin 19"):
                rtt.append((time.time() - t_send) * 1000.0)
                break
        time.sleep(0.05)
    if rtt:
        rtt.sort()
        print(f"  n={len(rtt)} median={st.median(rtt):.1f} ms  p90={rtt[int(len(rtt)*0.9)-1]:.1f} ms "
              f"worst={max(rtt):.1f} ms  best={min(rtt):.1f} ms")
    else:
        print("  no acknowledgements received")

    # ---- 3. observable latency: LED command -> next JZ1 frame
    print("\n== LATENCY B: LED command -> next JZ1 frame (what the game sees) ==")
    obs = []
    for i in range(20):
        ser.reset_input_buffer()
        t_send = time.time()
        ser.write(b"LED 0.0 0.0 0.0\n")
        ser.flush()
        deadline = t_send + 2.0
        while time.time() < deadline:
            ln = ser.readline()
            if ln and ln.startswith(b"JZ1"):
                obs.append((time.time() - t_send) * 1000.0)
                break
        time.sleep(0.02)
    if obs:
        obs.sort()
        print(f"  n={len(obs)} median={st.median(obs):.1f} ms  p90={obs[int(len(obs)*0.9)-1]:.1f} ms "
              f"worst={max(obs):.1f} ms")
        print("  NOTE: JZ1 is emitted on change or every HEARTBEAT_MS=500 when idle,")
        print("        so with a resting stick this measures heartbeat phase, not link lag.")
finally:
    if ser is not None:
        try:
            ser.write(b"LED 0.0 0.0 0.0\n"); ser.flush(); time.sleep(0.1)
        except Exception:
            pass
        link.close(ser)
    print("\nport closed")

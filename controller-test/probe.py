"""Step 1+2: open each candidate port, report bytes and JZ1 frames seen."""
import sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

LISTEN_S = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
path = sys.argv[1]

print(f"port      : {path}")
print(f"baud      : {link.BAUD}")
ser = None
try:
    ser, open_ms = link.open_port(path, open_timeout=5.0)
    print(f"open      : ok in {open_ms:.1f} ms")
    t0 = time.time()
    nbytes = 0
    jz1 = 0
    rawdbg = 0
    other = 0
    first_jz1_at = None
    samples = []
    while time.time() - t0 < LISTEN_S:
        line = ser.readline()
        if not line:
            continue
        nbytes += len(line)
        txt = line.decode("ascii", "ignore").strip()
        if txt.startswith("JZ1"):
            jz1 += 1
            if first_jz1_at is None:
                first_jz1_at = time.time() - t0
            if len(samples) < 5:
                samples.append(txt)
        elif txt.startswith("RAW x="):
            rawdbg += 1
            if len(samples) < 5:
                samples.append(txt)
        elif txt:
            other += 1
    el = time.time() - t0
    print(f"listened  : {el:.1f} s")
    print(f"bytes     : {nbytes}")
    print(f"JZ1 lines : {jz1}   ({jz1/el:.1f}/s)"
          + (f"  first at {first_jz1_at*1000:.0f} ms" if first_jz1_at is not None else ""))
    print(f"RAW dbg   : {rawdbg}")
    print(f"other     : {other}")
    for s in samples:
        print(f"  | {s}")
    print("VERDICT   : " + ("LINK LIVE" if jz1 else "NO FRAMES"))
except TimeoutError as exc:
    print(f"open      : BLOCKED - {exc}")
    print("VERDICT   : OPEN HUNG")
except Exception as exc:
    print(f"open      : FAILED - {type(exc).__name__}: {exc}")
    print("VERDICT   : OPEN FAILED")
finally:
    if ser is not None:
        link.close(ser)
    print("closed    : yes")

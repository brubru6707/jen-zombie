"""Diagnose: pin->colour identity, dim visibility, and current bleed between lamps."""
import sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

GREEN, YELLOW, RED = 19, 18, 21
ser = None

def raw(s, pin, val):
    s.write(f"RAW {pin} {val}\n".encode()); s.flush()

def led(s, g, y, r):
    s.write(f"LED {g:.2f} {y:.2f} {r:.2f}\n".encode()); s.flush()

def banner(msg, secs):
    print(f"  [{secs:>2}s] {msg}", flush=True)

try:
    ser, _ = link.open_port("/dev/cu.usbserial-0001", 5.0)
    led(ser, 0, 0, 0)
    print("lead-in 6 s, all lamps off"); time.sleep(6.0)

    print("\n== PART 1: name the colour you see for each pin, 4 s each ==")
    for pin, label in ((GREEN, "pin 19 (sketch calls this GREEN)"),
                       (YELLOW, "pin 18 (sketch calls this YELLOW)"),
                       (RED, "pin 21 (sketch calls this RED)")):
        banner(f"{label} -> full on", 4)
        raw(ser, pin, 1); time.sleep(4.0)
        raw(ser, pin, 0); time.sleep(2.0)

    print("\n== PART 2: is 20% brightness visible at all? ==")
    banner("pin 18 alone at 0.20 (the crossfade level you said you could not see)", 5)
    led(ser, 0, 0.20, 0); time.sleep(5.0)
    banner("pin 18 alone at 1.00 for comparison", 4)
    led(ser, 0, 1.0, 0); time.sleep(4.0)
    led(ser, 0, 0, 0); time.sleep(2.0)

    print("\n== PART 3: bleed test. Only ONE pin is driven, the others float ==")
    banner("RED driven HIGH, green and yellow pins HIGH-Z. Any other lamp lit = current bleed", 6)
    raw(ser, GREEN, 2); raw(ser, YELLOW, 2); raw(ser, RED, 1); time.sleep(6.0)
    raw(ser, RED, 0); time.sleep(2.0)

    banner("GREEN driven HIGH, yellow and red pins HIGH-Z", 6)
    raw(ser, YELLOW, 2); raw(ser, RED, 2); raw(ser, GREEN, 1); time.sleep(6.0)
    raw(ser, GREEN, 0); time.sleep(2.0)

    print("\n== PART 4: all three pins HIGH-Z. Nothing should be lit at all ==")
    banner("all pins floating", 5)
    raw(ser, GREEN, 2); raw(ser, YELLOW, 2); raw(ser, RED, 2); time.sleep(5.0)

    # restore every pin to a driven LOW output, then normal PWM control
    for p in (GREEN, YELLOW, RED):
        raw(ser, p, 0); time.sleep(0.1)
    led(ser, 0, 0, 0)
    print("\npins restored to OUTPUT LOW, PWM control handed back")

    # drain the board's acknowledgements so we can prove it accepted them
    time.sleep(0.4)
    acks = []
    t0 = time.time()
    while time.time() - t0 < 1.0:
        ln = ser.readline()
        if ln and (b"RAW pin" in ln or b"polarity" in ln):
            acks.append(ln.decode("ascii", "ignore").strip())
    print(f"board acknowledgements seen this run: {len(acks)}")
    for a in acks[-4:]:
        print(f"  | {a}")
finally:
    if ser is not None:
        try:
            led(ser, 0, 0, 0); time.sleep(0.2)
        except Exception:
            pass
        link.close(ser)
    print("lamps off, port closed")

"""
Stage 0: does the external battery actually power the board?

With USB unplugged there is no data link, so the lamps are the only signal.
Light all three, then leave them lit ON PURPOSE and close the port. If the
board keeps running on battery the lamps stay on. If USB was the only power
source they go dark the instant the cable comes out.

All three at full is also the heaviest LED load the board can present, so this
doubles as a crude check that the battery can drive it.
"""
import re, sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

RAWRE = re.compile(rb"\(centre\s*(\d+)/\s*(\d+)\)")
ser = None
try:
    ser, ms = link.open_port("/dev/cu.usbserial-0001", 5.0)
    print(f"port open in {ms:.1f} ms")

    # Record the boot calibration centre. If the board reboots later these
    # change, which is how we prove a power cycle actually happened.
    centre = None
    t0 = time.time()
    while time.time() - t0 < 4.0 and centre is None:
        ln = ser.readline()
        if not ln:
            continue
        m = RAWRE.search(ln)
        if m:
            centre = (int(m.group(1)), int(m.group(2)))
    print(f"current boot centre: {centre}   <-- remember this, it proves a reboot")

    ser.write(b"LED 1.0 1.0 1.0\n")
    ser.flush()
    time.sleep(0.5)
    print("all three lamps driven to full brightness")
finally:
    if ser is not None:
        # Deliberately NOT sending 'LED 0 0 0' here. The lit lamps are the
        # measurement. The port is still closed properly.
        link.close(ser)
    print("port closed, lamps left lit on purpose")

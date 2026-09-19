"""
Push WiFi and host config to the board over USB serial.

    python3 configure.py <ssid> <password> <mac_ip> [port]

The password is written to the board and never printed, never logged and never
stored on this machine. It lives only in the board's NVS.
"""
import sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

if len(sys.argv) < 4:
    print(__doc__)
    raise SystemExit(2)

ssid, password, ip = sys.argv[1], sys.argv[2], sys.argv[3]
port = sys.argv[4] if len(sys.argv) > 4 else "3333"

# SSID and PASS take the rest of the line verbatim, so spaces are fine now.
# The whole line must still fit in the board's 64 byte buffer.
for label, val in (("SSID", ssid), ("password", password)):
    if len(val) > 55:
        print(f"ERROR: {label} is too long for the 64 byte line buffer")
        raise SystemExit(1)

ser = None
try:
    ser, _ = link.open_port("/dev/cu.usbserial-0001", 5.0)

    # Opening the port resets this board. A command sent now lands in the
    # bootloader and is silently lost, which looks exactly like the firmware
    # ignoring it. Wait for the boot banner, or 6 s, whichever comes first.
    print("waiting for boot to finish...")
    t_boot = time.time()
    while time.time() - t_boot < 6.0:
        ln = ser.readline()
        if ln and b"calibrating centre" in ln:
            time.sleep(1.5)          # let calibration and setup() finish
            break
    ser.reset_input_buffer()

    ser.write(f"SSID {ssid}\n".encode("ascii")); ser.flush()
    time.sleep(1.0)
    ser.write(f"PASS {'' if password == '-' else password}\n".encode("ascii")); ser.flush()
    time.sleep(1.0)
    ser.write(f"HOST {ip} {port}\n".encode("ascii")); ser.flush()
    time.sleep(1.0)

    print(f"sent: SSID {ssid}")
    print(f"sent: PASS <withheld>" if password != "-" else "sent: PASS (open network)")
    print(f"sent: HOST {ip} {port}")
    print("\nwaiting up to 45 s for association and TCP connect...")

    t0 = time.time()
    last = ""
    while time.time() - t0 < 45:
        ln = ser.readline()
        if not ln:
            continue
        t = ln.decode("ascii", "ignore").rstrip()
        if password in t:
            continue                      # belt and braces, never echo it
        if t.startswith("wifi:") or t.startswith("tcp:") or t.startswith("NET"):
            print(f"  [{time.time()-t0:5.1f}s] {t}")
        if t.startswith("RAW x=") and "wifi=1" in t:
            if "tcp=1" in t and "tcp=1" not in last:
                print(f"  [{time.time()-t0:5.1f}s] *** TCP UP ***")
                last = t
                break
            last = t

    ser.write(b"NET\n"); ser.flush()
    time.sleep(0.6)
    t0 = time.time()
    got_ssid = None
    while time.time() - t0 < 3:
        ln = ser.readline()
        if ln and ln.startswith(b"NET"):
            txt = ln.decode("ascii", "ignore").rstrip()
            print("  " + txt)
            if txt.startswith("NET ssid="):
                got_ssid = txt.split("=", 1)[1]
    # Verify the board actually took the setting rather than trusting the write.
    if got_ssid is not None and got_ssid != ssid:
        print(f"\n*** WARNING: board reports ssid={got_ssid}, not {ssid}")
        print("*** the command did not take effect")
finally:
    if ser is not None:
        link.close(ser)
    print("port closed")

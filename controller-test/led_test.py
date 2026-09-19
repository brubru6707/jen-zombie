"""Step 5: each lamp alone, then a meter sweep using the game's own mapping."""
import sys, time
sys.path.insert(0, "/Users/brrodrig/Documents/FUQ_WINDOWS/bat_cave/jen-zombie/controller-test")
import link

# Faithful copy of meter_lamps() from game/audio.py:206, same constants.
def meter_lamps(level):
    lvl = max(0.0, min(1.0, level))
    def ramp(a, b):
        if b <= a:
            return 1.0 if lvl >= b else 0.0
        return max(0.0, min(1.0, (lvl - a) / (b - a)))
    audible = ramp(0.0, 0.02)
    to_yellow = ramp(0.28, 0.38)
    to_red = ramp(0.62, 0.72)
    return audible * (1.0 - to_yellow), to_yellow * (1.0 - to_red), to_red

ser = None
def send(s, g, y, r):
    s.write(f"LED {g:.2f} {y:.2f} {r:.2f}\n".encode("ascii"))
    s.flush()

try:
    ser, _ = link.open_port("/dev/cu.usbserial-0001", 5.0)
    send(ser, 0, 0, 0)
    print("lead-in 6 s, lamps off")
    time.sleep(6.0)

    print("\n== PART A: each lamp alone, 3 s each ==")
    for name, trio in (("GREEN  (pin 19)", (1, 0, 0)),
                       ("YELLOW (pin 18)", (0, 1, 0)),
                       ("RED    (pin 21)", (0, 0, 1))):
        print(f"  t={time.strftime('%H:%M:%S')}  {name} full on")
        send(ser, *trio); time.sleep(3.0)
        send(ser, 0, 0, 0); print("            all off"); time.sleep(2.0)

    print("\n== PART B: meter sweep 0.00 -> 1.00, 2 s per step ==")
    print(f"  {'level':>6}  {'green':>5} {'yellow':>6} {'red':>5}   expect")
    for i in range(11):
        lvl = i / 10.0
        g, y, r = meter_lamps(lvl)
        if g > 0.5 and y <= 0.5: exp = "GREEN"
        elif y > 0.5 and g <= 0.5 and r <= 0.5: exp = "YELLOW"
        elif r > 0.5: exp = "RED"
        elif g > 0 and y > 0: exp = "green+yellow crossfade"
        elif y > 0 and r > 0: exp = "yellow+red crossfade"
        elif g == y == r == 0: exp = "all off"
        else: exp = "mixed"
        print(f"  {lvl:6.2f}  {g:5.2f} {y:6.2f} {r:5.2f}   {exp}")
        send(ser, g, y, r); time.sleep(2.0)
finally:
    if ser is not None:
        try:
            send(ser, 0, 0, 0); time.sleep(0.2)
        except Exception:
            pass
        link.close(ser)
    print("\nlamps off, port closed")

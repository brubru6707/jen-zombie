"""Step 7: run the game's OWN SPPControllerSource and time what it does."""
import sys, time
sys.path.insert(0, "/Users/brrodrig/documents/fuq_windows/bat_cave/jenzombies")
from game.spp import SPPControllerSource, find_ports, PORT_GLOBS, VALIDATE_S, RECONNECT_EVERY, STALE_AFTER

print(f"PORT_GLOBS order : {PORT_GLOBS}")
print(f"candidates found : {find_ports()}")
print(f"VALIDATE_S={VALIDATE_S}  RECONNECT_EVERY={RECONNECT_EVERY}  STALE_AFTER={STALE_AFTER}\n")

src = SPPControllerSource()
t0 = time.time()
connected_at = None
try:
    while time.time() - t0 < 20.0:
        if src.connected and connected_at is None:
            connected_at = time.time() - t0
            print(f"*** CONNECTED after {connected_at:.2f} s on {src.port_path}")
            print(f"    opened_note: {src.opened_note}")
        time.sleep(0.1)
    print(f"\nfinal status : {src.status()}")
    print(f"port_path    : {src.port_path}")
    print(f"lines_rx     : {src.lines_rx}   bad_lines: {src.bad_lines}   reconnects: {src.reconnects}")
    print(f"last_error   : {src.last_error}")
    print(f"raw tuple    : {src.raw}")
    st = src.read()
    print(f"read()       : move={st.move} attack={st.attack} interact={st.interact} sens={st.sensitivity:.2f}")
    if connected_at is None:
        print("\n*** NEVER CONNECTED in 20 s")
    else:
        print(f"\ntime to usable controller: {connected_at:.2f} s")
        print(f"of which the dead SteelHacks port costs up to VALIDATE_S={VALIDATE_S}s per attempt")
finally:
    src.close()
    time.sleep(0.3)
    print("source closed")

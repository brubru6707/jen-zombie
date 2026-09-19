"""
Safe open/read helpers for the SteelHacks controller.

Every open runs on a daemon thread behind a wall clock deadline, because a
Bluetooth SPP open can block forever and a hang must not look like a dead
board. The port is closed in a finally block and on SIGINT/SIGTERM.
"""
import signal
import sys
import threading
import time

import serial

BAUD = 115200          # game/spp.py BAUD, and Serial.begin(115200) in the sketch

_open_ser = []


def _cleanup(*_a):
    for s in _open_ser:
        try:
            s.close()
        except Exception:
            pass
    _open_ser.clear()


signal.signal(signal.SIGINT, lambda *a: (_cleanup(), sys.exit(130)))
signal.signal(signal.SIGTERM, lambda *a: (_cleanup(), sys.exit(143)))


def open_port(path, open_timeout=5.0):
    """Open with DTR/RTS low. Returns serial or raises TimeoutError/OSError."""
    box = {}

    def _do():
        try:
            s = serial.Serial(timeout=0.2, write_timeout=0.5, baudrate=BAUD)
            s.port = path
            s.dtr = False
            s.rts = False
            t0 = time.time()
            s.open()
            box["ms"] = (time.time() - t0) * 1000.0
            box["ser"] = s
        except Exception as exc:
            box["err"] = exc

    th = threading.Thread(target=_do, daemon=True)
    th.start()
    th.join(open_timeout)
    if th.is_alive():
        raise TimeoutError(f"open({path}) still blocked after {open_timeout:.1f}s")
    if "err" in box:
        raise box["err"]
    _open_ser.append(box["ser"])
    return box["ser"], box["ms"]


def close(s):
    try:
        if s in _open_ser:
            _open_ser.remove(s)
        s.close()
    except Exception:
        pass

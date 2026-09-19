# jenzombies controller wire protocol

Version 2.0, 2026-09-19. Transport changed from Bluetooth Classic SPP to raw
TCP over WiFi. **The line protocol is byte for byte identical to version 1.**
Only the transport underneath changed.

This file is the contract. The Mac side is implemented against it. It does not
change without the owner of the firmware saying so first.

---

## 1. Architecture

```
ESP32 handheld  --raw TCP over WiFi-->  Mac  --(bridge, not covered here)-->  phone browser
```

- The **ESP32 is the TCP client**. It dials out to the Mac.
- The **Mac is the TCP server**. It listens and accepts.
- This direction is deliberate: the board knows the Mac's address from its own
  persisted config, and venue DHCP is free to move the board around without
  anyone needing to discover it.
- There is **no websocket, no TLS, no HTTP and no framing layer**. It is a raw
  byte stream carrying newline delimited ASCII.

The board speaks **USB serial and TCP at the same time** and does not care which
one is listening. See section 6.

---

## 2. Framing and encoding

| Property | Value |
|---|---|
| Encoding | 7 bit ASCII. Never UTF-8, never binary |
| Line terminator, board to Mac | `\n` (0x0A) only. No carriage return |
| Line terminator, Mac to board | `\n` or `\r\n`. Both accepted, `\r` is stripped |
| Field separator | a single space (0x20) |
| Maximum line length | **64 bytes including the terminator** |
| Case | Command keywords are UPPERCASE and matched exactly |

**Overlong lines are dropped, not truncated and not wrapped.** If the board
receives more than 63 bytes without a terminator it discards the buffer and
resynchronises at the next newline. Implementations on the Mac side must do the
same, because a partial line must never be parsed as a complete one.

**Unrecognised lines are ignored silently.** This is how the protocol stays
forward compatible: either side may add a message type without breaking the
other. Do not treat an unknown keyword as an error.

---

## 3. Board to Mac

### JZ1, the only message

```
JZ1 <ax> <ay> <sw> <atk> <sens>\n
```

| Field | Type | Range | Meaning |
|---|---|---|---|
| `ax` | int | 0..4095 | Joystick X. 0 is full left, 4095 is full right, 2048 is centre |
| `ay` | int | 0..4095 | Joystick Y. 0 is full **up**, 4095 is full **down**, 2048 is centre |
| `sw` | int | 0 or 1 | Joystick push switch. **1 = pressed** |
| `atk` | int | 0 or 1 | Attack button. **1 = pressed** |
| `sens` | int | 2048 or 4095 | Sensitivity toggle. **Only these two values ever occur** |

Example:

```
JZ1 2048 2047 0 0 2048
```

**Notes that will bite an implementer who skips them:**

- `ay` is already inverted on the board. Pushing the stick **up** yields a
  value **below** 2048. This matches screen coordinates, where y grows downward,
  and it matches the keyboard, where up is negative. Do not invert it again.
- Both axes are **centre corrected and per half rescaled on the board** against
  a calibration taken at boot. The Mac receives a linearised value and must not
  apply its own centring.
- Axes are **not deadzoned on the board**. The Mac applies the deadzone.
  Reference value in use: 0.05 of full scale, which is +/-102 counts.
- `sens` is a **two position digital toggle**, not a potentiometer, despite the
  ADC shaped range. GPIO 23 has no ADC and ADC2 is unusable while the radio is
  on. Only 2048 (released) and 4095 (pressed) are ever transmitted.
- Measured resting jitter is 11 to 14 counts on each axis. Anything inside that
  band is noise, not movement.

### When JZ1 is sent

A line is emitted when **either** condition holds:

1. **Change.** An axis moved at least `AXIS_EMIT_DELTA` (18) raw counts, or any
   of `sw`, `atk`, `sens` changed, or `sens` moved 200 or more.
2. **Heartbeat.** 500 ms have passed since the last line, regardless of input.

A stationary controller therefore produces a steady **2 Hz** stream and nothing
more. An actively moved one produces up to about 125 Hz.

**The heartbeat is the liveness signal.** The Mac distinguishes "idle" from
"gone" by the absence of lines, not by any explicit disconnect message.
Recommended staleness threshold on the Mac: **1.0 s**, which is two missed
heartbeats.

**Do not use the heartbeat to measure latency.** Timing a command to the next
`JZ1` measures where you landed in the 500 ms heartbeat phase, not link lag.
Use the `RAW` acknowledgement in section 4 instead.

---

## 4. Mac to board

### LED, lamp brightness

```
LED <green> <yellow> <red>\n
```

Three floats, each clamped to 0.0..1.0, applied directly as PWM duty. Two
decimal places is the convention. The meter logic lives on the Mac; the board
applies what it is told and holds it until the next `LED` line.

```
LED 0.00 1.00 0.00
```

The lamps are driven by these **colours**, not by pin numbers. The firmware owns
the pin mapping, which was corrected on 2026-09-19 after a three way rotation
was found by eye:

| Colour | GPIO |
|---|---|
| red | 18 |
| yellow | 19 |
| green | 21 |

**The Mac must never address pins directly except through `RAW`.**

### POL, lamp polarity

```
POL <0|1>\n
```

`0` = active high, `1` = active low. This board is **active high (`POL 0`)**:
each LED has its cathode on ground and its anode on a GPIO through a 220 ohm
resistor. The setting exists because polarity is a property of the wiring, not
the code, and getting it wrong inverts every lamp. It is not persisted and
resets to active high on every boot.

The board flashes all three lamps briefly when polarity changes, so the change
is visible without instrumentation.

### RAW, diagnostic pin drive

```
RAW <pin> <val>\n
```

Drives one lamp pin with `digitalWrite`, bypassing PWM entirely.

| `val` | Effect |
|---|---|
| `0` | pin OUTPUT, driven LOW |
| `1` | pin OUTPUT, driven HIGH |
| `2` | pin INPUT, high impedance |

`pin` must be 18, 19 or 21. Any other value is ignored.

This exists to separate three failures that look identical from the Mac: a dead
lamp, a pin that PWM is not driving, and current bleeding between lamps through
a floating common. Setting every pin to `2` and seeing a lamp still lit proves
current is arriving through another lamp.

**`RAW` is the only command the board acknowledges.** The acknowledgement goes
back on **the transport the command arrived on**, so a round trip can be timed
on the link actually under test. A command that arrived over TCP is additionally
echoed to USB, so the serial log stays complete for a human watching it:

```
RAW pin 19 -> HIGH
RAW pin 19 -> LOW
RAW pin 19 -> HI-Z
```

**This is the supported way to measure round trip latency.** Send a `RAW`, wait
for the matching echo, take the difference. `LED` is deliberately not
acknowledged, because it is on the hot path of a live meter.

After a `RAW`, the affected pin stays under `digitalWrite` control until the
next `LED` line restores PWM.

---

## 5. Connection lifecycle

### Board side

1. On boot the board reads WiFi and host config from NVS (section 7).
2. It associates with WiFi, then dials `HOST:PORT` over TCP.
3. On success it begins emitting `JZ1` on the TCP socket.
4. On any failure, at either layer, it retries with **exponential backoff
   starting at 1 s, doubling to a 30 s ceiling**, and resets to 1 s after a
   successful connection.
5. **Reconnection never blocks the input loop.** The joystick continues to be
   sampled and USB serial continues to work at full rate while WiFi is down.

### Mac side, what an implementer must handle

- **Accept one client at a time.** If a second connection arrives, the newest
  wins and the older socket is closed. A board that lost its link without the
  Mac noticing will reconnect while the stale socket still looks open, and
  refusing the new connection would strand the controller until the demo ends.
- **Set `TCP_NODELAY`.** Nagle's algorithm will otherwise coalesce these tiny
  lines and add tens of milliseconds for no benefit.
- **Treat silence as disconnection** after 1.0 s, as in section 3. There is no
  goodbye message and a dropped WiFi link produces none.
- **Do not assume a clean close.** A board that walks out of range produces a
  half open socket, and reads return nothing rather than an error. Time it out.
- **Buffer by line.** TCP is a stream. A single read may return half a line, or
  three lines and a fragment. Split on `\n` and keep the remainder.
- **Neutral, not last known, on staleness.** When the link goes stale, report a
  centred stick and released buttons. If the link dies with the stick pushed,
  the player should stop, not keep running.

---

## 6. USB serial, which runs in parallel

USB serial is **always live** at **115200 baud, 8N1**, whether or not WiFi is
connected. The board does not care which transport is listening.

| Traffic | USB | TCP |
|---|---|---|
| `JZ1` frames | always | only while connected |
| `LED`, `POL`, `RAW` accepted | yes | yes |
| `RAW` acknowledgements | yes | yes, when the request came from TCP |
| Config commands (section 7) | yes | **no** |
| Debug line (section 8) | yes | no |
| Boot and diagnostic text | yes | no |

Commands from both transports are accepted and the **last one to arrive wins**.
There is no locking and no priority.

**Operational warning.** USB power and USB data share one connector on this
board. When it runs from a USB power bank there is **no data host**, so USB
serial is unavailable in exactly the untethered configuration the demo uses.
Treat USB as a bench and development transport, not as a fallback at the venue.

---

## 7. Runtime configuration, USB serial only

Credentials are never compiled into the sketch. These commands are accepted
**only over USB serial**, deliberately, so nothing on the network can rewrite
where the board connects.

```
SSID <name>\n                set the SSID, rest of line verbatim, spaces allowed
PASS <password>\n            set the key, rest of line verbatim; empty or `-` = open
WIFI <ssid> <password>\n     both at once, convenience form, NO spaces allowed
HOST <ip> <port>\n           set the Mac endpoint, persist to NVS, reconnect
NET\n                        print current state, see below
SCAN\n                       list 2.4 GHz networks this radio can actually see
KEEP <on> <period_ms> <burst_ms>\n   tune the power bank keep-alive
SAVE\n                       force a write of current config to NVS
```

`KEEP` exists because this handheld runs from a USB power bank, and power banks
shut down when current draw falls below their threshold. It periodically raises
the board's draw to keep the bank awake. `on` is 0 or 1, `period_ms` is the gap
between bursts and `burst_ms` is how long each burst lasts. Defaults are
`1 6000 250`. It is persisted to NVS like the rest of the config.

The burst runs on a task pinned to **core 0**, while the input loop runs on core
1, so it never adds latency or blocks sampling. While TCP is disconnected the
burst also blinks the green lamp, which is the only way to see that a board is
alive when it is running untethered with no data link.

- **`WIFI` splits on spaces and cannot carry a name containing one.** Use
  `SSID` and `PASS`, which take everything after the keyword verbatim. A real
  network on this project is named `Pitt Guest`, so this is not hypothetical.
- **Open networks**: send `WIFI <ssid>` with no password, or use `-` as the
  password. Both store an empty key and associate with open authentication.
- The radio is **2.4 GHz only**. A 5 GHz SSID is invisible to this board, which
  looks identical to a wrong password: association simply never completes.
- Settings are written to NVS immediately and survive power cycles and reflashes.
- Changing either setting tears down the existing connection and redials.

`NET` prints a block of `key=value` lines terminated by `NET END`:

```
NET ssid=<ssid or -> 
NET wifi=<0|1> ip=<dotted quad or -> rssi=<dBm or 0>
NET host=<ip> port=<n> tcp=<0|1>
NET backoff=<current retry interval ms>
NET keepalive=<0|1> period=<ms> burst=<ms>
NET END
```

The password is **never printed**, by `NET` or anything else.

---

## 8. Debug line, USB serial only

Emitted every 100 ms. This is **not part of the wire protocol** and the Mac side
must not parse it for gameplay. It exists for humans and for test scripts.

```
RAW x=<raw> y=<raw> (centre <cx>/<cy>) -> ax=<n> ay=<n> sw=<n> atk=<n> sens=<n> wifi=<0|1> tcp=<0|1> rssi=<dBm>
```

The `wifi`, `tcp` and `rssi` fields replace the `bt=` field of version 1 and
serve the same purpose: seeing link state on the device without trusting the
host's opinion of it. `rssi` reads 0 when WiFi is down, and is the field to
watch during a range test.

---

## 9. Boot sequence

1. Lamps flash green, yellow, red, twice, at roughly 90 ms each.
2. `Serial.begin(115200)`.
3. **Joystick centre calibration, about 300 ms. The stick must be still.**
   A stick held off centre at boot becomes the new zero.
4. NVS config load.
5. WiFi association and TCP dial, both non blocking.

Calibration values are printed and appear in the debug line as `centre cx/cy`.
**They are the reliable way to prove a board actually rebooted**, since they
differ slightly on every boot.

---

## 10. Reserved and deliberately absent

- **No hello, version or goodbye message.** The first `JZ1` is the hello and
  silence is the goodbye. Version 1 had none and adding them would break the
  "identical line protocol" requirement.
- **No acknowledgement for `LED`.** Latency is measured with `RAW`.
- **No binary mode, no compression, no batching.**
- **No authentication.** Anyone on the network who can reach the port can drive
  the lamps. The lamps are the only writable surface and the board cannot be
  reconfigured over TCP, which bounds the damage. Do not add config commands to
  the TCP path without revisiting this.

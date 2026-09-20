# The handheld's power bank shuts off after ~30 s

Handover report. Written 2026-09-20 after exhausting every fix available
without touching the ESP32 firmware. **The problem is not solved.** Everything
below is measured or read from source; where a number is an estimate it says
so.

## Symptom

The ESP32 handheld runs from a USB power bank. The bank has charge. About
thirty seconds after the handheld is connected, the bank switches itself off
and the controller dies. Plugged into the Mac's USB instead, the board runs
indefinitely.

## Why it happens, from the firmware source

`controller-test/firmware/jenzombies_controller/jenzombies_controller.ino`,
`keepAliveTask`:

```c
bool beacon = !tcpUp;
if (beacon) writeLamp(PIN_LED_GREEN, 1.0f);

unsigned long until = millis() + keepBurstMs;
volatile double acc = 1.0;
while (millis() < until) {
  for (int i = 0; i < 2000; i++) acc = acc * 1.000001 + 0.000001;
  if (acc > 1e9) acc = 1.0;
}

if (beacon) writeLamp(PIN_LED_GREEN, 0.0f);
```

Two things follow, and together they explain the thirty seconds exactly:

1. **The burst is a CPU busy-loop and nothing else.** A floating-point spin on
   core 0. It raises draw by tens of milliamps, not hundreds.
2. **The green lamp is lit only while TCP is DOWN** (`beacon = !tcpUp`). While
   the board is booting, associating and dialling the Pi it draws CPU spin plus
   one LED. The instant it connects, the lamp goes out and the draw drops to
   CPU spin plus an associated-but-idle radio.

So the board's current *falls* at the moment it finishes connecting, which is
roughly thirty seconds after power-up. The bank sees the draw cross below its
auto-off threshold and cuts power. The keep-alive was written for a board that
had not yet connected; it stops helping precisely when the demo starts.

## What has been tried, in order, and what happened

| # | Change | Result |
|---|---|---|
| 1 | `KEEP 1 2500 800` — 800 ms burst every 2.5 s, 32% duty | Still cut out |
| 2 | `KEEP 1 500 499` — 499 ms burst every 500 ms, ~99.8% duty | Still cut out |
| 3 | Pi-side radio keep-alive: bridge re-sends the current `LED` line at 25 Hz | Still cut out |

Notes on each:

**1 and 2** were sent over USB serial, which is the only transport the firmware
accepts config on (PROTOCOL.md section 7). Both are persisted to NVS. **Nothing
was reflashed.** 499/500 is the maximum the firmware will accept: the parser
clamps `period >= 500` and requires `burst < period`.

```c
if (sscanf(line, "KEEP %d %lu %lu", &on, &per, &bur) == 3) {
  keepOn = (on != 0);
  if (per >= 500)  keepPeriodMs = per;
  if (bur >= 10 && bur < per) keepBurstMs = bur;
```

**3** is in `controller-bridge/bridge.py` (`radio_keepalive`). The reasoning:
the radio is the dominant consumer on an ESP32, and an associated station drops
into modem sleep between DTIM beacons. Writing to it continuously forces it to
stay in receive. The writes are deliberately redundant — they repeat the lamp
value the board already holds, so nothing changes visually and the game is
unaffected. Verified working: 492 writes in the first 20 seconds, controller
still streaming normally throughout. It did not keep the bank awake either.

## Current draw, estimated

Nobody has measured this. These are order-of-magnitude figures for an
ESP32-D0WD-V3:

| Source | Estimate |
|---|---|
| CPU busy-loop (the KEEP burst) | 20–40 mA |
| Radio, associated and idle | 20–30 mA |
| Radio, actively receiving | ~100 mA |
| One LED at full (3.3 V, ~2 V Vf, 220 Ω) | ~6 mA |
| All three LEDs at full | ~18 mA |

A typical bank wants 50–100 mA sustained and gives up after 10–30 s below it.
With everything above turned on we are plausibly in the 60–120 mA range, and it
still cuts out, which suggests either the estimates are optimistic or this
bank's threshold is at the high end.

**This is the biggest gap in the report.** A USB power meter (about $10) or a
multimeter in series would turn all of the above into facts and would say
immediately whether the next lever is worth pulling.

## What is left

### Software, implemented but off by default

`JZ_LED_FLOOR` on the jzbridge service holds every lamp at a minimum
brightness, so the three LEDs are never dark. Worth roughly 18 mA more.

```
sudo systemctl edit jzbridge      # Environment=JZ_LED_FLOOR=1.0
sudo systemctl restart jzbridge
```

Left off because permanently lit lamps look exactly like the stuck-meter bug
that was reported and fixed earlier today. It is the last software lever and it
is probably not enough on its own.

### Firmware, NOT done, and deliberately so

The correct fix is one line. In `keepAliveTask`:

```c
bool beacon = !tcpUp;        // becomes: bool beacon = true;
```

That lights a lamp during every burst whether or not the controller is
connected, adding 6 mA continuously at the current ~100% duty, or up to 18 mA
if all three lamps are driven. Combined with the CPU burst and the radio
keep-alive already in place, that is the most the board can draw.

**It has not been done because reflashing is off the table by the owner's
instruction: one upload in eight bricked the board, and the firmware is final.**
If a flash is ever authorised for another reason, this change should ride along
so the board is only exposed to one upload.

### Hardware, most likely to actually work

1. **The bank's own low-current mode.** Most banks have one for earbuds and
   watches, usually a double-press of the power button, sometimes a dedicated
   button. This is the designed solution for exactly this problem and costs
   nothing. Worth checking the model's manual before any further engineering.
2. **A different power source.** A wall charger, or one of the Pi's USB ports —
   the Pi is already at the venue, already powered, and does not auto-off.
3. **A bank without auto-shutoff.** Some are sold specifically for low-draw
   devices.

## State the board and the bridge are in now

Whoever picks this up should know what has already been changed:

- **Board NVS**: `keepalive=1 period=500 burst=499`. Core 0 now spins almost
  continuously. The input loop is on core 1 so latency is unaffected, but the
  board runs warmer and drains any supply faster. If the power problem is
  solved by other means, consider restoring the original `KEEP 1 6000 250` over
  USB serial.
- **jzbridge**: runs `radio_keepalive` at 25 Hz. Controlled by
  `JZ_LED_KEEPALIVE_HZ` (default 25, set 0 to disable) and `JZ_LED_FLOOR`
  (default 0). Visible in `/status` as `led.keepalive_writes`.
- Nothing in the firmware image has been modified.

## Recommendation

Check the bank for a low-current mode first, and failing that power the
handheld from the Pi or a wall charger for the demo. Both are free and
immediate.

Buy a USB power meter before spending more engineering time here; every number
in this report above the firmware section is an estimate, and the decision
about whether the one-line firmware change is worth a flash should be made
against a measurement rather than against arithmetic.

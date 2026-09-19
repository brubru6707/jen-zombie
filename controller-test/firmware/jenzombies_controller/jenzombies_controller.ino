/*
 * jenzombies controller firmware, version 2
 * ESP32-D0WD-V3, raw TCP over WiFi, plus USB serial in parallel.
 *
 * Replaces the Bluetooth Classic SPP transport of version 1. The line protocol
 * is byte for byte unchanged, so the Mac side needs no protocol work. See
 * PROTOCOL.md, which is the contract.
 *
 *   OUT (board to Mac), on input change or every HEARTBEAT_MS:
 *     JZ1 <ax> <ay> <sw> <atk> <sens>
 *
 *   IN (Mac to board), on either transport:
 *     LED <green0_1> <yellow0_1> <red0_1>
 *     POL <0|1>
 *     RAW <pin> <val>            val 2 = high impedance
 *
 *   IN (USB serial only, never over TCP):
 *     WIFI <ssid> <password>
 *     HOST <ip> <port>
 *     KEEP <on> <period_ms> <burst_ms>
 *     NET
 *     SAVE
 *
 * Why SPP was abandoned: it never established RFCOMM on macOS. The port opened
 * in about 1 ms and delivered zero bytes while the board reported no client.
 * Even working it was a dead end, because the game now runs in a phone browser
 * and Web Bluetooth is BLE GATT only, which classic SPP cannot speak.
 *
 * Why raw TCP and not a websocket: the protocol is already newline delimited,
 * so it maps 1:1 onto a byte stream with no framing layer, no dependency and
 * less latency.
 *
 * Threading. loop() runs on core 1 and does nothing but sample input and write
 * frames. All network work, which can block, runs on netTask pinned to core 0.
 * The joystick therefore keeps being read at full rate while WiFi is down, and
 * USB serial keeps working regardless. Every WiFiClient touch is behind
 * netMutex, because the two cores really do race otherwise.
 */

#include <WiFi.h>
#include <Preferences.h>

// ---------------------------------------------------------------- pin mapping
/*
 * Measured on the bench, not assumed: pushing the stick horizontally moves the
 * pin on GPIO 33 and pushing it vertically moves GPIO 32. The module is wired
 * with its axes crossed relative to the silkscreen, so the mapping is corrected
 * here, at the source, rather than being worked around on the Mac.
 */
const int PIN_JOY_X   = 33;   // ADC1_CH5, horizontal
const int PIN_JOY_Y   = 32;   // ADC1_CH4, vertical

/*
 * Full up reads 4095, but the game wants up to be negative: screen y grows
 * downward, so KEY_UP yields -1 and the stick has to agree with the keyboard.
 */
const bool INVERT_Y = true;
const int PIN_JOY_SW  = 27;   // INPUT_PULLUP, pressed = LOW
const int PIN_ATTACK  = 13;   // INPUT_PULLUP, pressed = LOW
const int PIN_SENS    = 23;   // sensitivity input

/*
 * Lamp pins, corrected 2026-09-19 after checking the board by eye.
 *
 * These used to read 18=yellow, 19=green, 21=red. That is not how this unit is
 * wired. Driving each pin alone with digitalWrite and naming the colour that
 * lit gives 19=yellow, 18=red, 21=green, a three way rotation.
 *
 * The bug was invisible in operation and inverted the meter in the worst way:
 * a quiet room lit yellow, a medium room lit red, and shouting lit green. Do
 * not let it come back.
 */
const int PIN_LED_RED    = 18;  // regenerating
const int PIN_LED_YELLOW = 19;  // hostility
const int PIN_LED_GREEN  = 21;  // health

/*
 * GPIO 23 is NOT an ADC pin on the ESP32. ADC2 is also unusable while the radio
 * is on, which rules out most of the remaining pins here. So the sensitivity
 * input is read as a digital toggle rather than a pot, and reported as one of
 * two ADC-shaped values so the existing wire format and the Mac side parser do
 * not change. If you want a real analog pot later, move it to GPIO 34/35/36/39
 * (ADC1, input only) and set SENS_IS_ANALOG to 1.
 */
#define SENS_IS_ANALOG 0

// ------------------------------------------------------------------ behaviour
const unsigned long SAMPLE_MS     = 8;     // ~125 Hz sampling
const unsigned long HEARTBEAT_MS  = 500;   // liveness even when nothing moves
const unsigned long DEBOUNCE_MS   = 25;
const unsigned long DEBUG_MS      = 100;   // USB debug line cadence

// Emit only when an axis moves at least this much, in raw ADC counts. Measured
// resting jitter is 11 counts horizontal and 12 vertical, so 18 sits 1.5x above
// the noise floor and finer than the Mac side deadzone of about 102 counts.
const int AXIS_EMIT_DELTA = 18;

// TCP reconnect backoff. Starts gentle so a brief glitch recovers fast, and
// tops out so a long outage does not hammer the radio flat.
const unsigned long BACKOFF_MIN_MS = 1000;
const unsigned long BACKOFF_MAX_MS = 30000;

// Bound how long a single connect attempt may stall netTask. loop() is on the
// other core and is unaffected either way, but a shorter stall means a faster
// return to retrying.
const int TCP_CONNECT_TIMEOUT_MS = 800;

/*
 * How long to leave an association attempt alone before re-issuing it.
 *
 * Association routinely takes several seconds, and the TCP backoff starts at
 * 1 s. Driving WiFi.begin() from the TCP schedule therefore interrupted each
 * attempt before it could finish, and the core answered with
 * "E wifi:sta is connecting, cannot set config" on every retry. WiFi retry and
 * TCP retry are different problems on different timescales, so they now have
 * separate clocks.
 */
const unsigned long WIFI_REASSOC_MS = 15000;

// ------------------------------------------------------- boot time calibration
/*
 * The stick is on 3.3V, so full scale is not the 2048 midpoint you would get
 * from a 5V rail divided onto a 0..4095 range. Rather than hardcode a centre,
 * sample the resting position at boot and treat that as zero. This also cancels
 * per unit variation and any slight mechanical offset in the gimbal.
 *
 * Hold the stick still during the first second after reset.
 */
int centreX = 2048;
int centreY = 2048;
int restNoiseX = 0;
int restNoiseY = 0;
int minX, maxX, minY, maxY;

// --------------------------------------------------------------------- state
int lastAx = -1, lastAy = -1, lastSens = -1;
int lastSw = -1, lastAtk = -1;
unsigned long lastSendMs = 0;
unsigned long swChangeMs = 0, atkChangeMs = 0;
int swStable = 1, atkStable = 1;      // 1 = released (pullup idle high)
int swReading = 1, atkReading = 1;

// ------------------------------------------------------------------- network
Preferences prefs;
WiFiClient tcp;
SemaphoreHandle_t netMutex;

String cfgSsid = "";
String cfgPass = "";
String cfgHost = "192.168.1.10";
uint16_t cfgPort = 3333;

// Written by netTask on core 0, read by loop() on core 1. Single word reads and
// writes on this architecture are atomic, and a stale read costs one debug line.
volatile bool wifiUp = false;
volatile bool tcpUp = false;
volatile int  lastRssi = 0;
volatile unsigned long curBackoff = BACKOFF_MIN_MS;

// Power bank keep-alive. See PROTOCOL.md section 7 for why this exists at all.
// Set from loop(), serviced on netTask. Scanning blocks for seconds, so it
// must not happen on the core that samples the joystick.
volatile bool scanRequested = false;

volatile bool keepOn = true;
volatile unsigned long keepPeriodMs = 6000;
volatile unsigned long keepBurstMs = 250;

bool wasTcpUp = false;

// --------------------------------------------------------------------- helpers

int readAveraged(int pin, int n = 8) {
  long total = 0;
  for (int i = 0; i < n; i++) {
    total += analogRead(pin);
    delayMicroseconds(60);
  }
  return (int)(total / n);
}

void calibrateCentre() {
  // Discard the first reads: the ADC settles for a moment after boot.
  for (int i = 0; i < 16; i++) { analogRead(PIN_JOY_X); analogRead(PIN_JOY_Y); delay(2); }

  long sx = 0, sy = 0;
  int lox = 4095, hix = 0, loy = 4095, hiy = 0;
  const int N = 64;
  for (int i = 0; i < N; i++) {
    int x = analogRead(PIN_JOY_X);
    int y = analogRead(PIN_JOY_Y);
    sx += x; sy += y;
    if (x < lox) lox = x;  if (x > hix) hix = x;
    if (y < loy) loy = y;  if (y > hiy) hiy = y;
    delay(4);
  }
  centreX = (int)(sx / N);
  centreY = (int)(sy / N);
  restNoiseX = hix - lox;
  restNoiseY = hiy - loy;

  minX = maxX = centreX;
  minY = maxY = centreY;
}

/*
 * Re-centre a raw reading so the resting position maps to 2048, then rescale
 * each half of the travel independently.
 *
 * Independent halves matter: a 3.3V stick is rarely symmetric, and if you scale
 * both directions by one factor the axis reaches full travel one way and stops
 * short the other. The Mac side applies its own deadzone, so this stage
 * deliberately does no deadzoning, it only linearises.
 */
int normaliseAxis(int raw, int centre, int lo, int hi) {
  long out;
  if (raw >= centre) {
    int span = hi - centre;
    if (span < 200) span = 2047;        // not yet pushed that far, assume full
    out = 2048L + (long)(raw - centre) * 2047L / span;
  } else {
    int span = centre - lo;
    if (span < 200) span = 2048;
    out = 2048L - (long)(centre - raw) * 2048L / span;
  }
  if (out < 0) out = 0;
  if (out > 4095) out = 4095;
  return (int)out;
}

int readSensitivity() {
#if SENS_IS_ANALOG
  return readAveraged(PIN_SENS, 4);
#else
  // Digital toggle reported in ADC units: released is 1.0x, pressed is ~1.75x.
  return digitalRead(PIN_SENS) == LOW ? 4095 : 2048;
#endif
}

/*
 * LED wiring polarity, overridable at runtime with the POL command.
 *
 * This board: each LED has its cathode on GND and its anode on a GPIO through a
 * 220 ohm resistor. That is ACTIVE HIGH, the pin sources current and HIGH
 * lights the lamp.
 */
bool ledActiveLow = false;

void writeLamp(int pin, float v) {
  int duty = (int)(constrain(v, 0.0f, 1.0f) * 255.0f);
  analogWrite(pin, ledActiveLow ? 255 - duty : duty);
}

void applyLeds(float green, float yellow, float red) {
  writeLamp(PIN_LED_GREEN, green);
  writeLamp(PIN_LED_YELLOW, yellow);
  writeLamp(PIN_LED_RED, red);
}

void ledBootFlash() {
  int pins[3] = {PIN_LED_GREEN, PIN_LED_YELLOW, PIN_LED_RED};
  for (int r = 0; r < 2; r++) {
    for (int i = 0; i < 3; i++) {
      writeLamp(pins[i], 1.0f); delay(90); writeLamp(pins[i], 0.0f);
    }
  }
}

// ------------------------------------------------------------------ config IO

void loadConfig() {
  prefs.begin("jz", false);
  cfgSsid = prefs.getString("ssid", "");
  cfgPass = prefs.getString("pass", "");
  cfgHost = prefs.getString("host", "192.168.1.10");
  cfgPort = prefs.getUShort("port", 3333);
  keepOn       = prefs.getBool("keepon", true);
  keepPeriodMs = prefs.getULong("keepper", 6000);
  keepBurstMs  = prefs.getULong("keepbur", 250);
}

void saveConfig() {
  prefs.putString("ssid", cfgSsid);
  prefs.putString("pass", cfgPass);
  prefs.putString("host", cfgHost);
  prefs.putUShort("port", cfgPort);
  prefs.putBool("keepon", keepOn);
  prefs.putULong("keepper", keepPeriodMs);
  prefs.putULong("keepbur", keepBurstMs);
}

/*
 * Never prints the password. This output is read by humans and by test scripts,
 * and a credential in a log is a credential on disk forever.
 */
void printNet() {
  Serial.printf("NET ssid=%s\n", cfgSsid.length() ? cfgSsid.c_str() : "-");
  Serial.printf("NET wifi=%d ip=%s rssi=%d\n",
                wifiUp ? 1 : 0,
                wifiUp ? WiFi.localIP().toString().c_str() : "-",
                wifiUp ? (int)WiFi.RSSI() : 0);
  Serial.printf("NET host=%s port=%u tcp=%d\n", cfgHost.c_str(), cfgPort, tcpUp ? 1 : 0);
  Serial.printf("NET backoff=%lu\n", curBackoff);
  Serial.printf("NET keepalive=%d period=%lu burst=%lu\n",
                keepOn ? 1 : 0, keepPeriodMs, keepBurstMs);
  Serial.println("NET END");
}

// --------------------------------------------------------------- line handling

/*
 * fromUsb gates the configuration commands. They are deliberately unreachable
 * over TCP: nothing on a venue network should be able to rewrite where this
 * board connects, and the lamps are the only surface worth exposing.
 */
void applyLine(const char* line, bool fromUsb, Stream* origin) {
  float g = 0, y = 0, r = 0;
  int pol = 0;

  if (sscanf(line, "LED %f %f %f", &g, &y, &r) == 3) {
    applyLeds(g, y, r);
    return;
  }
  if (strncmp(line, "RAW ", 4) == 0) {
    // Diagnostic: drive one pin with plain digitalWrite, bypassing PWM.
    // Distinguishes "LED or wiring is dead" from "analogWrite is not driving
    // this pin", which look identical from the Mac. Also the only acknowledged
    // command, which makes it the supported way to time a round trip.
    int pin = -1, val = 0;
    if (sscanf(line, "RAW %d %d", &pin, &val) == 2) {
      if (pin == PIN_LED_GREEN || pin == PIN_LED_YELLOW || pin == PIN_LED_RED) {
        if (val == 2) {
          pinMode(pin, INPUT);
          // Echo to whichever transport asked. A round trip can only be timed
          // on the link under test, so a TCP request must be answered on TCP.
          origin->printf("RAW pin %d -> HI-Z\n", pin);
          if (!fromUsb) Serial.printf("RAW pin %d -> HI-Z\n", pin);
        } else {
          pinMode(pin, OUTPUT);
          digitalWrite(pin, val ? HIGH : LOW);
          origin->printf("RAW pin %d -> %s\n", pin, val ? "HIGH" : "LOW");
          if (!fromUsb) Serial.printf("RAW pin %d -> %s\n", pin, val ? "HIGH" : "LOW");
        }
      }
    }
    return;
  }
  if (sscanf(line, "POL %d", &pol) == 1) {
    ledActiveLow = (pol != 0);
    applyLeds(1, 1, 1); delay(150); applyLeds(0, 0, 0);
    Serial.printf("polarity: active %s\n", ledActiveLow ? "LOW" : "HIGH");
    return;
  }

  if (!fromUsb) return;   // everything below is USB only, on purpose

  /*
   * SSID and PASS take the rest of the line verbatim, so a name with spaces in
   * it works. "Pitt Guest" is exactly that case, and the space separated WIFI
   * form below cannot express it at all. WIFI is kept for the common case and
   * for scripts that already use it.
   */
  if (strncmp(line, "SSID ", 5) == 0) {
    if (xSemaphoreTake(netMutex, pdMS_TO_TICKS(200)) == pdTRUE) {
      cfgSsid = String(line + 5);
      cfgSsid.trim();
      xSemaphoreGive(netMutex);
    }
    saveConfig();
    Serial.printf("ssid set to [%s], reassociating\n", cfgSsid.c_str());
    WiFi.disconnect(false);
    wifiUp = false;
    return;
  }
  if (strncmp(line, "PASS", 4) == 0 && (line[4] == ' ' || line[4] == '\0')) {
    String p = String(line + 4);
    p.trim();
    if (xSemaphoreTake(netMutex, pdMS_TO_TICKS(200)) == pdTRUE) {
      cfgPass = (p == "-") ? String("") : p;
      xSemaphoreGive(netMutex);
    }
    saveConfig();
    Serial.printf("password set, security=%s, reassociating\n",
                  cfgPass.length() ? "psk" : "open");
    WiFi.disconnect(false);
    wifiUp = false;
    return;
  }

  if (strcmp(line, "SCAN") == 0) { scanRequested = true; Serial.println("scan queued"); return; }
  if (strcmp(line, "NET") == 0) { printNet(); return; }
  if (strcmp(line, "SAVE") == 0) { saveConfig(); Serial.println("saved"); return; }

  char a[64], b[64];
  int nw = sscanf(line, "WIFI %63s %63s", a, b);
  if (nw >= 1) {
    // One token means an open network. "-" as the password means the same
    // thing, so a script can always send a fixed three token line.
    if (xSemaphoreTake(netMutex, pdMS_TO_TICKS(200)) == pdTRUE) {
      cfgSsid = a;
      cfgPass = (nw == 2 && strcmp(b, "-") != 0) ? String(b) : String("");
      xSemaphoreGive(netMutex);
    }
    saveConfig();
    Serial.printf("wifi config set, ssid=%s, security=%s, reassociating\n",
                  cfgSsid.c_str(), cfgPass.length() ? "psk" : "open");
    WiFi.disconnect(true);
    wifiUp = false;
    return;
  }
  int port = 0;
  if (sscanf(line, "HOST %63s %d", a, &port) == 2 && port > 0 && port < 65536) {
    cfgHost = a; cfgPort = (uint16_t)port;
    saveConfig();
    Serial.printf("host set to %s:%u, reconnecting\n", cfgHost.c_str(), cfgPort);
    if (xSemaphoreTake(netMutex, pdMS_TO_TICKS(200)) == pdTRUE) {
      tcp.stop();
      tcpUp = false;
      xSemaphoreGive(netMutex);
    }
    return;
  }
  int on = 0; unsigned long per = 0, bur = 0;
  if (sscanf(line, "KEEP %d %lu %lu", &on, &per, &bur) == 3) {
    keepOn = (on != 0);
    if (per >= 500)  keepPeriodMs = per;
    if (bur >= 10 && bur < per) keepBurstMs = bur;
    saveConfig();
    Serial.printf("keepalive on=%d period=%lu burst=%lu\n",
                  keepOn ? 1 : 0, keepPeriodMs, keepBurstMs);
    return;
  }
  // Unrecognised lines are ignored in silence, which is what keeps the protocol
  // forward compatible.
}

void handleIncomingFrom(Stream &io, char *buf, int &n, int cap, bool fromUsb) {
  while (io.available()) {
    char c = (char)io.read();
    if (c == '\n' || c == '\r') {
      if (n > 0) { buf[n] = '\0'; applyLine(buf, fromUsb, &io); n = 0; }
    } else if (n < cap - 1) {
      buf[n++] = c;
    } else {
      n = 0;  // overlong line, drop it rather than wrap
    }
  }
}

void handleIncoming() {
  static char usbBuf[64]; static int usbN = 0;
  static char netBuf[64]; static int netN = 0;
  handleIncomingFrom(Serial, usbBuf, usbN, sizeof(usbBuf), true);
  if (tcpUp) {
    if (xSemaphoreTake(netMutex, 0) == pdTRUE) {
      if (tcp.connected()) handleIncomingFrom(tcp, netBuf, netN, sizeof(netBuf), false);
      xSemaphoreGive(netMutex);
    }
  }
}

// --------------------------------------------------------------- network task

/*
 * Everything that can block lives here, on core 0. loop() never waits on the
 * radio, so the joystick keeps being sampled and USB keeps working at full rate
 * with WiFi down. That is requirement 5, and it is the reason for the mutex.
 */
void netTask(void*) {
  unsigned long nextAttempt = 0;      // TCP dial schedule
  unsigned long nextWifiTry = 0;      // association schedule, deliberately separate
  for (;;) {
    unsigned long now = millis();

    /*
     * A scan from the board itself is the only authoritative answer to "can
     * this radio see that network". The ESP32 is 2.4 GHz only, so anything
     * absent from this list is unreachable no matter what the Mac can see.
     */
    if (scanRequested) {
      scanRequested = false;
      Serial.println("SCAN start (2.4 GHz only, this radio has no 5 GHz)");
      // A scan started while the station is mid association returns -2,
      // WIFI_SCAN_FAILED. Quiesce the radio first and hold off the retry
      // clock, otherwise the two fight and the scan never succeeds.
      WiFi.disconnect(false);
      vTaskDelay(pdMS_TO_TICKS(400));
      int n = WiFi.scanNetworks(false, true);
      if (n < 0) {
        vTaskDelay(pdMS_TO_TICKS(800));
        n = WiFi.scanNetworks(false, true);
      }
      Serial.printf("SCAN found %d networks\n", n);
      for (int i = 0; i < n; i++) {
        Serial.printf("SCAN %2d ch=%2d rssi=%4d enc=%d ssid=[%s]\n",
                      i, WiFi.channel(i), (int)WiFi.RSSI(i),
                      (int)WiFi.encryptionType(i), WiFi.SSID(i).c_str());
      }
      WiFi.scanDelete();
      Serial.println("SCAN END");
      nextWifiTry = millis() + 1500;   // let association resume promptly
    }

    bool assoc = (WiFi.status() == WL_CONNECTED);
    wifiUp = assoc;
    if (assoc) lastRssi = WiFi.RSSI();

    if (!assoc) {
      tcpUp = false;
      // Copy the config out under the mutex, then let go before touching the
      // radio. cfgSsid is a String written from loop() on the other core, so
      // reading c_str() while it reallocates is a genuine crash. Holding the
      // mutex across WiFi.begin would instead stall the input loop, which
      // requirement 5 forbids, so a copy is the only safe shape.
      String ssidCopy, passCopy;
      if (xSemaphoreTake(netMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
        ssidCopy = cfgSsid;
        passCopy = cfgPass;
        xSemaphoreGive(netMutex);
      }
      if (ssidCopy.length() && now >= nextWifiTry) {
        Serial.printf("wifi: associating with [%s] (%s)\n",
                      ssidCopy.c_str(), passCopy.length() ? "psk" : "open");
        // Cancel any half finished attempt first, otherwise the core refuses
        // the new config outright and we retry forever against a busy radio.
        WiFi.disconnect(false);
        vTaskDelay(pdMS_TO_TICKS(120));
        // A NULL passphrase, not an empty string, is what selects open auth.
        WiFi.begin(ssidCopy.c_str(), passCopy.length() ? passCopy.c_str() : NULL);
        nextWifiTry = now + WIFI_REASSOC_MS;
      }
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }

    bool up = false;
    if (xSemaphoreTake(netMutex, pdMS_TO_TICKS(50)) == pdTRUE) {
      up = tcp.connected();
      xSemaphoreGive(netMutex);
    }

    if (up) {
      curBackoff = BACKOFF_MIN_MS;
      tcpUp = true;
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }

    tcpUp = false;
    if (now < nextAttempt) { vTaskDelay(pdMS_TO_TICKS(50)); continue; }

    if (xSemaphoreTake(netMutex, pdMS_TO_TICKS(200)) == pdTRUE) {
      tcp.stop();
      tcp.setTimeout(1);
      bool ok = tcp.connect(cfgHost.c_str(), cfgPort, TCP_CONNECT_TIMEOUT_MS);
      if (ok) {
        // Nagle would coalesce these tiny lines and add tens of milliseconds
        // for no benefit whatsoever.
        tcp.setNoDelay(true);
        tcpUp = true;
        curBackoff = BACKOFF_MIN_MS;
        Serial.printf("tcp: connected to %s:%u\n", cfgHost.c_str(), cfgPort);
      } else {
        Serial.printf("tcp: connect to %s:%u failed, retry in %lu ms\n",
                      cfgHost.c_str(), cfgPort, curBackoff);
        nextAttempt = now + curBackoff;
        curBackoff = min(curBackoff * 2, BACKOFF_MAX_MS);
      }
      xSemaphoreGive(netMutex);
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ------------------------------------------------------------ keep-alive task

/*
 * The handheld runs from a 10000 mAh USB power bank. Power banks shut down when
 * draw falls below their threshold, and a measured test had this one cutting
 * out while the board idled. Runtime is not the constraint here: at roughly
 * 200 mA the bank lasts about 30 hours against a demo window of two, so the
 * cheap fix is simply to make sure the board never looks idle to the bank.
 *
 * A burst rather than a permanent spin, because a permanent spin costs the same
 * to the bank and far more to the battery. Pinned to core 0 so it can never
 * delay a sample or add a latency spike on core 1.
 *
 * While TCP is down the burst also blinks green. On battery there is no data
 * link at all, so that blink is the only way to tell a live board from a dead
 * one without plugging it into something.
 */
void keepAliveTask(void*) {
  for (;;) {
    if (!keepOn) { vTaskDelay(pdMS_TO_TICKS(500)); continue; }

    bool beacon = !tcpUp;
    if (beacon) writeLamp(PIN_LED_GREEN, 1.0f);

    unsigned long until = millis() + keepBurstMs;
    volatile double acc = 1.0;
    while (millis() < until) {
      for (int i = 0; i < 2000; i++) acc = acc * 1.000001 + 0.000001;
      if (acc > 1e9) acc = 1.0;
    }

    if (beacon) writeLamp(PIN_LED_GREEN, 0.0f);

    unsigned long gap = keepPeriodMs > keepBurstMs ? keepPeriodMs - keepBurstMs : 500;
    vTaskDelay(pdMS_TO_TICKS(gap));
  }
}

// ------------------------------------------------------------------- lifecycle

void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(PIN_JOY_SW, INPUT_PULLUP);
  pinMode(PIN_ATTACK, INPUT_PULLUP);
#if !SENS_IS_ANALOG
  pinMode(PIN_SENS, INPUT_PULLUP);
#endif
  pinMode(PIN_LED_YELLOW, OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_LED_RED, OUTPUT);

  analogReadResolution(12);                  // 0..4095
  analogSetPinAttenuation(PIN_JOY_X, ADC_11db);   // full 0..3.3V swing
  analogSetPinAttenuation(PIN_JOY_Y, ADC_11db);

  ledBootFlash();

  Serial.println();
  Serial.println("jenzombies controller v2 (WiFi/TCP): calibrating centre, hold the stick still");
  calibrateCentre();
  Serial.printf("centre  X=%d Y=%d   rest noise  X=%d Y=%d\n",
                centreX, centreY, restNoiseX, restNoiseY);

  netMutex = xSemaphoreCreateMutex();
  loadConfig();

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  // Keeping the radio awake costs current we have in abundance and buys back
  // the latency that modem sleep would otherwise add to every frame.
  WiFi.setSleep(false);

  xTaskCreatePinnedToCore(netTask, "net", 4096, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(keepAliveTask, "keepalive", 2048, NULL, 1, NULL, 0);

  if (cfgSsid.length() == 0) {
    Serial.println("no wifi credentials stored. set them with: WIFI <ssid> <password>");
  }
  printNet();
}

void loop() {
  unsigned long now = millis();

  bool up = tcpUp;
  if (up != wasTcpUp) {
    wasTcpUp = up;
    Serial.println(up ? "tcp client up" : "tcp client gone");
    if (!up) {
      // Drop the lamps so the board does not sit lit after the game closes.
      applyLeds(0, 0, 0);
      lastAx = lastAy = lastSens = lastSw = lastAtk = -1;  // force a full resend
    }
  }

  handleIncoming();

  static unsigned long lastSample = 0;
  if (now - lastSample < SAMPLE_MS) return;
  lastSample = now;

  int rawX = readAveraged(PIN_JOY_X, 4);
  int rawY = readAveraged(PIN_JOY_Y, 4);
  if (rawX < minX) minX = rawX;
  if (rawX > maxX) maxX = rawX;
  if (rawY < minY) minY = rawY;
  if (rawY > maxY) maxY = rawY;

  int ax = normaliseAxis(rawX, centreX, minX, maxX);
  int ay = normaliseAxis(rawY, centreY, minY, maxY);
  if (INVERT_Y) ay = 4095 - ay;

  // Debounce both buttons. INPUT_PULLUP means LOW is pressed, so invert here
  // and let the rest of the system speak in 1 = pressed.
  int swNow  = digitalRead(PIN_JOY_SW);
  int atkNow = digitalRead(PIN_ATTACK);
  if (swNow != swReading)  { swReading = swNow;  swChangeMs = now; }
  if (atkNow != atkReading) { atkReading = atkNow; atkChangeMs = now; }
  if (now - swChangeMs  > DEBOUNCE_MS) swStable  = swReading;
  if (now - atkChangeMs > DEBOUNCE_MS) atkStable = atkReading;

  int sw  = (swStable  == LOW) ? 1 : 0;
  int atk = (atkStable == LOW) ? 1 : 0;
  int sens = readSensitivity();

  bool changed =
      (lastAx < 0) ||
      (abs(ax - lastAx) >= AXIS_EMIT_DELTA) ||
      (abs(ay - lastAy) >= AXIS_EMIT_DELTA) ||
      (sw != lastSw) || (atk != lastAtk) ||
      (abs(sens - lastSens) >= 200);

  bool heartbeat = (now - lastSendMs) >= HEARTBEAT_MS;

  /*
   * Emit on BOTH transports. The USB line costs nothing when unused and is the
   * transport that cannot silently fail, so it is always live. The Mac side
   * parses JZ1 identically from either path.
   *
   * Worth remembering at the venue: USB power and USB data share one connector
   * on this board, so running from the power bank means there is no USB host.
   * USB is a bench transport, not a demo fallback.
   */
  if (changed || heartbeat) {
    if (tcpUp && xSemaphoreTake(netMutex, 0) == pdTRUE) {
      if (tcp.connected()) tcp.printf("JZ1 %d %d %d %d %d\n", ax, ay, sw, atk, sens);
      xSemaphoreGive(netMutex);
    }
    Serial.printf("JZ1 %d %d %d %d %d\n", ax, ay, sw, atk, sens);
    lastAx = ax; lastAy = ay; lastSw = sw; lastAtk = atk; lastSens = sens;
    lastSendMs = now;
  }

  // Same line to USB serial so every control can be verified with no Mac side
  // and no network in the picture. The wifi/tcp/rssi fields replace v1's bt=
  // field and serve the same purpose: link state as the board sees it, not as
  // the host imagines it. rssi is the field to watch during a range test.
  static unsigned long lastDbg = 0;
  if (now - lastDbg >= DEBUG_MS) {
    lastDbg = now;
    Serial.printf("RAW x=%4d y=%4d (centre %4d/%4d) -> ax=%4d ay=%4d sw=%d atk=%d sens=%d wifi=%d tcp=%d rssi=%d\n",
                  rawX, rawY, centreX, centreY, ax, ay, sw, atk, sens,
                  wifiUp ? 1 : 0, tcpUp ? 1 : 0, (int)lastRssi);
  }
}

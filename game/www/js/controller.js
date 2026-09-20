// ESP32 handheld input over Server-Sent Events (GET /events on the same origin).
//
// The bridge on the Pi already parses the wire protocol, applies the deadzone
// and emits JSON:  {"t":"state","x":-1..1,"y":-1..1,"sw":0|1,"atk":0|1,
//                   "sens":2048|4095,"connected":bool,"seq":n,"ts":s}
// at ~2 Hz idle (heartbeat) and up to ~125 Hz while the stick moves.
//
// Contract with the rest of the game:
//   * ADDITIVE. Touch keeps working whether or not a handheld is attached and
//     nothing in here ever throws into the frame loop. No controller => every
//     poll() is neutral with no edges, and the page behaves exactly as before.
//   * `connected` means frames are ARRIVING: the bridge's own flag AND an event
//     inside staleMs. A half-open link that stops sending reads as gone, and on
//     disconnect the stick and buttons go NEUTRAL, not last-known (PROTOCOL.md 5).
//   * Buttons are LATCHED between polls, so a tap shorter than one frame at
//     125 Hz input / ~90 fps render is never lost.
//   * `sens` is a two-position MOMENTARY button (2048 released, 4095 pressed),
//     not a knob and not a latching switch. `sensPressed` is the press EDGE,
//     which is what the game acts on: one press steps one sensitivity preset
//     and it stays there, so the button never has to be held and releasing it
//     does not undo anything. `sensChanged`/`sens` still report the raw
//     position, for the dashboard.
//   * The stick has no locomotion job (the player walks). It is exposed as raw
//     axes plus FLICK edges (push past FLICK_ON, re-arm inside FLICK_OFF).
//
// Free of DOM and WebXR so it runs under node for test_controller.mjs.

export const SENS_NORMAL = 2048;
export const SENS_HIGH = 4095;
// At or above this, the button is being held down.
export const SENS_PRESS_LEVEL = 3000;
export const isSensDown = raw => Number(raw) >= SENS_PRESS_LEVEL;
// Contact bounce on a real button can cross the threshold several times in
// a few milliseconds. Each bounce would step the sensitivity again, so a
// press is ignored if it follows the last one this closely. Nobody presses
// a sensitivity button six times a second on purpose.
export const SENS_DEBOUNCE_MS = 150;
export const FLICK_ON = 0.6;
export const FLICK_OFF = 0.3;
export const LINK = { IDLE: 'idle', CONNECTING: 'connecting', OPEN: 'open',
                      CLOSED: 'closed', UNSUPPORTED: 'unsupported' };

const defaultNow = () => (typeof performance !== 'undefined' && performance.now)
  ? performance.now() : Date.now();
const num = (v, d = 0) => (Number.isFinite(v) ? v : d);
const clamp1 = v => Math.max(-1, Math.min(1, num(v)));

export class Controller {
  // Reconnect quickly: the common reason this link dies is the Pi being
  // power-cycled, and it is back inside a minute. A 30 s ceiling meant the
  // game could sit there controller-less long after the bridge returned.
  constructor({ url = '/events', staleMs = 1500, reconnectMs = 1500, maxReconnectMs = 8000,
                now = defaultNow,
                EventSourceImpl = (typeof EventSource !== 'undefined' ? EventSource : null),
                log = () => {}, warn = () => {} } = {}) {
    this.url = url;
    this.staleMs = staleMs;
    this.reconnectMs = reconnectMs;
    this.maxReconnectMs = maxReconnectMs;
    this.now = now;
    this.ES = EventSourceImpl;
    this.log = log; this.warn = warn;

    this.link = LINK.IDLE;
    this.es = null;
    this._timer = null;
    this._backoff = reconnectMs;

    // live state
    this.x = 0; this.y = 0; this.sw = 0; this.atk = 0; this.sens = SENS_NORMAL;
    this.seq = 0;
    this.bridgeConnected = false;   // what the bridge last said
    this.lastEventAt = -Infinity;
    this.frames = 0;                // state messages received
    this.drops = 0;                 // live -> gone transitions
    this._live = false;             // what the last poll() reported

    this._resetLatches();
  }

  _resetLatches() {
    this._atkLatch = false; this._swLatch = false; this._sensLatch = null;
    this._sensPressLatch = false;
    this._xFlick = 0; this._yFlick = 0; this._xArmed = true; this._yArmed = true;
  }
  _neutralise() {
    this.x = 0; this.y = 0; this.sw = 0; this.atk = 0;     // sens is a switch: keep it
    this._resetLatches();
  }

  /** Frames are arriving and the bridge says the board is streaming. */
  isConnected(now = this.now()) {
    return this.bridgeConnected && this.frames > 0 && (now - this.lastEventAt) <= this.staleMs;
  }
  get connected() { return this.isConnected(); }

  /** Feed one bridge message. Safe with junk; never throws. */
  apply(m, now = this.now()) {
    try {
      if (!m || m.t !== 'state') return false;
      const wasLive = this.isConnected(now);
      this.frames++;
      this.lastEventAt = now;
      this.seq = num(m.seq, this.seq);
      this.bridgeConnected = !!m.connected;
      if (!this.bridgeConnected) { this._neutralise(); return true; }

      const x = clamp1(m.x), y = clamp1(m.y);
      const sw = m.sw ? 1 : 0, atk = m.atk ? 1 : 0;
      const sens = num(m.sens, this.sens);
      if (!wasLive) {
        // Link (re)established: adopt the switch position; a button or stick
        // already held is state, not an action. Flicks arm once the stick is
        // back inside the release band.
        this._resetLatches();
        this._sensLatch = sens;
        this._sensDown = isSensDown(sens);       // held at connect is not a press
        this._xArmed = Math.abs(x) < FLICK_OFF;
        this._yArmed = Math.abs(y) < FLICK_OFF;
      } else {
        if (atk && !this.atk) this._atkLatch = true;
        if (sw && !this.sw) this._swLatch = true;
        if (sens !== this.sens) this._sensLatch = sens;
        const down = isSensDown(sens);
        if (down && !this._sensDown) {                              // the edge
          if (now - (this._lastSensPressAt || -Infinity) >= SENS_DEBOUNCE_MS) {
            this._sensPressLatch = true;
            this._lastSensPressAt = now;
          } else {
            this.sensBounces = (this.sensBounces || 0) + 1;
          }
        }
        this._sensDown = down;
        if (this._xArmed && Math.abs(x) >= FLICK_ON) { this._xFlick = x > 0 ? 1 : -1; this._xArmed = false; }
        else if (!this._xArmed && Math.abs(x) < FLICK_OFF) this._xArmed = true;
        if (this._yArmed && Math.abs(y) >= FLICK_ON) { this._yFlick = y > 0 ? 1 : -1; this._yArmed = false; }
        else if (!this._yArmed && Math.abs(y) < FLICK_OFF) this._yArmed = true;
      }
      this.x = x; this.y = y; this.sw = sw; this.atk = atk; this.sens = sens;
      return true;
    } catch (e) {
      this.warn('controller apply (ignored):', e);
      return false;
    }
  }

  /**
   * Once per frame. Returns the current input plus the edges that happened
   * since the previous poll, then clears them. Neutral with no edges whenever
   * the handheld is not live, so callers need no special case for "no pad".
   */
  poll(now = this.now()) {
    const live = this.isConnected(now);
    const out = {
      connected: live,
      x: live ? this.x : 0, y: live ? this.y : 0,
      atk: live ? this.atk : 0, sw: live ? this.sw : 0,
      sens: this.sens,
      atkPressed: live && this._atkLatch,
      swPressed: live && this._swLatch,
      sensChanged: live && this._sensLatch !== null,
      sensPressed: live && this._sensPressLatch,
      xFlick: live ? this._xFlick : 0,
      yFlick: live ? this._yFlick : 0,
      restored: live && !this._live,
      dropped: !live && this._live,
    };
    if (!live && this._live) {
      this.drops++;
      this._neutralise();               // stale or bridge-reported: same outcome
    }
    this._live = live;
    this._atkLatch = false; this._swLatch = false; this._sensLatch = null;
    this._sensPressLatch = false;
    this._xFlick = 0; this._yFlick = 0;
    return out;
  }

  /** Snapshot for telemetry / the dashboard. */
  status(now = this.now()) {
    return {
      link: this.link, connected: this.isConnected(now),
      x: +this.x.toFixed(3), y: +this.y.toFixed(3), atk: this.atk, sw: this.sw, sens: this.sens,
      frames: this.frames, seq: this.seq, drops: this.drops, bounces: this.sensBounces || 0,
      ageMs: this.frames ? Math.round(now - this.lastEventAt) : null,
    };
  }

  /** Open the SSE link. Returns false (and stays inert) where EventSource is missing. */
  start() {
    if (!this.ES) { this.link = LINK.UNSUPPORTED; return false; }
    this._open();
    return true;
  }

  _open() {
    if (this.es) return;
    this.link = LINK.CONNECTING;
    let es;
    try { es = new this.ES(this.url); } catch (e) {
      this.warn('EventSource failed:', e); this.link = LINK.CLOSED; this._scheduleReopen(); return;
    }
    this.es = es;
    es.onopen = () => { this.link = LINK.OPEN; this._backoff = this.reconnectMs; };
    es.onmessage = ev => {
      let m = null;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      this.apply(m, this.now());
    };
    es.onerror = () => {
      // EventSource retries a dropped stream by itself; a 404/refused endpoint
      // (the Mac fallback server has no /events) closes it for good, and we
      // reopen with backoff so a bridge that appears later is picked up.
      this.bridgeConnected = false;
      this._neutralise();
      if (es.readyState === 2 /* CLOSED */) {
        this.link = LINK.CLOSED;
        this.es = null;
        this._scheduleReopen();
      } else {
        this.link = LINK.CONNECTING;
      }
    };
  }

  _scheduleReopen() {
    if (this._timer) return;
    const wait = this._backoff;
    this._backoff = Math.min(this.maxReconnectMs, this._backoff * 2);
    this._timer = setTimeout(() => { this._timer = null; this._open(); }, wait);
  }

  stop() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    if (this.es) { try { this.es.close(); } catch (e) {} this.es = null; }
    this.link = LINK.IDLE;
    this.bridgeConnected = false;
    this._neutralise();
  }
}

// Microphone metering + calibration. Metering ONLY -- nothing here drives
// enemy behaviour yet.
//
// Two things that bite, handled explicitly:
//   * AudioContext starts suspended; start() must be called from the same user
//     gesture that enters the AR session, and it resume()s the context there.
//   * A dead stream must never look like silence. State is reported separately
//     from level, so "quiet room" and "broken mic" are distinguishable.

export const MIC = {
  IDLE: 'idle', STARTING: 'starting', LIVE: 'live',
  DENIED: 'denied', LOST: 'lost', UNSUPPORTED: 'unsupported',
};

// FIVE discrete sensitivity presets, two either side of normal, 6 dB apart.
// A lower ceiling means a quieter room reaches full hostility, so the shift is
// NEGATIVE as sensitivity rises.
//
// The ESP32's input is a two-position momentary button, not a knob (no usable
// ADC on that pin while the radio is on), so the hardware cannot address a
// preset directly: each PRESS steps one along this list and stays there. The
// same step is what the on-screen SENS chip does, and the dashboard can jump
// straight to any of them.
export const PRESETS = {
  min:    { label: 'min',    ceilingShift: +12 },   // shouting room; hardest to trigger
  low:    { label: 'low',    ceilingShift: +6 },
  normal: { label: 'normal', ceilingShift: 0 },
  high:   { label: 'high',   ceilingShift: -6 },
  max:    { label: 'max',    ceilingShift: -12 },   // quiet room; hair trigger
};
/** Least to most sensitive. The cycle order for the handheld and the chip. */
export const PRESET_ORDER = ['min', 'low', 'normal', 'high', 'max'];
export const ESP32_TOGGLE = { 2048: 'normal', 4095: 'high' };
export function presetFromToggle(raw) {
  return ESP32_TOGGLE[raw] || (Number(raw) > 3000 ? 'high' : 'normal');
}

// Prior art from the earlier phone-mic-over-HTTP build. A starting point, not
// a target: different mic path, so calibrate on the actual device.
export const DEFAULT_CAL = { floor: -49.3, ceiling: -21.6 };
export const CAL_SPAN_DB = 27.7;     // nominal span above the floor
export const CAL_MARGIN_DB = 3.0;    // floor sits this far above measured ambient
// A high ambient floor plus a fixed span pushes the ceiling to near 0 dBFS,
// which no real voice reaches -- measured on a noisy floor: ambient -31.7 gave
// a -1.0 dBFS ceiling, permanently unreachable. Cap the ceiling somewhere a
// shout can actually get to, and keep a usable span underneath it.
export const CEILING_MAX_DB = -8.0;
export const MIN_SPAN_DB = 10.0;
export const DB_MIN = -100;
const STORAGE_KEY = 'jz.mic.cal.v1';

export function percentile(sorted, p) {
  if (!sorted.length) return DB_MIN;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.round((sorted.length - 1) * p)));
  return sorted[i];
}

/** dBFS from a time-domain RMS. Digital silence clamps to DB_MIN, never -Inf. */
export function rmsToDb(rms) {
  if (!(rms > 0) || !Number.isFinite(rms)) return DB_MIN;
  const db = 20 * Math.log10(rms);
  return Number.isFinite(db) ? Math.max(DB_MIN, db) : DB_MIN;
}

/** Floor/ceiling window, linear between. Never NaN. */
export function hostilityFrom(db, floor, ceiling) {
  if (!Number.isFinite(db)) return 0;
  const span = ceiling - floor;
  if (!(span > 0)) return db >= ceiling ? 1 : 0;
  const h = (db - floor) / span;
  return h <= 0 ? 0 : h >= 1 ? 1 : h;
}

export function loadCal() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (!Number.isFinite(o.floor) || !Number.isFinite(o.ceiling)) return null;
    // Sanitise: a window stored by an older build (or a calibration taken in a
    // roaring room) can have a ceiling near 0 dBFS that no voice reaches.
    // Clamp it on the way in rather than silently keeping a dead window.
    o.ceiling = Math.min(o.ceiling, CEILING_MAX_DB);
    if (o.ceiling - o.floor < MIN_SPAN_DB) o.floor = o.ceiling - MIN_SPAN_DB;
    return o;
  } catch (e) { return null; }          // private mode / blocked storage
}
export function saveCal(o) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(o)); return true; }
  catch (e) { return false; }
}

export class MicMeter {
  constructor({ fftSize = 1024, peakWindowMs = 2000 } = {}) {
    this.fftSize = fftSize;
    this.peakWindowMs = peakWindowMs;
    this.state = MIC.IDLE;
    this.error = null;
    this.db = DB_MIN;
    this.peak = DB_MIN;
    // Rolling peak over a ring buffer rather than an array of objects: this
    // runs every frame, and {t, db} per frame plus a shift() was ~90
    // allocations a second for a two-second window.
    this._pt = new Float64Array(256);
    this._pd = new Float64Array(256);
    this._pHead = 0;                  // next slot to write
    this._pCount = 0;
    this.ctx = null; this.analyser = null; this.stream = null; this.track = null;
    this._buf = null;
    this.calibrating = false;
    this.calRemainingMs = 0;
    // The game speaks through the same phone that is listening, 15 cm away,
    // with echo cancellation deliberately off. While one of our own voices is
    // playing the meter must not hear it, or the zombies would shout the room
    // into being more hostile, which makes more zombies shout.
    this.suppressedUntil = 0;
    this.suppressedFrames = 0;

    const saved = loadCal();
    this.cal = saved ? { floor: saved.floor, ceiling: saved.ceiling }
                     : { ...DEFAULT_CAL };
    this.preset = (saved && PRESETS[saved.preset]) ? saved.preset : 'normal';
    this.fineDb = (saved && Number.isFinite(saved.fineDb)) ? saved.fineDb : 0;
    this.calibratedAt = saved ? saved.at || null : null;
  }

  get floor() { return this.cal.floor; }
  /** Sensitivity SHIFTS the window; it does not amplify the signal. */
  get ceiling() {
    const shift = (PRESETS[this.preset] || PRESETS.normal).ceilingShift + this.fineDb;
    return Math.max(this.cal.floor + 3, this.cal.ceiling + shift);
  }
  get hostility() { return this.suppressed ? 0 : hostilityFrom(this.db, this.floor, this.ceiling); }
  /** True while the game's own audio is playing (plus a short tail). */
  get suppressed() { return this._nowMs() < this.suppressedUntil; }
  _nowMs() { return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now(); }
  /** Deafen the meter for `ms`. Extends an existing window, never shortens it
   *  below what is already granted unless `ms` is 0, which releases it. */
  suppress(ms) {
    const now = this._nowMs();
    if (!(ms > 0)) { this.suppressedUntil = 0; return 0; }
    this.suppressedUntil = Math.max(this.suppressedUntil, now + ms);
    return this.suppressedUntil - now;
  }
  /** Set the window to exactly `ms` from now. For the caller that knows the
   *  real length of what it is playing; `suppress` only ever extends. */
  suppressFor(ms) {
    this.suppressedUntil = (ms > 0) ? this._nowMs() + ms : 0;
    return Math.max(0, this.suppressedUntil - this._nowMs());
  }
  releaseSuppression() { this.suppressedUntil = 0; }
  get live() { return this.state === MIC.LIVE; }

  setPreset(name) {
    if (!PRESETS[name]) return this.preset;
    this.preset = name;
    this._persist();
    return this.preset;
  }
  /**
   * Step `dir` places along PRESET_ORDER and stay there, wrapping at the ends.
   * This is what a press of the handheld's button does, and what tapping the
   * SENS chip does -- a press toggles, it is not held.
   */
  cyclePreset(dir = 1) {
    const i = PRESET_ORDER.indexOf(this.preset);
    const n = PRESET_ORDER.length;
    const next = PRESET_ORDER[(((i < 0 ? PRESET_ORDER.indexOf('normal') : i) + dir) % n + n) % n];
    return this.setPreset(next);
  }
  /** Where this preset sits in the list, for a dot indicator. */
  get presetIndex() { return Math.max(0, PRESET_ORDER.indexOf(this.preset)); }
  /** Screen-only fine trim, in dB. The hardware toggle never reaches this. */
  setFineDb(db) {
    this.fineDb = Math.max(-24, Math.min(24, Math.round(db * 2) / 2));
    this._persist();
  }
  applyEsp32Toggle(raw) { this.setPreset(presetFromToggle(raw)); }

  _persist() {
    saveCal({ floor: this.cal.floor, ceiling: this.cal.ceiling,
              preset: this.preset, fineDb: this.fineDb, at: this.calibratedAt });
  }

  /**
   * Must be called from the user gesture that enters AR. Resolves to the state
   * string; never rejects.
   */
  async start() {
    if (this.state === MIC.LIVE || this.state === MIC.STARTING) return this.state;
    this.state = MIC.STARTING; this.error = null;
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        this.state = MIC.UNSUPPORTED; this.error = 'getUserMedia unavailable';
        return this.state;
      }
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) { this.state = MIC.UNSUPPORTED; this.error = 'no AudioContext'; return this.state; }

      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false, noiseSuppression: false, autoGainControl: false,
        },
        video: false,
      });
      this.ctx = new AC();
      // Started suspended: resume here, inside the gesture, not lazily later.
      if (this.ctx.state === 'suspended') {
        try { await this.ctx.resume(); } catch (e) { /* reported below */ }
      }
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = this.fftSize;
      this.analyser.smoothingTimeConstant = 0;
      src.connect(this.analyser);       // deliberately NOT connected to output
      this._buf = new Float32Array(this.analyser.fftSize);

      this.track = this.stream.getAudioTracks()[0] || null;
      if (this.track) {
        this.track.addEventListener('ended', () => {
          this.state = MIC.LOST; this.error = 'track ended';
        });
        this.track.addEventListener('mute', () => {
          this.state = MIC.LOST; this.error = 'track muted';
        });
        this.track.addEventListener('unmute', () => {
          if (this.state === MIC.LOST) { this.state = MIC.LIVE; this.error = null; }
        });
      }
      this.state = MIC.LIVE;
      return this.state;
    } catch (e) {
      const name = (e && e.name) || '';
      this.state = (name === 'NotAllowedError' || name === 'SecurityError')
        ? MIC.DENIED : MIC.LOST;
      this.error = name ? `${name}: ${e.message || ''}`.slice(0, 80) : String(e).slice(0, 80);
      return this.state;
    }
  }

  /** Read one frame. Safe to call every frame; never throws. */
  sample(now) {
    if (this.state !== MIC.LIVE || !this.analyser) return this.db;
    try {
      // A track that dies without firing an event still must not read as silence.
      if (this.track && this.track.readyState !== 'live') {
        this.state = MIC.LOST; this.error = 'track ' + this.track.readyState;
        return this.db;
      }
      if (this.ctx && this.ctx.state === 'suspended') {
        this.ctx.resume().catch(() => {});
      }
      if (this.suppressed) {
        // Hold the last quiet reading rather than measuring our own speaker.
        // The level is pinned below the floor so nothing downstream -- the
        // meter, the lamps, the room's mood -- reacts to the game's own voice.
        this.suppressedFrames++;
        this.db = Math.min(this.db, this.floor - 1);
        return this.db;
      }
      this.analyser.getFloatTimeDomainData(this._buf);
      let sum = 0;
      for (let i = 0; i < this._buf.length; i++) sum += this._buf[i] * this._buf[i];
      this.db = rmsToDb(Math.sqrt(sum / this._buf.length));

      const cap = this._pt.length;
      this._pt[this._pHead] = now;
      this._pd[this._pHead] = this.db;
      this._pHead = (this._pHead + 1) % cap;
      if (this._pCount < cap) this._pCount++;
      const cutoff = now - this.peakWindowMs;
      let p = DB_MIN;
      for (let i = 0; i < this._pCount; i++) {
        const idx = (this._pHead - 1 - i + cap * 2) % cap;
        if (this._pt[idx] < cutoff) break;          // older still, by construction
        if (this._pd[idx] > p) p = this._pd[idx];
      }
      this.peak = p;

      if (this.calibrating) this._calSamples.push(this.db);
      return this.db;
    } catch (e) {
      this.state = MIC.LOST;
      this.error = 'read: ' + ((e && e.message) || e);
      return this.db;
    }
  }

  /**
   * Sample ambient for `ms`, put the floor just above it and the ceiling a
   * fixed span higher. onTick(msRemaining) drives the on-screen countdown.
   */
  calibrate(ms = 3000, onTick = () => {}) {
    if (this.state !== MIC.LIVE) {
      return Promise.resolve({ ok: false, reason: 'mic not live (' + this.state + ')' });
    }
    if (this.suppressed) {
      return Promise.resolve({ ok: false, reason: 'the game is talking; try again in a moment' });
    }
    if (this.calibrating) return Promise.resolve({ ok: false, reason: 'already calibrating' });
    this.calibrating = true;
    this._calSamples = [];
    const started = (typeof performance !== 'undefined') ? performance.now() : Date.now();
    return new Promise(resolve => {
      const iv = setInterval(() => {
        const now = (typeof performance !== 'undefined') ? performance.now() : Date.now();
        const left = Math.max(0, ms - (now - started));
        this.calRemainingMs = left;
        try { onTick(left); } catch (e) {}
        if (left > 0) return;
        clearInterval(iv);
        this.calibrating = false;
        this.calRemainingMs = 0;
        const s = this._calSamples.filter(Number.isFinite).sort((a, b) => a - b);
        if (s.length < 5) {
          resolve({ ok: false, reason: 'too few samples (' + s.length + ')' });
          return;
        }
        // p95 of ambient, so a cough during calibration does not set the floor.
        const ambient = percentile(s, 0.95);
        let floor = Math.min(-10, ambient + CAL_MARGIN_DB);
        let ceiling = Math.min(floor + CAL_SPAN_DB, CEILING_MAX_DB);
        if (ceiling - floor < MIN_SPAN_DB) floor = ceiling - MIN_SPAN_DB;
        this.cal = { floor, ceiling };
        this.calibratedAt = new Date().toISOString();
        const stored = this._persist();
        resolve({ ok: true, ambient, floor: this.cal.floor, ceiling: this.cal.ceiling,
                  span: this.cal.ceiling - this.cal.floor,
                  cappedCeiling: ceiling >= CEILING_MAX_DB - 1e-9,
                  samples: s.length, persisted: stored !== false });
      }, 100);
    });
  }

  resetCal() {
    this.cal = { ...DEFAULT_CAL };
    this.preset = 'normal'; this.fineDb = 0; this.calibratedAt = null;
    this._persist();
  }

  stop() {
    try { if (this.stream) this.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
    try { if (this.ctx) this.ctx.close(); } catch (e) {}
    this.stream = null; this.ctx = null; this.analyser = null; this.track = null;
    if (this.state === MIC.LIVE) this.state = MIC.IDLE;
  }

  /** One-line status for the metrics block. */
  statusText() {
    switch (this.state) {
      case MIC.LIVE: return this.suppressed ? 'live (deafened)' : 'live';
      case MIC.DENIED: return 'DENIED';
      case MIC.LOST: return 'LOST';
      case MIC.STARTING: return 'starting…';
      case MIC.UNSUPPORTED: return 'unsupported';
      default: return 'idle';
    }
  }
}

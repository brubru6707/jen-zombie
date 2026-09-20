// Drive the handheld's three lamps (green, yellow, red) from the live
// hostility value, through POST /led {led:[g,y,r]} on the same origin.
//
// The meter CLIMBS: green fills first, then yellow, then red, so a rising
// shout reads left to right on the board. Values are PWM duty 0..1, two
// decimals, per PROTOCOL.md section 4.
//
// Network discipline, because this sits on the frame loop:
//   * send only when the quantised tuple CHANGES, and at most `hz` times a
//     second -- never per frame;
//   * but ALSO at least once every `keepaliveMs`, even unchanged. The board
//     holds the last line it was given for ever, so silence on this channel
//     has to mean "the game is gone" and not "the meter is steady" -- the
//     bridge darkens the lamps when nothing has driven them for a few
//     seconds, and it can only tell those apart if a steady meter still
//     speaks. Same reasoning as the board's own 2 Hz heartbeat.
//   * one request in flight at a time; a slow Pi never gets a queue;
//   * a change that lands inside the rate window is sent at the next
//     opportunity, so the final value always reaches the board;
//   * nothing here can throw into the caller.
export const STAGES = [[0.00, 0.34], [0.33, 0.67], [0.66, 1.00]];   // [start, full] per lamp

const clamp01 = v => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0);
const round2 = v => Math.round(v * 100) / 100;

/** hostility 0..1 -> [g, y, r] duties, each 0..1 with two decimals. */
export function ledsFromHostility(h) {
  const v = clamp01(h);
  return STAGES.map(([lo, hi]) => round2(clamp01((v - lo) / (hi - lo))));
}

export class LedMeter {
  constructor({ url = '/led', hz = 10, quantum = 0.05, keepaliveMs = 2000,
                fetchImpl = (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : null),
                warn = () => {} } = {}) {
    this.url = url;
    this.minGapMs = 1000 / hz;
    this.keepaliveMs = keepaliveMs;
    this.quantum = quantum;
    this.fetch = fetchImpl;
    this.warn = warn;
    this.last = null;           // last tuple SENT (quantised)
    this.lastSentAt = -Infinity;
    this.inFlight = false;
    this.sent = 0; this.failed = 0;
    this.value = [0, 0, 0];     // latest computed tuple, for telemetry
    this.pending = false;
  }

  _quant(t) { const q = this.quantum; return t.map(v => round2(Math.round(v / q) * q)); }
  _same(a, b) { return !!a && !!b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2]; }

  /**
   * Call every frame. `enabled` false (no handheld) means nothing is sent and
   * the next enable re-sends the current value. Returns true when a POST left.
   */
  update(now, hostility, enabled = true) {
    try {
      const t = this._quant(ledsFromHostility(hostility));
      this.value = t;
      if (!enabled) { this.last = null; this.pending = false; return false; }
      const stale = (now - this.lastSentAt) >= this.keepaliveMs;
      if (this._same(t, this.last) && !stale) { this.pending = false; return false; }
      this.pending = true;
      if (this.inFlight || (now - this.lastSentAt) < this.minGapMs) return false;
      return this._post(t, now);
    } catch (e) { this.warn('led update (ignored):', e); return false; }
  }

  /** Lamps off, immediately (session end, or the page going away). */
  off(now = Date.now()) {
    try { return this._post([0, 0, 0], now, true); } catch (e) { return false; }
  }

  /**
   * `final` marks the one request that has to outlive the page. ONLY that one
   * uses fetch's keepalive: the browser allows very few keepalive requests in
   * flight at once, and using it for the 10 Hz stream made most of them fail
   * -- measured 3715 failures against 2390 successes -- which left the lamps
   * dark while the meter was reading perfectly well.
   */
  _post(t, now, final = false) {
    if (!this.fetch) return false;
    this.inFlight = true;
    this.lastSentAt = now;
    this.last = t;
    this.pending = false;
    let p;
    try {
      const opts = {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ led: t }), cache: 'no-store',
      };
      if (final) opts.keepalive = true;
      p = this.fetch(this.url, opts);
    } catch (e) { this.inFlight = false; this.failed++; return false; }
    Promise.resolve(p)
      .then(r => { if (r && r.ok === false) this.failed++; else this.sent++; })
      .catch(() => { this.failed++; })
      .finally(() => { this.inFlight = false; });
    return true;
  }

  status() {
    return { value: this.value, last: this.last, sent: this.sent, failed: this.failed,
             inFlight: this.inFlight, pending: this.pending,
             sinceSentMs: Number.isFinite(this.lastSentAt) ? -1 : null };
  }
}

// The Stage 3 countdown/prefetch/swap machine, deliberately free of WebXR and
// three.js so it can be driven by a fake clock in node.
//
// Two modes:
//
//   AUTOMATIC (default) -- the clock is a METRONOME and the world swaps by
//   itself at every deadline. This is what the timing tests pin down.
//
//   MANUAL ({manual:true}) -- the clock is a COOLDOWN on taking a snapshot of
//   the room. It counts down, and at zero it STOPS AT ZERO and waits: the
//   world only changes when the player presses the handheld's stick button,
//   which is what trigger() is. Holding at ready costs nothing and the player
//   can take as long as they like. The cooldown restarts from the press, not
//   from the world landing, because the press is when the photo was taken.
//
// Contract (automatic mode):
//   * The clock is a METRONOME. Every deadline advances by exactly cycleMs.
//     A late world never extends, freezes or resets it.
//   * The next world is fetched AND prepared while the clock runs, so the swap
//     itself is a pointer swap, not construction work.
//   * If nothing is ready at zero, the cycle reports 'incoming' and the current
//     world keeps playing; the new one lands mid-cycle the moment it is ready.
//   * A fetch failure keeps the current world and retries. It never throws.

export class WorldCycle {
  constructor({ cycleMs = 30000, fetchWorld, prepare, swap, manual = false,
                retryMs = 3000, log = () => {}, warn = () => {} }) {
    this.cycleMs = cycleMs;
    this.manual = manual;
    this.fetchWorld = fetchWorld;
    this.prepare = prepare;
    this.swap = swap;
    this.retryMs = retryMs;
    this.log = log; this.warn = warn;

    this.started = false;
    this.deadline = 0;
    this.pending = null;      // {world, prepared}
    this.fetching = false;
    this.awaiting = false;    // deadline passed with nothing ready
    this.retryAt = 0;
    this.current = null;

    this.swaps = 0; this.lateSwaps = 0; this.fails = 0;
    this.skips = 0;           // operator skips (handheld sw button)
    this.ready = false;       // manual: cooldown done, waiting for the press
    this.snapshots = 0;       // manual: presses that actually took one
    this.earlyPresses = 0;    // manual: presses while still cooling down
    this.lastFetchMs = 0; this.lastError = null;
    this.lastSwapMs = 0; this.maxSwapMs = 0;
    this.deadlineHistory = [];
  }

  start(t) {
    if (this.started) return;
    this.started = true;
    this.deadline = t + this.cycleMs;
    // The FIRST world is taken automatically, or the player would walk into an
    // empty room and have to press to see anything at all. Every world after
    // it waits for a press. The cooldown starts running now, so the first
    // snapshot is available one cooldown into the session.
    if (this.manual) { this.awaiting = true; this._initial = true; }
    this._prefetch(t);
  }

  /** A world is fetched and built, waiting to be shown. */
  get prepared() { return !!this.pending; }

  _prefetch(t) {
    if (this.fetching || this.pending) return;
    this.fetching = true;
    this.lastError = null;
    const t0 = t;
    let p;
    try {
      p = this.fetchWorld();
    } catch (e) {                       // synchronous throw from the caller
      this.fetching = false; this.fails++; this.lastError = String(e);
      this.retryAt = t + this.retryMs;
      return;
    }
    Promise.resolve(p).then(world => {
      let prepared = null;
      try {
        prepared = this.prepare ? this.prepare(world) : null;
      } catch (e) {
        this.warn('prepare failed:', e);
        this.fetching = false; this.fails++; this.lastError = 'prepare: ' + e;
        this.retryAt = Date.now ? 0 : 0;   // retry handled by tick via retryAt
        this._scheduleRetry();
        return;
      }
      this.pending = { world, prepared };
      this.fetching = false;
      this.lastFetchMs = this._elapsed(t0);
      this.log('world ready:', world && world.biome);
    }).catch(e => {
      this.fetching = false; this.fails++; this.lastError = String(e && e.message || e);
      this.warn('world fetch failed (keeping current world):', e);
      this._scheduleRetry();
    });
  }

  _scheduleRetry() { this._retryPending = true; }
  _elapsed() { return this.lastFetchMs; }   // real timing is set by the caller

  /**
   * Manual mode: the player pressed the stick button.
   *   cooling down  -> refused, nothing happens, the clock keeps running
   *   ready         -> take the snapshot; the world lands when it lands
   * Returns true if a snapshot was taken.
   */
  trigger(t) {
    if (!this.started || !this.manual) return false;
    if (!this.ready) { this.earlyPresses++; return false; }
    this.snapshots++;
    this.ready = false;
    this.deadline = t + this.cycleMs;      // cooldown restarts from the press
    this.awaiting = true;                  // shown as SCANNING until it lands
    if (this.pending) { this._doSwap(t, false); return true; }
    if (!this.fetching) { this.retryAt = 0; this._prefetch(t); }
    return true;
  }

  tick(t) {
    if (!this.started) return;
    if (this._retryPending) { this._retryPending = false; this.retryAt = t + this.retryMs; }
    if (this.retryAt && t >= this.retryAt && !this.pending && !this.fetching) {
      this.retryAt = 0;
      this._prefetch(t);
    }
    if (this.manual) {
      // At zero the clock stops and waits. It is not a deadline any more.
      if (!this.ready && t >= this.deadline) this.ready = true;
      if (this.awaiting && this.pending) {
        const late = !this._initial;        // the opening world is not "late"
        this._initial = false;
        this._doSwap(t, late);
      }
      return;
    }
    if (t >= this.deadline) {
      this.deadlineHistory.push(this.deadline);
      this.deadline += this.cycleMs;        // metronome -- never extended
      if (this.pending) this._doSwap(t, false);
      else this.awaiting = true;
    }
    if (this.awaiting && this.pending) this._doSwap(t, true);
  }

  _doSwap(t, late) {
    const { world, prepared } = this.pending;
    this.pending = null;
    const t0 = (typeof performance !== 'undefined' && performance.now)
      ? performance.now() : 0;
    try {
      if (this.swap) this.swap(prepared, world, late);
    } catch (e) {
      this.warn('swap failed (world kept):', e);
    }
    const t1 = (typeof performance !== 'undefined' && performance.now)
      ? performance.now() : 0;
    this.lastSwapMs = t1 - t0;
    if (this.lastSwapMs > this.maxSwapMs) this.maxSwapMs = this.lastSwapMs;
    this.current = world;
    this.swaps++; if (late) this.lateSwaps++;
    this.awaiting = false;
    // Automatic mode lines the next world up during the countdown. Manual mode
    // waits for the next press, so the snapshot is of the room at that moment.
    if (!this.manual) this._prefetch(t);
  }

  /**
   * Operator skip (the handheld's stick button): swap NOW if the next world is
   * ready, otherwise show INCOMING and land it the instant it arrives. The
   * countdown restarts from this moment, a full cycle away. This is the one
   * deliberate exception to the metronome: an explicit human action, not a
   * late server, moves the deadline. Returns true if a swap happened here.
   */
  skip(t) {
    if (!this.started) return false;
    this.skips++;
    this.deadline = t + this.cycleMs;
    if (this.pending) { this._doSwap(t, false); return true; }
    this.awaiting = true;                   // tick() swaps the moment it lands
    if (!this.fetching) { this.retryAt = 0; this._prefetch(t); }
    return false;
  }

  /**
   * Milliseconds left on the clock: 0 once the cooldown is done (it holds
   * there), or null while a world is on its way.
   */
  remaining(t) { return this.awaiting ? null : Math.max(0, this.deadline - t); }
}

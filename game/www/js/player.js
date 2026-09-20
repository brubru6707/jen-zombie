// The player: three hearts, a hit, and five seconds of mercy afterwards.
//
// Deliberately free of the DOM and of three, so the rules (and especially the
// immunity window, which is the difference between "hard" and "unplayable")
// are checked in node rather than by being mobbed in a room.
export const PLAYER = {
  hearts: 3,
  immunityMs: 5000,     // after a hit, nothing can touch you for this long
  flashMs: 650,         // how long the screen stays tinted
  reviveMs: 3500,       // after being overrun, hearts come back (demo, not roguelike)
};

/**
 * Points. Kills pay by how much trouble the thing was, a snapshot pays more
 * than any single kill because scanning the room is the mechanic the whole
 * game is built on, and taking a hit costs -- but the score never goes
 * negative, because a number that only falls is not worth looking at.
 */
// Being overrun costs 50 and REPLACES that hit's own -20 rather than stacking
// on top of it, so losing your hearts costs exactly the 50 the HUD announces.
// Stacking would have made the death moment cost 70 and the number on screen
// disagree with the message next to it.
export const POINTS = { walker: 10, runner: 15, brute: 25, snapshot: 50, hit: -20, overrun: -50 };

export class Score {
  constructor() { this.reset(); }
  reset() {
    this.value = 0; this.kills = 0; this.snapshots = 0; this.hitsTaken = 0;
    this.overruns = 0;
    this.best = 0;
  }
  _add(n) {
    this.value = Math.max(0, this.value + n);
    if (this.value > this.best) this.best = this.value;
    return this.value;
  }
  kill(kind = 'walker') { this.kills++; return this._add(POINTS[kind] ?? POINTS.walker); }
  snapshot() { this.snapshots++; return this._add(POINTS.snapshot); }
  hit() { this.hitsTaken++; return this._add(POINTS.hit); }
  /**
   * All three hearts gone. Costs POINTS.overrun INSTEAD of the hit penalty --
   * the caller uses this in place of hit(), not as well as it. Floored at zero
   * by _add, so a death can never put the score negative and can never make
   * the chain settlement owe anything.
   */
  overrun() { this.hitsTaken++; this.overruns++; return this._add(POINTS.overrun); }
  /** Padded so the HUD does not jiggle as it grows. */
  get text() { return String(this.value).padStart(4, '0'); }
  status() {
    return { value: this.value, best: this.best, kills: this.kills,
             snapshots: this.snapshots, hitsTaken: this.hitsTaken,
             overruns: this.overruns };
  }
}

export class Player {
  constructor({ hearts = PLAYER.hearts, immunityMs = PLAYER.immunityMs,
                reviveMs = PLAYER.reviveMs } = {}) {
    this.maxHearts = Math.max(1, hearts);
    this.immunityMs = immunityMs;
    this.reviveMs = reviveMs;
    this.reset(0);
  }

  /** `graceMs` is a settle-in window: walking into a room already surrounded
   *  should not cost a heart before the player has seen anything. */
  reset(now = 0, graceMs = 0) {
    this.hearts = this.maxHearts;
    this.lastHitAt = -Infinity;
    this.overrunAt = -Infinity;
    this.hits = 0;
    this.blocked = 0;                 // hits absorbed by immunity
    this.overruns = 0;
    this._startedImmuneUntil = now + graceMs;
  }

  immune(now) {
    return now - this.lastHitAt < this.immunityMs || now < this._startedImmuneUntil;
  }
  immuneLeftMs(now) {
    const left = Math.max(this._startedImmuneUntil - now,
                          this.immunityMs - (now - this.lastHitAt));
    return this.immune(now) ? Math.max(0, left) : 0;
  }
  get alive() { return this.hearts > 0; }
  /** 0..1, for the red tint: strongest the instant you are hit. */
  flash(now) {
    const dt = now - this.lastHitAt;
    if (!(dt >= 0) || dt > PLAYER.flashMs) return 0;
    return 1 - dt / PLAYER.flashMs;
  }

  /**
   * A mob connected. Returns what happened, so the caller does not have to
   * infer it: { hit, blocked, hearts, overrun }.
   */
  damage(now) {
    if (!this.alive) return { hit: false, blocked: false, hearts: 0, overrun: false };
    if (this.immune(now)) { this.blocked++; return { hit: false, blocked: true, hearts: this.hearts, overrun: false }; }
    this.hearts--;
    this.hits++;
    this.lastHitAt = now;
    const overrun = this.hearts <= 0;
    if (overrun) { this.overruns++; this.overrunAt = now; }
    return { hit: true, blocked: false, hearts: this.hearts, overrun };
  }

  /** Call every frame. Returns true on the frame the player comes back. */
  tick(now) {
    if (this.alive || !Number.isFinite(this.overrunAt)) return false;
    if (now - this.overrunAt < this.reviveMs) return false;
    const at = now;
    this.reset(0);
    this.lastHitAt = at;               // come back with the immunity window
    return true;
  }

  status(now = 0) {
    return { hearts: this.hearts, max: this.maxHearts, alive: this.alive,
             immune: this.immune(now), immuneMs: Math.round(this.immuneLeftMs(now)),
             hits: this.hits, blocked: this.blocked, overruns: this.overruns };
  }
}

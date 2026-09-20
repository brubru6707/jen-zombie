// Who is in the room depends on how loud it is.
//
// Quiet for a moment and the hostile crowd wanders off while calm ones drift
// in; raise your voice and the calm ones leave again. The two thresholds are
// deliberately far apart and a change has to persist, because a population
// that flips on every cough is worse than one that never changes.
export const MOOD = { HOSTILE: 'hostile', CALM: 'calm' };

export const MOOD_RULE = {
  calmBelow: 0.18,      // hostility at or under this, for dwellMs -> calm
  hostileAbove: 0.45,   // at or over this, for dwellMs -> hostile
  dwellMs: 2000,        // how long it has to hold before the room turns over
};

export class Population {
  constructor({ calmBelow = MOOD_RULE.calmBelow, hostileAbove = MOOD_RULE.hostileAbove,
                dwellMs = MOOD_RULE.dwellMs, mood = MOOD.HOSTILE } = {}) {
    this.calmBelow = calmBelow;
    this.hostileAbove = hostileAbove;
    this.dwellMs = dwellMs;
    this.mood = mood;
    this.pending = null;      // the mood being argued for
    this.pinned = null;       // set by pin(): ignore the microphone
    this.heldMs = 0;
    this.changes = 0;
  }

  /** 0..1 of the way to the next turnover, for a HUD bar. */
  get progress() { return this.pending ? Math.min(1, this.heldMs / this.dwellMs) : 0; }

  /**
   * Pin the room to one mood and stop listening. Useful on the floor when the
   * hostile half has to be shown in a quiet corridor, or the calm half in a
   * loud hall -- and it is how the two populations are tested without filling
   * the room with noise.
   */
  pin(mood) {
    this.pinned = (mood === MOOD.CALM || mood === MOOD.HOSTILE) ? mood : null;
    if (!this.pinned) return null;
    this.pending = null; this.heldMs = 0;
    if (this.mood === this.pinned) return null;
    this.mood = this.pinned; this.changes++;
    return this.mood;
  }

  /**
   * Feed it the frame time and the current hostility. Returns the NEW mood on
   * the frame it changes, else null. Never throws.
   */
  update(dtMs, hostility) {
    if (this.pinned) return null;              // not listening
    const h = Number.isFinite(hostility) ? hostility : 0;
    let wants = null;
    if (h <= this.calmBelow) wants = MOOD.CALM;
    else if (h >= this.hostileAbove) wants = MOOD.HOSTILE;
    if (!wants || wants === this.mood) {          // nothing to argue for
      this.pending = null; this.heldMs = 0;
      return null;
    }
    if (wants !== this.pending) { this.pending = wants; this.heldMs = 0; }
    this.heldMs += (dtMs > 0 ? dtMs : 0);
    if (this.heldMs < this.dwellMs) return null;
    this.mood = wants;
    this.pending = null; this.heldMs = 0; this.changes++;
    return this.mood;
  }

  status() { return { mood: this.mood, pending: this.pending, pinned: this.pinned,
                      progress: +this.progress.toFixed(2), changes: this.changes }; }
}

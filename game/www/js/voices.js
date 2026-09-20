// Who is talking, what they say, and making sure the game does not hear
// itself say it.
//
// The world arrives with dialogue for each kind of undead and for the
// peaceful locals (`barks.walker|runner|brute|calm`, written by Nemotron for
// that biome). This picks a mob, picks one of its lines, shows it over that
// mob's head and asks the Pi for the audio.
//
// Three things matter more than the feature itself:
//
//  * THE MICROPHONE MUST NOT HEAR IT. Hostility drives the whole game and the
//    phone's speaker is 15 cm from its microphone with echo cancellation
//    deliberately off, so a zombie shouting would raise hostility, which
//    turns the room hostile, which makes more zombies shout. The mic is
//    suppressed from before playback until a tail after it, and the line is
//    never spoken while the player is being metered for anything that matters.
//  * NO AUDIO IS NOT NO DIALOGUE. If the key is missing, the budget is spent
//    or the Mac is off, the line still appears over the mob. The failure is
//    recorded and shown on the dashboard rather than swallowed.
//  * ONE VOICE AT A TIME. Overlapping lines are noise, and they multiply the
//    bill.
//  * NOTHING IS EVER SAID TWICE. A line, once spoken, is retired for the rest
//    of the session -- across mobs, across roles and across worlds. Hearing
//    the same six words for the third time is what makes a crowd read as a
//    loop instead of a place. When every mob on screen has run out of fresh
//    lines they simply go quiet until the next world brings more, which is
//    the honest failure: silence, not repetition.
//
// Free of the DOM and of three: the page owns the bubble, this owns the
// choosing, the timing and the audio.

export const VOICE = {
  gapMs: [5000, 9000],     // quiet between lines
  showMs: 3400,            // how long a bubble stays up
  tailMs: 400,             // mic stays suppressed this long after audio ends
  leadMs: 120,             // ...and from this long before it starts
  maxChars: 140,
  volume: 1.0,
};

const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

// Case and spacing are not meaningful differences between two lines, so the
// retirement set is keyed on the flattened text. Two worlds that both produce
// "Stay on the path." therefore only ever spend it once.
const norm = (s) => String(s == null ? '' : s).toLowerCase().replace(/\s+/g, ' ').trim();

/** A copy in random order. Fisher-Yates; the caller may keep it. */
function shuffled(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    const t = a[i]; a[i] = a[j]; a[j] = t;
  }
  return a;
}

/** The dialogue list for a mob, out of a world's barks. Never throws. */
export function linesFor(world, mob) {
  const barks = (world && world.barks) || null;
  if (!barks) return null;
  const key = (mob && mob.mood === 'calm') ? 'calm' : ((mob && mob.kind) || 'walker');
  let list = barks[key];
  if (!Array.isArray(list) || !list.length) {
    // A world written before calm dialogue existed, or a kind the model did
    // not cover: fall back within the same side rather than putting a zombie
    // line in a peaceful mouth.
    if (key === 'calm') return null;
    for (const k of ['walker', 'runner', 'brute']) {
      if (Array.isArray(barks[k]) && barks[k].length) { list = barks[k]; break; }
    }
  }
  if (!Array.isArray(list) || !list.length) return null;
  // Twelve: the model is asked for six per role and a canned world carries
  // six, and nothing is ever repeated, so a bigger ceiling is free headroom.
  return list.filter(l => typeof l === 'string' && l.trim()).slice(0, 12);
}

/** Can this mob talk right now? */
export function canSpeak(m) {
  return !!m && !m.dying && !m.dead && !m.leaving && !!m.group;
}

export class Voices {
  constructor({ url = '/say', mic = null, now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
                fetchImpl = (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : null),
                audioFactory = null, log = () => {}, warn = () => {} } = {}) {
    this.url = url; this.mic = mic; this.now = now;
    this.fetch = fetchImpl;
    this.audioFactory = audioFactory ||
      ((typeof Audio !== 'undefined') ? (src => new Audio(src)) : null);
    this.log = log; this.warn = warn;

    this.current = null;        // { mob, text, role, until, spoken }
    this.nextAt = 0;
    this.enabled = true;
    // Muting silences the SPEAKER, not the dialogue: the bubble still appears
    // over the mob's head and lines are still retired. Anything else would
    // mean muting quietly changed what the game was doing.
    this.muted = false;
    this.lines = 0; this.spoken = 0; this.silent = 0; this.failures = 0;
    // Every line said so far, flattened. Nothing in here is ever said again.
    this.used = new Set();
    this.exhausted = 0;         // times every speaker on screen was out of lines
    this.lastError = null; this.lastLine = null; this.lastVoiceMs = 0;
    this._audio = null; this._url = null; this._busy = false;
  }

  /** Stop talking, drop any audio, release the microphone. */
  clear() {
    try { if (this._audio) { this._audio.pause(); this._audio.src = ''; } } catch (e) {}
    if (this._url) { try { URL.revokeObjectURL(this._url); } catch (e) {} this._url = null; }
    this._audio = null; this._busy = false;
    this.current = null;
    if (this.mic && this.mic.releaseSuppression) this.mic.releaseSuppression();
  }

  /**
   * The lines this mob has left -- its role's dialogue minus everything
   * already spoken. null when it has nothing new to say.
   */
  unusedFor(world, mob) {
    const lines = linesFor(world, mob);
    if (!lines || !lines.length) return null;
    const left = lines.filter(l => !this.used.has(norm(l)));
    return left.length ? left : null;
  }

  /** Let every line be said again. Only for tests and a deliberate restart. */
  forget() { this.used.clear(); this.exhausted = 0; }

  /**
   * Silence the speaker. Any line already playing stops at once and the
   * microphone is handed back, because a suppression that outlives its audio
   * would leave the game deaf.
   */
  setMuted(m) {
    this.muted = !!m;
    if (!this.muted) return this.muted;
    try { if (this._audio) { this._audio.pause(); this._audio.src = ''; } } catch (e) {}
    if (this._url) { try { URL.revokeObjectURL(this._url); } catch (e) {} this._url = null; }
    this._audio = null;
    if (this.mic && this.mic.suppressFor) { try { this.mic.suppressFor(0); } catch (e) {} }
    else if (this.mic && this.mic.releaseSuppression) { try { this.mic.releaseSuppression(); } catch (e) {} }
    return this.muted;
  }

  _schedule(now) {
    const [lo, hi] = VOICE.gapMs;
    this.nextAt = now + lo + Math.random() * (hi - lo);
  }

  /**
   * Once a frame. Picks a speaker when it is time, retires the bubble when it
   * expires. Returns the line being said now, or null.
   */
  update(now, mobs, world, prefer) {
    try {
      if (this.current && now >= this.current.until) {
        this.current = null;
        this._schedule(now);
      }
      if (!this.enabled || this.current || this._busy) return this.current;
      if (!this.nextAt) { this._schedule(now); return null; }
      if (now < this.nextAt) return null;
      const able = [];
      for (const m of mobs || []) if (canSpeak(m)) able.push(m);
      if (!able.length) { this.nextAt = now + 1000; return null; }
      // Walk the possible speakers in random order and take the first one
      // holding a line nobody has used. Picking the mob first and then
      // discovering it is out would silence the whole frame for no reason.
      //
      // `prefer` is the page asking for a speaker the player can actually
      // SEE. A line over the head of something behind you is a line nobody
      // reads, so on-screen mobs get first refusal and the rest are the
      // fallback -- never a reason to stay silent.
      let mob = null, lines = null;
      const order = shuffled(able);
      const passes = (typeof prefer === 'function')
        ? [order.filter(m => { try { return prefer(m); } catch (e) { return false; } }), order]
        : [order];
      for (const pass of passes) {
        for (const m of pass) {
          const left = this.unusedFor(world, m);
          if (left) { mob = m; lines = left; break; }
        }
        if (mob) break;
      }
      if (!mob) {
        // Everyone here has said everything they have. Wait for a new world.
        this.exhausted++;
        this.nextAt = now + 3000;
        return null;
      }
      const raw = pick(lines);
      this.used.add(norm(raw));       // retired BEFORE any truncation, so the
      const text = String(raw).slice(0, VOICE.maxChars);   // key matches next time
      const role = mob.mood === 'calm' ? 'calm' : (mob.kind || 'walker');
      this.current = { mob, text, role, until: now + VOICE.showMs, spoken: false };
      this.lines++;
      this.lastLine = { text, role, at: now };
      this._say(text, role);
      return this.current;
    } catch (e) {
      this.warn('voices update (ignored):', e);
      return null;
    }
  }

  /** Fetch and play one line. Never throws, never blocks the frame. */
  _say(text, role) {
    if (this.muted) { this.silent++; return; }   // shown, never sounded
    if (!this.fetch || !this.audioFactory) { this.silent++; return; }
    this._busy = true;
    const t0 = this.now();
    const url = `${this.url}?role=${encodeURIComponent(role)}&text=${encodeURIComponent(text)}`;
    let req;
    try { req = this.fetch(url, { cache: 'force-cache' }); }
    catch (e) { this._busy = false; this.failures++; this.lastError = String(e); return; }
    Promise.resolve(req)
      .then(r => {
        if (!r || !r.ok) {
          // Tell the dashboard WHY there was no audio.
          return r.json().catch(() => ({ error: 'HTTP ' + (r && r.status) }))
            .then(j => { throw new Error(j.error || ('HTTP ' + (r && r.status))); });
        }
        return r.blob();
      })
      .then(blob => {
        this.lastVoiceMs = Math.round(this.now() - t0);
        if (!blob || !blob.size) throw new Error('empty audio');
        this._play(blob);
      })
      .catch(e => {
        this.failures++; this.silent++;
        this.lastError = String(e && e.message || e);
        this.warn('no audio for this line (it is still shown):', this.lastError);
      })
      .finally(() => { this._busy = false; });
  }

  _play(blob) {
    try {
      const src = URL.createObjectURL(blob);
      const audio = this.audioFactory(src);
      audio.volume = VOICE.volume;
      this._audio = audio; this._url = src;
      // Deafen the game BEFORE the first sample reaches the speaker.
      this._suppress(3000);
      const done = () => {
        this._suppressExact(VOICE.tailMs);   // just the tail, for the room's echo
        try { URL.revokeObjectURL(src); } catch (e) {}
        if (this._url === src) this._url = null;
        if (this._audio === audio) this._audio = null;
      };
      audio.addEventListener('ended', done);
      audio.addEventListener('error', () => { this.failures++; done(); });
      audio.addEventListener('loadedmetadata', () => {
        // Now the real length is known, hold the mic for exactly that long.
        const ms = (audio.duration || 0) * 1000;
        // Now the length is known, hold the microphone for exactly that and
        // no longer: the safety net above was deliberately generous.
        if (ms > 0) this._suppressExact(ms + VOICE.tailMs);
        if (this.current) this.current.until = Math.max(this.current.until, this.now() + ms + 400);
      });
      const p = audio.play();
      if (p && p.catch) p.catch(e => {
        // Autoplay refused: the line is still on screen, and the mic is freed.
        this.failures++; this.silent++;
        this.lastError = 'playback refused: ' + (e && e.name || e);
        this._suppressExact(0);
      });
      this.spoken++;
      if (this.current) this.current.spoken = true;
    } catch (e) {
      this.failures++;
      this.lastError = String(e);
      this._suppressExact(0);
    }
  }

  _suppress(ms) {
    if (!this.mic || !this.mic.suppress) return;
    try { this.mic.suppress(ms + VOICE.leadMs); } catch (e) {}
  }

  _suppressExact(ms) {
    if (!this.mic) return;
    try {
      if (this.mic.suppressFor) this.mic.suppressFor(ms + VOICE.leadMs);
      else this.mic.suppress(ms + VOICE.leadMs);
    } catch (e) {}
  }

  status() {
    return { lines: this.lines, spoken: this.spoken, silent: this.silent,
             failures: this.failures, lastError: this.lastError, muted: this.muted,
             retired: this.used.size, exhausted: this.exhausted,
             fetchMs: this.lastVoiceMs,
             saying: this.current ? { text: this.current.text, role: this.current.role,
                                      spoken: this.current.spoken } : null };
  }
}

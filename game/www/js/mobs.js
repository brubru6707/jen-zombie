// Placeholder enemy geometry + steering. No sprites yet, no collision geometry,
// no level geometry: the room IS the level.
import * as THREE from '../vendor/three.module.js';
import { makeBillboard, faceCameraY, spriteFor, HUMANOID_HEIGHT_M } from './sprites.js';

// How close a mob gets before it stops. 0.70 m was "arm's length" on paper and
// far too close in practice: a 1.7 m sprite at 70 cm fills the entire screen.
// Tunable live with ?stop=<metres> so it can be dialled in on site.
export let STOP_DISTANCE = 2.0;
export function setStopDistance(m) {
  const v = Number(m);
  if (Number.isFinite(v) && v > 0.3 && v < 8) STOP_DISTANCE = v;
  return STOP_DISTANCE;
}
/** @deprecated kept so older imports keep working */
export const ARM_LENGTH = 2.0;
// A mob dies over this long: it pops upward and shrinks, which reads at a
// glance without turning the cutout sprite transparent (alphaTest depends on
// transparent=false, and switching it mid-death causes sorting artifacts).
export const DEATH_MS = 420;
// A calm mob keeps its distance and never raises a hand; a hostile one closes
// to STOP_DISTANCE and swings on this cadence.
export const CALM_DISTANCE = 3.2;
export const ATTACK_MS = 1600;
export const ATTACK_WINDUP_MS = 450;     // shown before the blow lands
// Far enough that a mob walking out has visibly gone, close enough that it
// does not wander for ever.
export const LEAVE_DISTANCE = 9.0;
// Slashes needed, before the world's hp_bonus. A brute is worth two throws.
export const BASE_HP = { walker: 1, runner: 1, brute: 2 };
const HIT_FLASH_MS = 130;
const SEP_RADIUS = 0.75;             // mobs nudge apart so they do not merge
                                     // into one blob -- this is legibility, not
                                     // collision geometry

const PALETTE = {
  walker: 0x6fbf73,
  runner: 0xe0c341,
  brute:  0xc0553f,
};

function buildBody(kind, biome, mood) {
  const g = new THREE.Group();
  // ~1.7 m for a humanoid; runners read as smaller and brutes as bigger, but
  // all stay anchored at the feet.
  const scale = kind === 'brute' ? 1.25 : kind === 'runner' ? 0.92 : 1.0;
  const h = HUMANOID_HEIGHT_M * scale;
  const name = spriteFor(biome, kind, mood);

  const board = makeBillboard(name, h);
  g.add(board);

  // Ground ring: sells the contact with the floor and carries the biome accent.
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(0.17 * scale, 0.24 * scale, 20).rotateX(-Math.PI / 2),
    new THREE.MeshBasicMaterial({ color: 0x101418, transparent: true, opacity: 0.5,
                                  depthWrite: false }));
  ring.position.y = 0.005;
  g.add(ring);

  g.userData.height = h;
  g.userData.board = board;
  g.userData.ring = ring;
  g.userData.sprite = name;
  return g;
}

export class Mob {
  constructor(kind, x, z, speed, biome, hpBonus = 0, mood = 'hostile') {
    this.kind = kind;
    this.biome = biome;
    this.mood = mood;
    this.group = buildBody(kind, biome, mood);
    this.group.position.set(x, 0, z);      // feet on the local-floor plane
    this.speed = speed;
    this.phase = Math.random() * Math.PI * 2;
    this.arrived = false;
    this.baseY = 0;
    this.maxHp = Math.max(1, (BASE_HP[kind] || 1) + Math.round(hpBonus || 0));
    this.hp = this.maxHp;
    this.dying = false;                    // playing the death animation
    this.dead = false;                     // finished; the caller disposes it
    this.deathT = 0;
    this.flashT = 0;
    this.leaving = false;                  // walking out of the room
    this.lastAttack = -Infinity;
    this.attacks = 0;
    this.windupT = 0;
  }

  /** The room turned over: walk away and stop being anybody's problem. */
  leave() {
    if (this.dying) return false;
    this.leaving = true;
    this.arrived = false;
    return true;
  }

  /** How close this one comes. Calm ones keep their distance. */
  get stopAt() { return this.mood === 'calm' ? CALM_DISTANCE : STOP_DISTANCE; }

  /**
   * Is this mob hitting the player on this frame? Hostile, arrived, not on
   * its way out, and off cooldown. The caller decides what a hit costs.
   */
  attack(now) {
    if (this.mood === 'calm' || this.leaving || this.dying || this.dead) return false;
    if (!this.arrived) { this.windupT = 0; return false; }
    if (now - this.lastAttack < ATTACK_MS) return false;
    this.lastAttack = now;
    this.attacks++;
    this.windupT = ATTACK_WINDUP_MS;
    return true;
  }

  /**
   * Take a hit from a thrown slash. Returns { hit, dead } so the caller can
   * count kills without inspecting state. A mob already dying absorbs nothing,
   * so two slashes in flight cannot both claim the same kill.
   */
  hit(damage = 1) {
    if (this.dying || this.dead) return { hit: false, dead: false };
    this.hp -= (damage > 0 ? damage : 0);
    this.flashT = HIT_FLASH_MS;
    if (this.hp <= 0) {
      this.hp = 0;
      this.dying = true;
      this.deathT = 0;
      return { hit: true, dead: true };
    }
    return { hit: true, dead: false };
  }

  /** The death pop. Returns true once the mob is finished and disposable. */
  _die(dt) {
    this.deathT += dt * 1000;
    const p = Math.min(1, this.deathT / DEATH_MS);
    const board = this.group.userData.board;
    const ring = this.group.userData.ring;
    try {
      const k = Math.max(0.001, 1 - p);
      board.scale.set(k, k, k);
      board.position.y = p * 0.45;                 // pops up as it shrinks
      board.material.color.setRGB(1, 1 - p * 0.7, 1 - p * 0.7);
      if (ring && ring.material) ring.material.opacity = 0.85 * (1 - p);
    } catch (e) { /* cosmetic only */ }
    if (p >= 1) this.dead = true;
    return this.dead;
  }
  /** Walk toward the player, stop at arm's length. Pure XZ; y stays on the floor. */
  step(dt, px, pz, t, others) {
    const g = this.group;
    if (this.dying) {                      // no steering, no separation
      this.distance = Math.hypot(g.position.x - px, g.position.z - pz);
      this._die(dt);
      faceCameraY(g, px, pz);
      return;
    }
    if (this.leaving) {                    // heading for the door
      const dx = g.position.x - px, dz = g.position.z - pz;
      const d = Math.hypot(dx, dz) || 1e-6;
      const sp = this.speed * 1.6 * dt;     // they do not dawdle
      g.position.x += (dx / d) * sp;
      g.position.z += (dz / d) * sp;
      this.distance = d + sp;
      this.phase += dt * 5;
      try { g.userData.board.position.y = Math.abs(Math.sin(this.phase)) * 0.035; } catch (e) {}
      if (this.distance > LEAVE_DISTANCE) this.dead = true;
      faceCameraY(g, px, pz);
      return;
    }
    if (this.flashT > 0) {
      this.flashT -= dt * 1000;
      try {
        const c = this.group.userData.board.material.color;
        if (this.flashT > 0) c.setRGB(1, 0.35, 0.3); else c.setRGB(1, 1, 1);
      } catch (e) { /* cosmetic only */ }
    }
    let dx = px - g.position.x;
    let dz = pz - g.position.z;
    const dist = Math.hypot(dx, dz) || 1e-6;
    this.distance = dist;

    const stopAt = this.stopAt;
    if (dist > stopAt) {
      this.arrived = false;
      const ux = dx / dist, uz = dz / dist;
      let mx = ux * this.speed * dt;
      let mz = uz * this.speed * dt;
      // do not overshoot past arm's length in one step
      const room = dist - stopAt;
      const stepLen = Math.hypot(mx, mz);
      if (stepLen > room) { const k = room / stepLen; mx *= k; mz *= k; }
      g.position.x += mx;
      g.position.z += mz;
      // walk bob
      this.phase += dt * (4.5 + this.speed * 2.2);
      // Bob the board a little; the pivot is at the feet so it reads as a
      // walk cycle rather than the sprite sliding off the floor.
      g.userData.board.position.y = Math.abs(Math.sin(this.phase)) * 0.035;
    } else {
      this.arrived = true;
      // idle sway once they have closed in
      g.userData.board.position.y = Math.abs(Math.sin(t * 0.0022 + this.phase)) * 0.012;
    }

    // minimal mutual separation
    if (others) {
      for (const o of others) {
        if (o === this) continue;
        const ox = g.position.x - o.group.position.x;
        const oz = g.position.z - o.group.position.z;
        const d2 = ox * ox + oz * oz;
        if (d2 > 1e-6 && d2 < SEP_RADIUS * SEP_RADIUS) {
          const d = Math.sqrt(d2);
          const push = (SEP_RADIUS - d) * 0.5;
          g.position.x += (ox / d) * push;
          g.position.z += (oz / d) * push;
        }
      }
    }

    // Hard invariant, applied AFTER separation: nothing gets closer than
    // arm's length. Without this, mobs converging on the player shove each
    // other through the stop radius (measured: 0.15 m against a 0.70 m ring).
    {
      const ddx = g.position.x - px, ddz = g.position.z - pz;
      const d = Math.hypot(ddx, ddz);
      if (d < stopAt) {
        if (d > 1e-4) {
          const k = stopAt / d;
          g.position.x = px + ddx * k;
          g.position.z = pz + ddz * k;
        } else {
          g.position.x = px + stopAt;         // degenerate: player inside the mob
          g.position.z = pz;
        }
        this.arrived = true;
      }
      this.distance = Math.hypot(g.position.x - px, g.position.z - pz);
    }

    // Billboard on Y ONLY -- upright relative to the floor at every camera
    // pitch. This is the whole difference between DOOM and floating stickers.
    faceCameraY(g, px, pz);
  }
  /** Biome accent on the floor ring, so a world swap reads instantly. */
  setAccent(hex) {
    try {
      this.accent = hex;
      const ring = this.group.userData.ring;
      if (ring && ring.material && ring.material.color) {
        ring.material.color.set(hex);
        ring.material.opacity = 0.85;
      }
    } catch (e) { /* cosmetic only */ }
  }
  /**
   * Handheld target cycling: the targeted mob's floor ring goes white and
   * grows; untargeting restores the biome accent. Colour and scale only, no
   * allocation, so it is safe to call from the frame loop.
   */
  setTargeted(on) {
    try {
      this.targeted = !!on;
      const ring = this.group.userData.ring;
      if (!ring || !ring.material) return;
      if (on) {
        ring.material.color.set(0xffffff);
        ring.material.opacity = 1.0;
        ring.scale.set(1.5, 1.5, 1.5);
      } else {
        ring.material.color.set(this.accent || 0x101418);
        ring.material.opacity = this.accent ? 0.85 : 0.5;
        ring.scale.set(1, 1, 1);
      }
    } catch (e) { /* cosmetic only */ }
  }
  dispose(scene) {
    scene.remove(this.group);
    this.group.traverse(o => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) o.material.dispose();
    });
  }
}

/**
 * Move an existing ring to a new centre. Used at world-swap time: the geometry
 * was built during the countdown, but the player has walked since, so the ring
 * is repositioned rather than rebuilt. Pure arithmetic -- no allocation, so it
 * is safe to do inside the swap frame.
 */
export function placeRing(mobs, cx, cz, { radius = 2.6, jitter = 0.45 } = {}) {
  const n = mobs.length || 1;
  mobs.forEach((m, i) => {
    const a = (i / n) * Math.PI * 2 + (m.spawnAngleJitter || 0);
    const r = radius + (m.spawnRadiusJitter || 0) * jitter;
    m.group.position.set(cx + Math.cos(a) * r, 0, cz + Math.sin(a) * r);
    m.arrived = false;
    m.distance = r;
  });
}

/**
 * Ring spawn around a centre point, on the floor plane.
 * The world knobs (count/speed/brute bias) are the same ones the Stage 3
 * server will return, so swapping an enemy set later is a re-spawn, not a
 * rewrite.
 */
export function spawnRing(scene, cx, cz, {
  count = 6, radius = 3.6, speedScale = 1.0, bruteBias = 0.15, jitter = 0.45,
  accent = null, visible = true, biome = null, hpBonus = 0, mood = 'hostile',
} = {}) {
  const mobs = [];
  const n = Math.max(1, Math.round(count));
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2 + Math.random() * 0.25;
    const r = radius + (Math.random() - 0.5) * 2 * jitter;
    const roll = Math.random();
    const kind = roll < bruteBias ? 'brute' : (roll < bruteBias + 0.25 ? 'runner' : 'walker');
    const base = kind === 'brute' ? 0.30 : kind === 'runner' ? 0.78 : 0.46;
    const mob = new Mob(kind, cx + Math.cos(a) * r, cz + Math.sin(a) * r,
                        base * speedScale * (0.85 + Math.random() * 0.3), biome, hpBonus, mood);
    mob.spawnAngleJitter = Math.random() * 0.25;
    mob.spawnRadiusJitter = (Math.random() - 0.5) * 2;
    if (accent) mob.setAccent(accent);
    mob.group.visible = visible;
    scene.add(mob.group);
    mobs.push(mob);
  }
  return mobs;
}

// Thrown slashes. A swing no longer touches anything by itself: it launches a
// crescent that flies from the weapon, travels until it hits a mob or runs out
// of range, and deals damage on contact.
//
// Deliberate choices:
//   * The projectile is spawned from the RENDER LOOP, not the input handler,
//     because the camera's world matrix only exists there. Input latches a
//     count; the loop drains it.
//   * One slash damages ONE mob and is consumed. No piercing, so a kill is
//     always traceable to a throw.
//   * The sprite is drawn procedurally into a canvas -- no new asset, no
//     licence question, and it stays crisp because it is generated at the size
//     it is used.
//   * Physics and hit-testing are plain arithmetic on {x,y,z}, free of three
//     and of the DOM, so test_slash.mjs drives them headlessly.
import * as THREE from '../vendor/three.module.js';

// Aiming. A slash is thrown AT something: if a mob is inside this cone ahead
// of the camera it is aimed at directly, which is what "thrown at them" means
// and what makes the handheld's target cycling worth anything. Nothing in the
// cone means it flies straight ahead and can still connect on its own.
export const AIM = { coneDeg: 42, maxDist: 8.0, chest: 0.62 };

export const SLASH = {
  speed: 7.0,        // m/s -- crosses the 3.6 m spawn ring in about half a second
  range: 9.0,        // m before it expires
  radius: 0.55,      // m, XZ hit radius against a mob
  size: 0.55,        // m, on-screen size of the crescent
  damage: 1,
  spin: 5.0,         // rad/s, texture roll while it flies
  maxLive: 24,       // hard cap; a stuck loop can never allocate forever
};

/** Vertical extent of a mob a slash can connect with. */
export function verticalOverlap(slashY, mobY, mobHeight) {
  return slashY >= mobY - 0.25 && slashY <= mobY + mobHeight + 0.3;
}

/**
 * Does this slash connect with this mob? XZ distance plus a vertical band.
 * `mob` needs only { group: { position, userData.height } }.
 */
export function connects(pos, mob, radius = SLASH.radius) {
  if (!mob || mob.dying || mob.dead) return false;
  const g = mob.group;
  if (!g) return false;
  const dx = pos.x - g.position.x, dz = pos.z - g.position.z;
  if (!Number.isFinite(dx) || !Number.isFinite(dz)) return false;
  const h = (g.userData && g.userData.height) || 1.7;
  if (!verticalOverlap(pos.y, g.position.y || 0, h)) return false;
  return (dx * dx + dz * dz) <= radius * radius;
}

/** Where on a mob a throw is aimed: chest height, not the feet. */
export function aimPoint(mob) {
  const g = mob.group;
  const h = (g.userData && g.userData.height) || 1.7;
  return { x: g.position.x, y: (g.position.y || 0) + h * AIM.chest, z: g.position.z };
}

/**
 * The mob a throw should go for: nearest one inside the forward cone.
 * Pure arithmetic on the camera's forward vector, so it is testable headless.
 * Returns null when nothing qualifies (throw straight ahead).
 */
export function pickTarget(mobs, origin, forward, { coneDeg = AIM.coneDeg, maxDist = AIM.maxDist } = {}) {
  if (!mobs || !mobs.length) return null;
  const cosCone = Math.cos(coneDeg * Math.PI / 180);
  let best = null, bestD = Infinity;
  for (const m of mobs) {
    if (!m || m.dying || m.dead || !m.group) continue;
    const p = aimPoint(m);
    const dx = p.x - origin.x, dy = p.y - origin.y, dz = p.z - origin.z;
    const d = Math.hypot(dx, dy, dz);
    if (!(d > 1e-3) || d > maxDist) continue;
    const dot = (dx * forward.x + dy * forward.y + dz * forward.z) / d;
    if (dot < cosCone) continue;                 // behind, or too far off axis
    if (d < bestD) { bestD = d; best = m; }
  }
  return best;
}

let TEXTURE = null;
/** A crescent, drawn once. Returns null where there is no canvas (node). */
export function slashTexture() {
  if (TEXTURE !== null) return TEXTURE;
  if (typeof document === 'undefined') { TEXTURE = null; return null; }
  try {
    const S = 128;
    const c = document.createElement('canvas');
    c.width = S; c.height = S;
    const x = c.getContext('2d');
    x.clearRect(0, 0, S, S);
    // Two arcs, the inner one thicker at the middle: a sword arc, not a ring.
    for (const [r, w, alpha, col] of [[S * 0.40, S * 0.13, 1.0, '#eaf6ff'],
                                      [S * 0.40, S * 0.22, 0.35, '#7fd4ff']]) {
      x.beginPath();
      x.strokeStyle = col;
      x.globalAlpha = alpha;
      x.lineWidth = w;
      x.lineCap = 'round';
      x.arc(S / 2, S / 2, r, Math.PI * 0.15, Math.PI * 0.85);
      x.stroke();
    }
    x.globalAlpha = 1;
    const tex = new THREE.CanvasTexture(c);
    tex.magFilter = THREE.LinearFilter;
    tex.minFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.center = new THREE.Vector2(0.5, 0.5);
    TEXTURE = tex;
  } catch (e) { TEXTURE = null; }
  return TEXTURE;
}

class Slash {
  constructor(pos, dir, { speed, range, size, damage }) {
    this.x = pos.x; this.y = pos.y; this.z = pos.z;
    this.dx = dir.x; this.dy = dir.y; this.dz = dir.z;
    this.speed = speed; this.travelled = 0; this.range = range;
    this.damage = damage; this.size = size;
    this.done = false; this.roll = Math.random() * Math.PI * 2;
    // Nothing thrown should outlive its flight by much, and nothing at all
    // should still be in the air five seconds later.
    this.age = 0; this.maxLife = Math.min(5, (range / Math.max(0.01, speed)) * 1.5);
    this.mesh = null;
  }
  get position() { return this; }          // connects() takes {x,y,z}
  advance(dt) {
    const d = this.speed * dt;
    this.x += this.dx * d; this.y += this.dy * d; this.z += this.dz * d;
    this.travelled += d;
    this.age += dt;
    this.roll += SLASH.spin * dt;
    // Distance is the normal way out. The age cap is the backstop: a slash
    // that somehow stops moving must still leave, or it would sit in the
    // scene until the session ends.
    if (this.travelled >= this.range || this.age >= this.maxLife) this.done = true;
    return this.done;
  }
}

export class SlashSwarm {
  constructor(scene, opts = {}) {
    this.scene = scene;
    this.cfg = Object.assign({}, SLASH, opts);
    this.live = [];
    this.thrown = 0; this.hits = 0; this.kills = 0; this.expired = 0; this.aimed = 0;
    this._geo = null;
  }

  /** Aim straight out of the camera, with the pitch tamed so a natural
   *  hold still connects with something standing on the floor. */
  /**
   * Throw from the camera. `mobs` (optional) turns it into a throw AT someone:
   * an explicit `aimAt` mob wins, otherwise the nearest one in the forward
   * cone, otherwise it flies straight ahead.
   */
  spawnFromCamera(cameraMatrixWorld, mobs = null, aimAt = null) {
    if (!cameraMatrixWorld) return null;
    const e = cameraMatrixWorld.elements;
    for (let i = 0; i < 16; i++) if (!Number.isFinite(e[i])) return null;
    let fx = -e[8], fy = -e[9], fz = -e[10];
    const fl = Math.hypot(fx, fy, fz);
    // An all-zero matrix (before the first tracked frame) has no forward
    // direction; a slash launched along it would never travel and never
    // expire by distance.
    if (!(fl > 1e-6)) return null;
    fx /= fl; fy /= fl; fz /= fl;
    const origin = {
      x: e[12] + fx * 0.45 + e[0] * 0.12 - e[4] * 0.10,
      y: e[13] + fy * 0.45 + e[1] * 0.12 - e[5] * 0.10,
      z: e[14] + fz * 0.45 + e[2] * 0.12 - e[6] * 0.10,
    };
    const mark = (aimAt && !aimAt.dying && !aimAt.dead && aimAt.group)
      ? aimAt : pickTarget(mobs, origin, { x: fx, y: fy, z: fz }, this.cfg);
    if (mark) {
      const p = aimPoint(mark);
      const dx = p.x - origin.x, dy = p.y - origin.y, dz = p.z - origin.z;
      const d = Math.hypot(dx, dy, dz);
      if (d > 1e-3) {
        this.aimed++;
        return this.spawn(origin, { x: dx / d, y: dy / d, z: dz / d });
      }
    }
    fy = Math.max(-0.35, Math.min(0.35, fy));       // no throwing at your feet
    const rl = Math.hypot(fx, fz) || 1e-6;
    const k = Math.sqrt(Math.max(0, 1 - fy * fy)) / rl;
    fx *= k; fz *= k;
    return this.spawn(origin, { x: fx, y: fy, z: fz });
  }

  spawn(pos, dir) {
    if (this.live.length >= this.cfg.maxLive) return null;
    if (!dir || !(Math.hypot(dir.x, dir.y, dir.z) > 1e-6)) return null;
    for (const v of [pos.x, pos.y, pos.z, dir.x, dir.y, dir.z]) if (!Number.isFinite(v)) return null;
    const s = new Slash(pos, dir, this.cfg);
    this.thrown++;
    try { this._build(s); } catch (e) { /* visual only; it still damages */ }
    this.live.push(s);
    return s;
  }

  _build(s) {
    if (!this.scene || typeof document === 'undefined') return;
    if (!this._geo) this._geo = new THREE.PlaneGeometry(this.cfg.size, this.cfg.size);
    const tex = slashTexture();
    // Own material per slash: each one rolls its texture independently.
    const mat = new THREE.MeshBasicMaterial({
      map: tex, transparent: true, opacity: 1, depthWrite: false,
      side: THREE.DoubleSide, toneMapped: false, blending: THREE.AdditiveBlending,
    });
    if (mat.map) { mat.map = tex.clone(); mat.map.needsUpdate = true; mat.map.center = new THREE.Vector2(0.5, 0.5); }
    const mesh = new THREE.Mesh(this._geo, mat);
    mesh.frustumCulled = false;
    mesh.renderOrder = 8000;
    mesh.position.set(s.x, s.y, s.z);
    this.scene.add(mesh);
    s.mesh = mesh;
  }

  /**
   * Advance every slash, damage what it touches, drop what is spent.
   * Returns {hits, kills, killed} for THIS frame, where `killed` lists the
   * kinds that died so the caller can score them. Never throws.
   */
  update(dt, mobs, camX = 0, camZ = 0) {
    let hits = 0, kills = 0;
    const killed = [];              // the kinds killed, so the caller can score
    if (!(dt > 0)) dt = 0;
    for (let i = this.live.length - 1; i >= 0; i--) {
      const s = this.live[i];
      try {
        s.advance(dt);
        if (!s.done && mobs) {
          for (const m of mobs) {
            if (!connects(s, m, this.cfg.radius)) continue;
            const r = m.hit ? m.hit(s.damage) : { hit: false, dead: false };
            if (r && r.hit) {
              hits++; this.hits++;
              if (r.dead) { kills++; this.kills++; killed.push(m.kind || 'walker'); }
              s.done = true;
            }
            break;                                   // one slash, one mob
          }
        }
        if (s.mesh) {
          s.mesh.position.set(s.x, s.y, s.z);
          s.mesh.rotation.set(0, Math.atan2(camX - s.x, camZ - s.z), 0);
          if (s.mesh.material.map) s.mesh.material.map.rotation = s.roll;
          const left = 1 - s.travelled / s.range;
          s.mesh.material.opacity = Math.max(0, Math.min(1, left * 1.6));
        }
        if (s.done) {
          if (s.travelled >= s.range) this.expired++;
          this._retire(s);
          this.live.splice(i, 1);
        }
      } catch (e) {
        this._retire(s);
        this.live.splice(i, 1);
      }
    }
    return { hits, kills, killed };
  }

  _retire(s) {
    try {
      if (s.mesh) {
        this.scene.remove(s.mesh);
        if (s.mesh.material.map) s.mesh.material.map.dispose();
        s.mesh.material.dispose();
        s.mesh = null;
      }
    } catch (e) { /* nothing left to do */ }
  }

  dispose() {
    for (const s of this.live) this._retire(s);
    this.live.length = 0;
    try { if (this._geo) this._geo.dispose(); } catch (e) {}
    this._geo = null;
  }

  stats() {
    return { thrown: this.thrown, hits: this.hits, kills: this.kills,
             expired: this.expired, aimed: this.aimed, live: this.live.length };
  }
}

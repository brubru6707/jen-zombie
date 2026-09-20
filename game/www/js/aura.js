// The green glow around the peaceful mobs.
//
// Pixel motes that rise and fade over anything calm, so "this one will not
// hurt you" is legible across a room at a glance -- the sprite swap alone is
// too subtle when both are 30 px tall and moving.
//
// One THREE.Points for the WHOLE room, not one per mob: a single draw call, a
// single buffer, and a fixed pool allocated once. The per-frame work is
// arithmetic into typed arrays, with no allocation at all, because this runs
// inside the render loop on a phone that is already busy.
//
// Motes belong to a mob for their lifetime and are recycled when it expires,
// leaves or dies. Unused motes park below the floor where nothing can see
// them, which costs one vertex each rather than a rebuild of the geometry.
import * as THREE from '../vendor/three.module.js';

export const AURA = {
  perMob: 12,          // motes following each calm mob
  maxMobs: 12,         // pool ceiling; beyond this the extras simply get none
  size: 0.075,         // metres per mote
  radius: 0.42,        // how far they drift from the mob's centre
  rise: 0.85,          // metres they climb over a life
  life: 1.9,           // seconds
  colour: 0x6bff8f,
  parked: -999,        // y for a mote with nothing to follow
};

/** A square, soft-edged mote. Null where there is no canvas (node). */
function moteTexture() {
  if (typeof document === 'undefined') return null;
  try {
    const S = 16;
    const c = document.createElement('canvas');
    c.width = S; c.height = S;
    const x = c.getContext('2d');
    x.fillStyle = '#ffffff';
    x.fillRect(3, 3, S - 6, S - 6);          // a pixel square, not a blurry dot
    x.globalAlpha = 0.45;
    x.fillRect(1, 1, S - 2, S - 2);
    const t = new THREE.CanvasTexture(c);
    t.magFilter = THREE.NearestFilter;
    t.minFilter = THREE.NearestFilter;
    t.generateMipmaps = false;
    return t;
  } catch (e) { return null; }
}

export class Aura {
  constructor(scene, opts = {}) {
    this.scene = scene;
    this.cfg = Object.assign({}, AURA, opts);
    this.count = this.cfg.perMob * this.cfg.maxMobs;
    // Fixed pool. Everything below is written in place, for ever.
    this.pos = new Float32Array(this.count * 3);
    this.alpha = new Float32Array(this.count);
    this.age = new Float32Array(this.count);
    this.lifeOf = new Float32Array(this.count);
    this.angle = new Float32Array(this.count);
    this.radiusOf = new Float32Array(this.count);
    this.owner = new Int16Array(this.count).fill(-1);
    for (let i = 0; i < this.count; i++) {
      this.pos[i * 3 + 1] = this.cfg.parked;
      this.lifeOf[i] = this.cfg.life * (0.7 + Math.random() * 0.6);
      this.age[i] = Math.random() * this.lifeOf[i];
      this.angle[i] = Math.random() * Math.PI * 2;
      this.radiusOf[i] = this.cfg.radius * (0.35 + Math.random() * 0.65);
    }
    this.points = null;
    this.tracked = 0;                  // calm mobs drawn on the last update
    try { this._build(); } catch (e) { this.points = null; }
  }

  _build() {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(this.pos, 3));
    geo.setAttribute('aAlpha', new THREE.BufferAttribute(this.alpha, 1));
    const mat = new THREE.PointsMaterial({
      color: this.cfg.colour,
      size: this.cfg.size,
      map: moteTexture(),
      transparent: true,
      opacity: 0.95,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      sizeAttenuation: true,
      toneMapped: false,
    });
    // Per-mote fade, without writing a whole shader: PointsMaterial does not
    // take a per-vertex alpha, so the attribute is folded into the existing
    // program with the smallest possible hook.
    mat.onBeforeCompile = (shader) => {
      shader.vertexShader = 'attribute float aAlpha;\nvarying float vAlpha;\n' +
        shader.vertexShader.replace('void main() {', 'void main() {\n  vAlpha = aAlpha;');
      shader.fragmentShader = 'varying float vAlpha;\n' +
        shader.fragmentShader.replace(
          'vec4 diffuseColor = vec4( diffuse, opacity );',
          'vec4 diffuseColor = vec4( diffuse, opacity * vAlpha );');
    };
    const pts = new THREE.Points(geo, mat);
    pts.frustumCulled = false;         // the pool spans the whole room
    pts.renderOrder = 7000;
    this.scene.add(pts);
    this.points = pts;
    this.geo = geo;
    this.mat = mat;
  }

  /** A mob that should be wearing an aura right now. */
  static wants(m) {
    return !!m && m.mood === 'calm' && !m.leaving && !m.dying && !m.dead && !!m.group;
  }

  /**
   * Call once a frame with the whole mob list. Never throws, never allocates.
   * Returns the number of calm mobs it is decorating.
   */
  update(dt, mobs) {
    if (!(dt > 0)) dt = 0;
    let calm = 0;
    try {
      const per = this.cfg.perMob, max = this.cfg.maxMobs;
      // Assign the first `maxMobs` calm mobs to consecutive blocks of motes.
      // A block whose mob has gone quiet parks below the floor.
      let slot = 0;
      for (let i = 0; i < mobs.length && slot < max; i++) {
        const m = mobs[i];
        if (!Aura.wants(m)) continue;
        calm++;
        const base = slot * per;
        const gx = m.group.position.x, gy = m.group.position.y || 0, gz = m.group.position.z;
        const h = (m.group.userData && m.group.userData.height) || 1.7;
        for (let k = 0; k < per; k++) {
          const p = base + k;
          if (this.owner[p] !== slot) { this.owner[p] = slot; this.age[p] = Math.random() * this.lifeOf[p]; }
          this.age[p] += dt;
          if (this.age[p] >= this.lifeOf[p]) {      // recycle in place
            this.age[p] = 0;
            this.angle[p] = Math.random() * Math.PI * 2;
            this.radiusOf[p] = this.cfg.radius * (0.35 + Math.random() * 0.65);
            this.lifeOf[p] = this.cfg.life * (0.7 + Math.random() * 0.6);
          }
          const u = this.age[p] / this.lifeOf[p];               // 0..1 of its life
          const a = this.angle[p] + u * 1.6;                    // a slow spiral
          const r = this.radiusOf[p] * (0.55 + u * 0.45);
          const i3 = p * 3;
          this.pos[i3] = gx + Math.cos(a) * r;
          this.pos[i3 + 1] = gy + h * 0.18 + u * this.cfg.rise;
          this.pos[i3 + 2] = gz + Math.sin(a) * r;
          // Fade in over the first fifth, out over the last half.
          this.alpha[p] = u < 0.2 ? u / 0.2 : (u > 0.5 ? (1 - u) / 0.5 : 1);
        }
        slot++;
      }
      this.tracked = slot;
      for (let p = slot * per; p < this.count; p++) {           // park the rest
        if (this.owner[p] === -1 && this.pos[p * 3 + 1] === this.cfg.parked) continue;
        this.owner[p] = -1;
        this.pos[p * 3 + 1] = this.cfg.parked;
        this.alpha[p] = 0;
      }
      if (this.geo) {
        this.geo.attributes.position.needsUpdate = true;
        this.geo.attributes.aAlpha.needsUpdate = true;
      }
    } catch (e) { /* decoration must never cost a frame */ }
    return calm;
  }

  dispose() {
    try {
      if (this.points) this.scene.remove(this.points);
      if (this.geo) this.geo.dispose();
      if (this.mat) { if (this.mat.map) this.mat.map.dispose(); this.mat.dispose(); }
    } catch (e) {}
    this.points = null; this.geo = null; this.mat = null;
  }

  stats() { return { calm: this.tracked, motes: this.tracked * this.cfg.perMob, pool: this.count }; }
}

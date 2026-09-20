// Biome weapon held in the right hand, DOOM viewmodel style.
//
// The weapon is NOT parented to the camera (three's XR camera is an ArrayCamera
// and reparenting it is fragile). Instead its world matrix is recomputed each
// frame as cameraMatrix * localOffset, which gives identical results and
// survives the XR camera being rebuilt.
//
// It draws on top of everything (depthTest off, high renderOrder) because a
// viewmodel that clips into a nearby mob looks broken.
import * as THREE from '../vendor/three.module.js';

const ASSET_DIR = './assets/kenney/';

// One weapon per biome. Plain table, same as the mob mapping.
export const BIOME_WEAPONS = {
  lab:    'staff_arc',    // arc-discharge rod
  rocky:  'hammer',       // sledgehammer
  cave:   'torch',        // you carry a torch underground
  desert: 'pickaxe',      // digging tool for sand
  forest: 'axe',          // felling axe
  meadow: 'sword',
  snow:   'staff_ice',
  water:  'cleaver',
  street: 'bat',          // baseball bat
  farm:   'battleaxe',    // splitting maul
};
const FALLBACK_WEAPON = 'bat';

export function weaponFor(biome) {
  return BIOME_WEAPONS[biome] || FALLBACK_WEAPON;
}

// Where the weapon sits in camera space: right of centre, below the eyeline,
// a little ahead. Right-handed grip.
// Tuned against the phone's landscape FOV. The first pass sat ~36 deg below
// the view axis, which put it off the bottom of the screen and behind the mic
// control bar -- it rendered fine and simply could not be seen.
// Angular size is what matters, not distance. At 0.5 m a 0.4 m sprite subtends
// ~44 deg and you just see a few enormous pixels; pushed back to 0.9 m the same
// sprite subtends ~25 deg and reads as a weapon held in front of you.
const REST = {
  pos: new THREE.Vector3(0.30, -0.20, -0.90),
  rotZ: -0.38,      // canted, so it reads as held rather than floating
  rotX: 0.10,
  scale: 0.40,      // metres of sprite height at rest
};
const GRIP_OFFSET = -0.15;   // fraction of height: pivot near the handle
const SWING_MS = 320;

const textures = new Map();
let manifest = null;

export async function preloadWeapons() {
  if (manifest) return manifest;
  const res = await fetch(ASSET_DIR + 'sprites.json', { cache: 'force-cache' });
  manifest = await res.json();
  const names = Object.keys(manifest.weapons || {});
  const loader = new THREE.TextureLoader();
  await Promise.all(names.map(name => new Promise(resolve => {
    loader.load(ASSET_DIR + manifest.weapons[name].file, tex => {
      tex.magFilter = THREE.NearestFilter;
      tex.minFilter = THREE.NearestFilter;
      tex.generateMipmaps = false;
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.needsUpdate = true;
      textures.set(name, tex);
      resolve();
    }, undefined, () => resolve());
  })));
  return manifest;
}

export class Weapon {
  constructor(scene) {
    this.scene = scene;
    this.name = null;
    this.mesh = null;
    this.swingStart = -1;
    this.swings = 0;
    this._m = new THREE.Matrix4();
    this._local = new THREE.Matrix4();
    this._pos = new THREE.Vector3();
    this._quat = new THREE.Quaternion();
    this._scl = new THREE.Vector3(1, 1, 1);
    this._euler = new THREE.Euler();
  }

  /** Swap to the weapon for this biome. Cheap: geometry is rebuilt only on change. */
  setBiome(biome) {
    const name = weaponFor(biome);
    if (name === this.name && this.mesh) return name;
    this.name = name;
    const tex = textures.get(name);
    const meta = (manifest && manifest.weapons && manifest.weapons[name]) || { aspect: 0.5 };
    const h = REST.scale;
    const w = h * (meta.aspect || 0.5);
    const geo = new THREE.PlaneGeometry(w, h);
    geo.translate(0, h * GRIP_OFFSET, 0);   // pivot near the handle
    const mat = new THREE.MeshBasicMaterial({
      map: tex || null,
      alphaTest: 0.5,          // same cutout treatment as the mobs
      transparent: false,
      side: THREE.DoubleSide,
      depthTest: false,        // always drawn over the world
      depthWrite: false,
      toneMapped: false,
    });
    if (this.mesh) this.dispose();
    this.mesh = new THREE.Mesh(geo, mat);
    this.mesh.renderOrder = 9999;
    this.mesh.frustumCulled = false;
    this.mesh.matrixAutoUpdate = false;
    this.scene.add(this.mesh);
    return name;
  }

  swing(now) {
    if (this.swinging(now)) return false;    // no queueing mid-swing
    this.swingStart = now;
    this.swings++;
    return true;
  }
  swinging(now) {
    return this.swingStart >= 0 && (now - this.swingStart) < SWING_MS;
  }
  /** 0..1 through the swing, or 0 when at rest. */
  swingPhase(now) {
    if (this.swingStart < 0) return 0;
    const p = (now - this.swingStart) / SWING_MS;
    return p >= 1 ? 0 : p;
  }

  /** Recompute the viewmodel transform. Never throws. */
  update(now, cameraMatrixWorld) {
    this.lastReason = null;
    if (!this.mesh) { this.lastReason = 'no mesh'; return; }
    if (!cameraMatrixWorld) { this.lastReason = 'no camera matrix'; return; }
    // A degenerate camera matrix (all zeros before the first tracked frame)
    // would collapse the viewmodel to a point. Keep the last good transform.
    const e = cameraMatrixWorld.elements;
    let finite = true, nonzero = false;
    for (let i = 0; i < 16; i++) {
      if (!Number.isFinite(e[i])) { finite = false; break; }
      if (e[i] !== 0) nonzero = true;
    }
    if (!finite || !nonzero) { this.lastReason = 'degenerate camera matrix'; return; }
    try {
      const p = this.swingPhase(now);
      // Arc: wind up slightly, chop down and across, then ease back.
      const swing = Math.sin(p * Math.PI);          // 0 -> 1 -> 0
      const chop = Math.sin(p * Math.PI) * (p < 0.5 ? 1 : 0.85);
      this._pos.set(
        REST.pos.x - swing * 0.26,
        REST.pos.y + swing * 0.16,
        REST.pos.z + swing * 0.12,
      );
      this._euler.set(
        REST.rotX - chop * 1.25,
        swing * 0.35,
        REST.rotZ + chop * 1.05,
        'XYZ',
      );
      this._quat.setFromEuler(this._euler);
      this._local.compose(this._pos, this._quat, this._scl);
      this._m.multiplyMatrices(cameraMatrixWorld, this._local);
      this.mesh.matrix.copy(this._m);
      this.mesh.matrixWorldNeedsUpdate = true;
      this.lastReason = 'ok';
    } catch (e) {
      this.lastReason = 'update threw: ' + ((e && e.message) || e);
    }
  }

  /** Diagnostic snapshot for the dashboard. */
  debug() {
    const out = { name: this.name, reason: this.lastReason,
                  hasMesh: !!this.mesh, hasMap: !!(this.mesh && this.mesh.material.map) };
    if (this.mesh) {
      const p = new THREE.Vector3().setFromMatrixPosition(this.mesh.matrix);
      out.pos = [ +p.x.toFixed(2), +p.y.toFixed(2), +p.z.toFixed(2) ];
      out.inScene = this.mesh.parent === this.scene;
      out.visible = this.mesh.visible;
    }
    return out;
  }

  dispose() {
    try {
      if (this.mesh) {
        this.scene.remove(this.mesh);
        this.mesh.geometry.dispose();
        this.mesh.material.dispose();
      }
    } catch (e) {}
    this.mesh = null;
  }
}

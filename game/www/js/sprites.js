// DOOM-style flat sprites standing in the 3D world.
//
// The four details that decide whether this reads as DOOM or as stickers
// floating in the air:
//
//  1. BILLBOARD ON Y ONLY. A THREE.Sprite billboards on every axis, so the mob
//     tilts backwards when you look down at it and the illusion dies. We use a
//     PlaneGeometry and rotate it about Y alone, so it stays perpendicular to
//     the floor no matter where the camera is.
//  2. NearestFilter on BOTH mag and min, generateMipmaps false. Default
//     filtering turns 16px art to mush, worst at distance.
//  3. alphaTest instead of transparent blending: hard cutout edges and no
//     depth-sort artifacts when two mobs overlap.
//  4. Anchored at the BOTTOM edge, not the centre, so feet meet the floor
//     plane. Sized to ~1.7 m for a humanoid -- in AR the player has a
//     real-world size reference and wrong scale is glaring.
import * as THREE from '../vendor/three.module.js';

export const HUMANOID_HEIGHT_M = 1.7;
export const ALPHA_TEST = 0.5;
const ASSET_DIR = './assets/kenney/';

// Biome -> [walker, runner, brute]. Deliberately a plain table.
export const BIOME_SPRITES = {
  lab:    ['armored',  'bat',      'knight'],
  rocky:  ['armored',  'spider',   'horned'],
  cave:   ['spider',   'bat',      'beast'],
  desert: ['scorpion', 'crab',     'barbarian'],
  forest: ['ranger',   'spider',   'beast'],
  meadow: ['ape',      'caveman',  'barbarian'],
  snow:   ['ghost',    'bat',      'elder'],
  water:  ['slime',    'crab',     'scorpion'],
  street: ['zombie',   'ranger',   'knight'],
  farm:   ['zombie',   'bat',      'ape'],
};
const FALLBACK = ['zombie', 'bat', 'knight'];

// The other half of the room. When it goes quiet the hostile crowd leaves and
// these wander in: people and animals rather than armour and teeth, so the
// change reads instantly without a caption. Every biome has a full row --
// nowhere in the game can go quiet and have nobody to show for it -- and
// nothing armed or fanged appears here even in the brute slot, because these
// are the ones that will not hurt you.
export const CALM_SPRITES = {
  lab:    ['elder',   'ranger',  'elder'],
  rocky:  ['caveman', 'ape',     'caveman'],
  cave:   ['caveman', 'slime',   'ape'],
  desert: ['ranger',  'elder',   'caveman'],
  forest: ['elder',   'ape',     'caveman'],
  meadow: ['elder',   'ranger',  'ape'],
  snow:   ['elder',   'caveman', 'ape'],
  water:  ['crab',    'slime',   'crab'],
  street: ['ranger',  'elder',   'caveman'],
  farm:   ['caveman', 'ape',     'elder'],
};
const CALM_FALLBACK = ['elder', 'ranger', 'ape'];

export function spriteFor(biome, kind, mood = 'hostile') {
  const table = mood === 'calm' ? CALM_SPRITES : BIOME_SPRITES;
  const row = table[biome] || (mood === 'calm' ? CALM_FALLBACK : FALLBACK);
  const i = kind === 'runner' ? 1 : kind === 'brute' ? 2 : 0;
  return row[i] || row[0];
}

const textures = new Map();      // name -> THREE.Texture
let manifest = null;

/** Load the manifest and every texture once, before anything spawns. */
export async function preloadSprites() {
  if (manifest) return manifest;
  const res = await fetch(ASSET_DIR + 'sprites.json', { cache: 'force-cache' });
  manifest = await res.json();
  const loader = new THREE.TextureLoader();
  const names = Object.keys(manifest.sprites);
  await Promise.all(names.map(name => new Promise(resolve => {
    loader.load(ASSET_DIR + manifest.sprites[name].file, tex => {
      tex.magFilter = THREE.NearestFilter;      // crisp up close
      tex.minFilter = THREE.NearestFilter;      // crisp at distance too
      tex.generateMipmaps = false;
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.needsUpdate = true;
      textures.set(name, tex);
      resolve();
    }, undefined, () => resolve());             // a missing file must not hang
  })));
  return manifest;
}

export function spriteAspect(name) {
  const m = manifest && manifest.sprites && manifest.sprites[name];
  return m ? m.aspect : 1;
}

/**
 * A bottom-anchored billboard plane. Returns a Mesh whose local origin is at
 * the FEET, so placing it at y=0 puts it on the floor.
 */
export function makeBillboard(name, heightM) {
  const tex = textures.get(name);
  const aspect = spriteAspect(name);
  const h = heightM;
  const w = h * aspect;
  const geo = new THREE.PlaneGeometry(w, h);
  geo.translate(0, h / 2, 0);                   // pivot at the bottom edge
  const mat = new THREE.MeshBasicMaterial({
    map: tex || null,
    alphaTest: ALPHA_TEST,   // cutout, NOT blended: hard edges, correct depth
    transparent: false,
    side: THREE.DoubleSide,
    color: 0xffffff,
    toneMapped: false,
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.frustumCulled = false;
  return mesh;
}

/**
 * Face the camera horizontally ONLY. yawTo is the camera's world XZ.
 * No pitch, no roll -- the sprite stays upright relative to the floor.
 */
export function faceCameraY(object3d, camX, camZ) {
  const dx = camX - object3d.position.x;
  const dz = camZ - object3d.position.z;
  if (dx === 0 && dz === 0) return;
  object3d.rotation.set(0, Math.atan2(dx, dz), 0);
}

export function spriteNames() { return Array.from(textures.keys()); }

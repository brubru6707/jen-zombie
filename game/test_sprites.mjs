// Verifies the four billboard details that decide whether this looks like DOOM.
import * as THREE from './www/vendor/three.module.js';
import { makeBillboard, faceCameraY, spriteFor, preloadSprites,
         BIOME_SPRITES, CALM_SPRITES, HUMANOID_HEIGHT_M, ALPHA_TEST } from './www/js/sprites.js';
import { spawnRing } from './www/js/mobs.js';

let failures = 0;
const check = (n, c, d='') => {
  console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++;
};
const near = (a,b,e=1e-4) => Math.abs(a-b) < e;

// ---- 1. filtering: stub the loader so we can inspect what gets configured ---
console.log('--- 2. texture filtering (NearestFilter, no mipmaps) ---');
{
  const made = [];
  THREE.TextureLoader.prototype.load = function (url, onLoad) {
    const t = new THREE.Texture();
    made.push(t);
    setTimeout(() => onLoad(t), 0);
    return t;
  };
  globalThis.fetch = async () => ({
    json: async () => ({ sprites: {
      zombie:{file:'zombie.png',w:16,h:15,aspect:16/15},
      bat:{file:'bat.png',w:16,h:12,aspect:16/12},
      ghost:{file:'ghost.png',w:16,h:15,aspect:16/15},
      scorpion:{file:'scorpion.png',w:16,h:16,aspect:1},
      knight:{file:'knight.png',w:16,h:15,aspect:16/15},
    }}),
  });
  await preloadSprites();
  check('every texture uses NearestFilter for magFilter',
        made.every(t => t.magFilter === THREE.NearestFilter));
  check('every texture uses NearestFilter for minFilter (crisp at distance)',
        made.every(t => t.minFilter === THREE.NearestFilter));
  check('mipmaps are disabled', made.every(t => t.generateMipmaps === false));
  check('textures were actually created', made.length === 5, `${made.length} textures`);
}

// ---- 3 + 4: material and anchoring ----------------------------------------
console.log('--- 3. cutout material, not blended ---');
{
  const m = makeBillboard('zombie', 1.7);
  check('alphaTest is set', near(m.material.alphaTest, ALPHA_TEST), String(m.material.alphaTest));
  check('transparent blending is OFF', m.material.transparent === false);
  check('it is a plane, not a THREE.Sprite',
        m.isSprite !== true && m.geometry.type === 'PlaneGeometry', m.geometry.type);
}

console.log('--- 4. bottom-anchored, ~1.7 m humanoid ---');
{
  const h = 1.7;
  const m = makeBillboard('zombie', h);
  m.geometry.computeBoundingBox();
  const bb = m.geometry.boundingBox;
  check('pivot sits at the FEET (min y = 0)', near(bb.min.y, 0), `min.y=${bb.min.y.toFixed(4)}`);
  check('top of the sprite is one height up', near(bb.max.y, h), `max.y=${bb.max.y.toFixed(4)}`);
  check('humanoid is ~1.7 m tall', near(bb.max.y - bb.min.y, HUMANOID_HEIGHT_M),
        `${(bb.max.y-bb.min.y).toFixed(2)} m`);
  const wide = makeBillboard('bat', 1.0);
  wide.geometry.computeBoundingBox();
  const bw = wide.geometry.boundingBox;
  check('width follows the sprite aspect, not forced square',
        near(bw.max.x - bw.min.x, 16/12, 1e-3), `w=${(bw.max.x-bw.min.x).toFixed(3)}`);
}

// ---- 1. THE BIG ONE: Y-axis-only billboard ---------------------------------
console.log('--- 1. billboard on Y ONLY (no tilt when looking down/up) ---');
{
  const o = new THREE.Object3D();
  o.position.set(0, 0, 0);
  // Camera directly in front, at head height, then crouching, then overhead.
  for (const [label, cx, cz] of [['in front', 0, 3], ['behind', 0, -3],
                                 ['left', -3, 0], ['right', 3, 0],
                                 ['diagonal', 2.5, 2.5]]) {
    faceCameraY(o, cx, cz);
    const okAxis = o.rotation.x === 0 && o.rotation.z === 0;
    const expected = Math.atan2(cx - 0, cz - 0);
    check(`${label}: upright (x=0,z=0) and yaw correct`,
          okAxis && near(o.rotation.y, expected),
          `x=${o.rotation.x} z=${o.rotation.z} y=${o.rotation.y.toFixed(3)}`);
  }
  // Camera height must not matter at all -- that is the tilt bug.
  faceCameraY(o, 0, 3);
  const yawAtHeadHeight = o.rotation.y;
  faceCameraY(o, 0, 3);                   // same XZ, imagine camera 2 m higher
  check('camera height never changes the sprite orientation',
        near(o.rotation.y, yawAtHeadHeight) && o.rotation.x === 0 && o.rotation.z === 0);
  // Degenerate: camera exactly on top of the mob
  o.rotation.set(0, 1.234, 0);
  faceCameraY(o, 0, 0);
  check('camera exactly overhead does not produce NaN or a flip',
        Number.isFinite(o.rotation.y) && o.rotation.x === 0 && o.rotation.z === 0,
        `y=${o.rotation.y}`);
}

console.log('--- biome mapping ---');
{
  check('all ten biomes are mapped', Object.keys(BIOME_SPRITES).length === 10,
        Object.keys(BIOME_SPRITES).join(','));
  check('desert and snow get visibly different walkers',
        spriteFor('desert','walker') !== spriteFor('snow','walker'),
        `${spriteFor('desert','walker')} vs ${spriteFor('snow','walker')}`);
  check('each biome differentiates walker/runner/brute',
        Object.entries(BIOME_SPRITES).every(([,r]) => new Set(r).size === 3));
  check('an unknown biome still yields a sprite', !!spriteFor('lava','walker'),
        spriteFor('lava','walker'));
}

console.log('--- existing guarantees survive the sprite swap ---');
{
  const scene = { add(){}, remove(){} };
  const mobs = spawnRing(scene, 0, 0, { count: 6, biome: 'desert', accent: '#e0b050' });
  check('ring still spawns the requested count', mobs.length === 6);
  check('feet start on the floor plane', mobs.every(m => m.group.position.y === 0));
  check('biome reached the mobs', mobs.every(m => m.biome === 'desert'));
  let minD = Infinity;
  for (let i = 0; i < 1200; i++) {
    for (const m of mobs) m.step(1/60, 0, 0, i*16.7, mobs);
    for (const m of mobs) minD = Math.min(minD, Math.hypot(m.group.position.x, m.group.position.z));
  }
  check('stop distance still holds', minD >= 2.0 - 1e-6, `min=${minD.toFixed(4)} m`);
  check('feet stayed on the floor throughout', mobs.every(m => m.group.position.y === 0));
  check('sprites differ by biome at spawn time',
        spawnRing(scene,0,0,{count:3,biome:'snow'})[0].group.userData.sprite !==
        spawnRing(scene,0,0,{count:3,biome:'desert'})[0].group.userData.sprite);
}

console.log('--- every biome has peaceful mobs too ---');
{
  const hostile = Object.keys(BIOME_SPRITES), calm = Object.keys(CALM_SPRITES);
  check('every biome that has hostile mobs also has calm ones',
        hostile.every(b => calm.includes(b)) && calm.length === hostile.length,
        `${hostile.length} biomes`);
  check('each calm row covers walker, runner and brute',
        calm.every(b => CALM_SPRITES[b].length === 3 && CALM_SPRITES[b].every(n => typeof n === 'string' && n)));
  const armed = ['knight', 'armored', 'barbarian', 'zombie', 'ghost', 'beast', 'horned', 'spider', 'scorpion', 'bat'];
  check('nothing armed or fanged appears in a calm row',
        calm.every(b => CALM_SPRITES[b].every(n => !armed.includes(n))),
        [...new Set(calm.flatMap(b => CALM_SPRITES[b]))].join(','));
  check('a calm mob never looks like the hostile one for the same biome and kind',
        calm.every(b => [0, 1, 2].every(i => CALM_SPRITES[b][i] !== BIOME_SPRITES[b][i])));
  check('spriteFor picks the calm table when asked',
        calm.every(b => spriteFor(b, 'walker', 'calm') === CALM_SPRITES[b][0] &&
                        spriteFor(b, 'brute', 'calm') === CALM_SPRITES[b][2]));
  check('and the hostile table by default', hostile.every(b => spriteFor(b, 'walker') === BIOME_SPRITES[b][0]));
  check('an unknown biome still yields a calm sprite', !!spriteFor('lava', 'walker', 'calm'));
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all sprite contracts hold');
process.exit(failures ? 1 : 0);

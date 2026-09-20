import * as THREE from './www/vendor/three.module.js';
import { Weapon, preloadWeapons, weaponFor, BIOME_WEAPONS } from './www/js/weapon.js';
import { STOP_DISTANCE, setStopDistance, spawnRing } from './www/js/mobs.js';

let failures = 0;
const check = (n,c,d='') => { console.log(`${c?'  PASS':'  FAIL'}  ${n}${d?'  '+d:''}`); if(!c) failures++; };
const near=(a,b,e=1e-4)=>Math.abs(a-b)<e;

const made = [];
THREE.TextureLoader.prototype.load = function (url, onLoad) {
  const t = new THREE.Texture(); made.push(t); setTimeout(()=>onLoad(t),0); return t;
};
const weaponsMeta = {};
for (const n of Object.values(BIOME_WEAPONS)) weaponsMeta[n] = {file:n+'.png',w:8,h:16,aspect:0.5};
globalThis.fetch = async () => ({ json: async () => ({ sprites:{}, weapons: weaponsMeta }) });
await preloadWeapons();

console.log('--- one weapon per biome ---');
check('all ten biomes have a weapon', Object.keys(BIOME_WEAPONS).length === 10);
check('every biome weapon is distinct',
      new Set(Object.values(BIOME_WEAPONS)).size === 10,
      Object.values(BIOME_WEAPONS).join(','));
check('forest gets an axe', weaponFor('forest') === 'axe');
check('desert gets the digging tool', weaponFor('desert') === 'pickaxe');
check('street gets a bat', weaponFor('street') === 'bat');
check('unknown biome still yields a weapon', !!weaponFor('lava'), weaponFor('lava'));
check('weapon textures use NearestFilter, no mipmaps',
      made.every(t => t.magFilter === THREE.NearestFilter &&
                      t.minFilter === THREE.NearestFilter && t.generateMipmaps === false));

console.log('--- viewmodel placement (right hand, drawn on top) ---');
const scene = new THREE.Scene();
const w = new Weapon(scene);
w.setBiome('forest');
check('mesh created', !!w.mesh);
check('drawn over the world', w.mesh.material.depthTest === false && w.mesh.renderOrder > 1000);
check('cutout, not blended', near(w.mesh.material.alphaTest, 0.5) && w.mesh.material.transparent === false);
check('own matrix, not auto-updated', w.mesh.matrixAutoUpdate === false);
{
  // Camera at origin looking down -Z: weapon must sit to the RIGHT and BELOW.
  const cam = new THREE.Object3D(); cam.updateMatrixWorld(true);
  w.update(0, cam.matrixWorld);
  const p = new THREE.Vector3().setFromMatrixPosition(w.mesh.matrix);
  check('sits on the right of view (+x)', p.x > 0.1, `x=${p.x.toFixed(3)}`);
  check('sits below the eyeline (-y)', p.y < 0, `y=${p.y.toFixed(3)}`);
  check('sits in front of the camera (-z)', p.z < 0, `z=${p.z.toFixed(3)}`);
  // The real invariant: wherever the camera goes, the weapon's position
  // expressed in CAMERA SPACE must be unchanged. Robust to tuning the offsets.
  const localOf = (camObj) => {
    const inv = new THREE.Matrix4().copy(camObj.matrixWorld).invert();
    return new THREE.Vector3().setFromMatrixPosition(w.mesh.matrix).applyMatrix4(inv);
  };
  const restLocal = localOf(cam);
  for (const [px,py,pz,ry] of [[5,1.6,-3,Math.PI/2],[-2,0.9,7,-1.1],[0,2.2,0,3.0]]) {
    cam.position.set(px,py,pz); cam.rotation.set(0,ry,0); cam.updateMatrixWorld(true);
    w.update(0, cam.matrixWorld);
    const l = localOf(cam);
    check(`camera at (${px},${py},${pz}) yaw ${ry.toFixed(1)}: same offset in camera space`,
          l.distanceTo(restLocal) < 1e-6,
          `local=(${l.x.toFixed(3)},${l.y.toFixed(3)},${l.z.toFixed(3)})`);
  }
  const world = new THREE.Vector3().setFromMatrixPosition(w.mesh.matrix);
  check('weapon moved in world space with the camera', !world.equals(p));
}

console.log('--- swing ---');
{
  const w2 = new Weapon(new THREE.Scene());
  w2.setBiome('rocky');
  check('at rest before swinging', w2.swingPhase(1000) === 0 && !w2.swinging(1000));
  check('swing starts', w2.swing(1000) === true);
  check('is swinging mid-arc', w2.swinging(1100) === true);
  const mid = w2.swingPhase(1160);
  check('phase advances through the arc', mid > 0.3 && mid < 0.7, `phase=${mid.toFixed(2)}`);
  check('cannot queue a second swing mid-arc', w2.swing(1100) === false);
  check('returns to rest after the arc', w2.swinging(1400) === false && w2.swingPhase(1400) === 0);
  check('can swing again once finished', w2.swing(1400) === true);
  check('swings are counted', w2.swings === 2, String(w2.swings));
  // the transform must actually differ mid-swing
  const cam = new THREE.Object3D(); cam.updateMatrixWorld(true);
  w2.swingStart = -1; w2.update(0, cam.matrixWorld);
  const rest = new THREE.Vector3().setFromMatrixPosition(w2.mesh.matrix);
  w2.swing(0); w2.update(160, cam.matrixWorld);
  const swung = new THREE.Vector3().setFromMatrixPosition(w2.mesh.matrix);
  check('the weapon visibly moves during the swing', rest.distanceTo(swung) > 0.05,
        `moved ${rest.distanceTo(swung).toFixed(3)} m`);
}

console.log('--- weapon changes with the world ---');
{
  const w3 = new Weapon(new THREE.Scene());
  w3.setBiome('forest'); const a = w3.name;
  w3.setBiome('snow');   const b = w3.name;
  check('a world swap changes the weapon', a !== b, `${a} -> ${b}`);
  const meshBefore = w3.mesh;
  w3.setBiome('snow');
  check('re-setting the same biome does not rebuild', w3.mesh === meshBefore);
}

console.log('--- mobs back off ---');
{
  check('default stop distance is well clear of the face', STOP_DISTANCE >= 1.8,
        `${STOP_DISTANCE} m`);
  const scene2 = { add(){}, remove(){} };
  const mobs = spawnRing(scene2, 0, 0, { count: 8, biome: 'forest' });
  let minD = Infinity;
  for (let i=0;i<1800;i++){ for(const m of mobs) m.step(1/60,0,0,i*16.7,mobs);
    for(const m of mobs) minD=Math.min(minD,Math.hypot(m.group.position.x,m.group.position.z)); }
  check('nothing gets closer than the stop distance', minD >= STOP_DISTANCE - 1e-6,
        `min=${minD.toFixed(3)} m`);
  setStopDistance(1.4);
  check('?stop= can retune it live', STOP_DISTANCE === 1.4);
  setStopDistance(99); check('absurd values rejected', STOP_DISTANCE === 1.4);
  setStopDistance(2.0);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all weapon contracts hold');
process.exit(failures ? 1 : 0);

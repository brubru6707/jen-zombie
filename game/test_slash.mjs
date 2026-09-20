// Thrown slashes and the damage they do, headless: no GPU, no canvas, no DOM.
// The projectile maths, the hit test and the mob's health are all plain
// arithmetic, so everything the player actually feels is checked here.
import * as THREE from './www/vendor/three.module.js';
import { SlashSwarm, SLASH, AIM, connects, verticalOverlap, pickTarget, aimPoint } from './www/js/slash.js';
import { Mob, spawnRing, STOP_DISTANCE, DEATH_MS, BASE_HP } from './www/js/mobs.js';

let failures = 0;
const check = (n, c, d = '') => { console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++; };
const scene = { add() {}, remove() {} };
const at = (x, z, kind = 'walker', hpBonus = 0) => {
  const m = new Mob(kind, x, z, 0.5, 'street', hpBonus);
  return m;
};
// A camera at the origin, eyes at 1.4 m, looking down -Z.
function camera(pos = [0, 1.4, 0], yaw = 0, pitch = 0) {
  const o = new THREE.Object3D();
  o.position.set(...pos);
  o.rotation.set(pitch, yaw, 0, 'YXZ');
  o.updateMatrixWorld(true);
  return o.matrixWorld;
}
const run = (sw, mobs, seconds, dt = 1 / 60) => {
  let hits = 0, kills = 0;
  for (let i = 0; i < Math.round(seconds / dt); i++) {
    for (const m of mobs) m.step(dt, 0, 0, i * dt * 1000, mobs);
    const r = sw.update(dt, mobs, 0, 0);
    hits += r.hits; kills += r.kills;
  }
  return { hits, kills };
};

console.log('--- the throw comes out of the camera ---');
{
  const sw = new SlashSwarm(scene);
  const s = sw.spawnFromCamera(camera());
  check('a slash is created', !!s && sw.live.length === 1 && sw.stats().thrown === 1);
  check('it starts in front of the eyes, not inside the player',
        s.z < -0.3 && Math.abs(s.y - 1.4) < 0.3, `pos=(${s.x.toFixed(2)},${s.y.toFixed(2)},${s.z.toFixed(2)})`);
  check('it travels forward (-Z) when looking forward', s.dz < -0.9, `dz=${s.dz.toFixed(3)}`);
  check('the direction is a unit vector', Math.abs(Math.hypot(s.dx, s.dy, s.dz) - 1) < 1e-6);
  const turned = new SlashSwarm(scene).spawnFromCamera(camera([0, 1.4, 0], Math.PI / 2));
  check('turn 90 deg and it flies the new way (-X)', turned.dx < -0.9, `dx=${turned.dx.toFixed(3)}`);
  const down = new SlashSwarm(scene).spawnFromCamera(camera([0, 1.4, 0], 0, -1.2));
  check('aiming at your feet is tamed, not thrown at the floor', down.dy > -0.36 && down.dy >= -0.35 - 1e-9,
        `dy=${down.dy.toFixed(3)}`);
  check('...and it still moves horizontally at full speed', Math.abs(Math.hypot(down.dx, down.dz) - Math.sqrt(1 - down.dy ** 2)) < 1e-6);
  const up = new SlashSwarm(scene).spawnFromCamera(camera([0, 1.4, 0], 0, 1.2));
  check('the same limit applies looking up', up.dy <= 0.35 + 1e-9, `dy=${up.dy.toFixed(3)}`);
  const bad = new SlashSwarm(scene);
  const m = new THREE.Matrix4(); m.elements[0] = NaN;
  const zero = new THREE.Matrix4(); zero.elements.fill(0);
  check('a degenerate camera matrix throws nothing and spawns nothing',
        bad.spawnFromCamera(m) === null && bad.spawnFromCamera(null) === null &&
        bad.spawnFromCamera(zero) === null && bad.live.length === 0,
        'NaN, null and the all-zero matrix before the first tracked frame');
  check('a zero or NaN direction is refused outright',
        bad.spawn({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: 0 }) === null &&
        bad.spawn({ x: 0, y: 1, z: 0 }, { x: NaN, y: 0, z: 1 }) === null &&
        bad.spawn({ x: NaN, y: 1, z: 0 }, { x: 0, y: 0, z: 1 }) === null && bad.live.length === 0);
}

console.log('--- a slash is thrown AT something ---');
{
  const origin = { x: 0, y: 1.4, z: 0 }, fwd = { x: 0, y: 0, z: -1 };
  const ahead = at(0.9, -3), far = at(0, -8.6), behind = at(0, 3), side = at(5, -1);
  check('a mob ahead but off-axis is picked', pickTarget([ahead], origin, fwd) === ahead);
  check('one behind you is not', pickTarget([behind], origin, fwd) === null);
  check('one out to the side is not', pickTarget([side], origin, fwd) === null);
  check('one beyond the aim range is not', pickTarget([far], origin, fwd) === null,
        `${AIM.maxDist} m limit`);
  const near = at(0.2, -2), further = at(0.2, -5);
  check('the nearest in the cone wins', pickTarget([further, near], origin, fwd) === near);
  check('a dying mob is never targeted', (near.dying = true, pickTarget([further, near], origin, fwd) === further));
  check('nothing in the cone means no lock', pickTarget([behind, side], origin, fwd) === null);
  check('an empty field is safe', pickTarget([], origin, fwd) === null && pickTarget(null, origin, fwd) === null);
  check('the aim point is chest height, not the feet',
        Math.abs(aimPoint(ahead).y - 1.7 * AIM.chest) < 1e-9, `${aimPoint(ahead).y.toFixed(2)} m`);

  const sw = new SlashSwarm(scene);
  const mob = at(1.4, -3);                       // ~25 deg off the view axis
  mob.speed = 0;
  const s1 = sw.spawnFromCamera(camera(), [mob]);
  check('the throw leaves along the line to the mob, not straight ahead',
        s1.dx > 0.3 && sw.stats().aimed === 1, `dir=(${s1.dx.toFixed(2)},${s1.dy.toFixed(2)},${s1.dz.toFixed(2)})`);
  check('and it connects', run(sw, [mob], 1.5).kills === 1);

  const sw2 = new SlashSwarm(scene);
  const chosen = at(-1.5, -3), nearer = at(0.2, -2);
  chosen.speed = nearer.speed = 0;
  sw2.spawnFromCamera(camera(), [chosen, nearer], chosen);
  const r2 = run(sw2, [chosen, nearer], 1.5);
  check('an explicit target beats the nearest one (the handheld cycles targets)',
        r2.kills === 1 && chosen.dying && !nearer.dying);

  const sw3 = new SlashSwarm(scene);
  const dead = at(0.5, -3); dead.dying = true;
  const s3 = sw3.spawnFromCamera(camera(), [dead], dead);
  check('aiming at a corpse falls back to straight ahead',
        sw3.stats().aimed === 0 && s3.dz < -0.9);
  const s4 = new SlashSwarm(scene).spawnFromCamera(camera(), null);
  check('no mob list at all still throws', !!s4 && s4.dz < -0.9);
}

console.log('--- flight and expiry ---');
{
  const sw = new SlashSwarm(scene, { range: 4 });
  const s = sw.spawn({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 });
  sw.update(0.5, [], 0, 0);
  check('it moves at the configured speed', Math.abs(s.z + SLASH.speed * 0.5) < 1e-6, `z=${s.z.toFixed(3)}`);
  check('nothing else drifts', s.x === 0 && s.y === 1);
  run(sw, [], 2);
  check('it expires at the end of its range and is removed',
        sw.live.length === 0 && sw.stats().expired === 1 && sw.stats().hits === 0);
  const nan = new SlashSwarm(scene);
  nan.spawn({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 });
  nan.update(NaN, [], 0, 0); nan.update(-1, [], 0, 0);
  check('a NaN or negative frame time cannot move it or crash',
        nan.live.length === 1 && Number.isFinite(nan.live[0].z) && nan.live[0].z === 0);
  const stuck = new SlashSwarm(scene, { range: 4, speed: 0 });   // cannot expire by distance
  stuck.spawn({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 });
  run(stuck, [], 6);
  check('a slash that never travels still leaves (age backstop)', stuck.live.length === 0);
}

console.log('--- what a slash connects with ---');
{
  const m = at(0, -3);
  check('dead centre connects', connects({ x: 0, y: 1.0, z: -3 }, m));
  check('just inside the radius connects', connects({ x: SLASH.radius - 0.02, y: 1.0, z: -3 }, m));
  check('just outside does not', !connects({ x: SLASH.radius + 0.05, y: 1.0, z: -3 }, m));
  check('over its head misses', !connects({ x: 0, y: 3.0, z: -3 }, m));
  check('under the floor misses', !connects({ x: 0, y: -0.8, z: -3 }, m));
  check('knee height still counts', connects({ x: 0, y: 0.3, z: -3 }, m));
  check('the vertical band covers a 1.7 m body', verticalOverlap(1.6, 0, 1.7) && !verticalOverlap(2.5, 0, 1.7));
  check('a NaN position connects with nothing', !connects({ x: NaN, y: 1, z: -3 }, m));
  check('nothing connects with a corpse', (m.dying = true, !connects({ x: 0, y: 1, z: -3 }, m)));
}

console.log('--- damage, death, and the kill count ---');
{
  const m = at(0, -3);
  check('a walker starts on one hit point', m.hp === 1 && m.maxHp === BASE_HP.walker);
  const r = m.hit(1);
  check('one slash kills a walker', r.hit && r.dead && m.dying && m.hp === 0);
  check('a dying mob absorbs nothing more (two slashes cannot both claim it)',
        m.hit(1).hit === false);
  const brute = at(0, -3, 'brute');
  check('a brute takes two', brute.hit(1).dead === false && brute.hit(1).dead === true);
  const tough = at(0, -3, 'brute', 2);
  check("the world's hp_bonus makes them tougher", tough.maxHp === BASE_HP.brute + 2);
  check('and it takes exactly that many throws',
        [1, 1, 1].every(d => tough.hit(d).dead === false) && tough.hit(1).dead === true);
  const z = at(0, -3);
  check('zero and negative damage cannot heal or kill', (z.hit(0), z.hit(-5), z.hp === 1 && !z.dying));
}

console.log('--- a throw that lands ---');
{
  const sw = new SlashSwarm(scene);
  const mob = at(0, -3);
  mob.speed = 0;
  sw.spawnFromCamera(camera());
  const r = run(sw, [mob], 1.5);
  check('the slash reaches a mob three metres ahead and kills it',
        r.hits === 1 && r.kills === 1 && mob.dying, `hits=${r.hits} kills=${r.kills}`);
  check('it is consumed by the hit, not left flying', sw.live.length === 0 && sw.stats().expired === 0);
  check('the swarm counts it', sw.stats().hits === 1 && sw.stats().kills === 1);
  check('the kill reports WHAT died, so it can be scored',
        (() => { const s2 = new SlashSwarm(scene); const m2 = at(0, -3, 'brute'); m2.speed = 0; m2.hp = 1;
                 s2.spawnFromCamera(camera(), [m2]);
                 let killed = [];
                 for (let i = 0; i < 90; i++) { m2.step(1/60, 0, 0, i * 16.7, [m2]);
                   const r = s2.update(1/60, [m2], 0, 0); if (r.killed.length) killed = r.killed; }
                 return killed.join() === 'brute'; })());
  run(sw, [mob], DEATH_MS / 1000 + 0.2);
  check('the body finishes its death and asks to be removed', mob.dead === true);
}

console.log('--- one slash, one mob ---');
{
  const sw = new SlashSwarm(scene);
  const a = at(0, -3), b = at(0.3, -3.2);
  a.speed = b.speed = 0;
  sw.spawnFromCamera(camera());
  const r = run(sw, [a, b], 1.5);
  check('two mobs shoulder to shoulder, one throw, one casualty',
        r.kills === 1 && (a.dying ? !b.dying : b.dying), `a=${a.dying} b=${b.dying}`);
}

console.log('--- a miss stays a miss ---');
{
  const sw = new SlashSwarm(scene);
  const off = at(4, -3);       // well to the side
  off.speed = 0;
  sw.spawnFromCamera(camera());
  const r = run(sw, [off], 2);
  check('a mob off to the side takes nothing', r.hits === 0 && off.hp === 1 && sw.stats().expired === 1);
}

console.log('--- volume, and the dead leaving the field ---');
{
  const sw = new SlashSwarm(scene, { maxLive: 3 });
  for (let i = 0; i < 10; i++) sw.spawn({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 });
  check('the live cap holds (a stuck loop cannot allocate forever)', sw.live.length === 3);
  const sw2 = new SlashSwarm(scene);
  const mobs = spawnRing(scene, 0, 0, { count: 5, biome: 'street', hpBonus: 0, radius: 3 });
  check('a spawned ring carries hit points', mobs.every(m => m.hp >= 1));
  for (const m of mobs) m.speed = 0;
  for (let i = 0; i < 5; i++) sw2.spawnFromCamera(camera([mobs[i].group.position.x * 0.0001, 1.4, 0],
    Math.atan2(-mobs[i].group.position.x, -mobs[i].group.position.z) + Math.PI));
  run(sw2, mobs, 2);
  check('throws aimed at each of five mobs never exceed five kills', sw2.stats().kills <= 5);
  check('every mob position is still finite after the exchange',
        mobs.every(m => Number.isFinite(m.group.position.x) && Number.isFinite(m.group.position.z)));
  const alive = mobs.filter(m => !m.dying);
  for (let i = 0; i < 400; i++) for (const m of alive) m.step(1 / 60, 0, 0, i * 16.7, mobs);
  const minD = Math.min(...alive.map(m => Math.hypot(m.group.position.x, m.group.position.z)));
  check('survivors still respect the stop distance', alive.length === 0 || minD >= STOP_DISTANCE - 1e-6,
        `min=${minD.toFixed(3)} m`);
}

console.log('--- disposal ---');
{
  const removed = [];
  const sc = { add() {}, remove(o) { removed.push(o); } };
  const sw = new SlashSwarm(sc);
  for (let i = 0; i < 3; i++) sw.spawn({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 });
  sw.dispose();
  check('dispose clears everything in flight', sw.live.length === 0);
  check('a fresh swarm still works afterwards', (() => { const s2 = new SlashSwarm(sc); return !!s2.spawn({ x: 0, y: 1, z: 0 }, { x: 0, y: 0, z: -1 }); })());
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all thrown-slash contracts hold');
process.exit(failures ? 1 : 0);

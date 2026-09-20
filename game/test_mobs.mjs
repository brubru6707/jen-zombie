// Headless check of the mob steering invariants, importing the SAME module the
// phone loads. Geometry construction needs no WebGL, so this runs in node.
//   node test_mobs.mjs
import { spawnRing, STOP_DISTANCE as ARM_LENGTH } from './www/js/mobs.js';

const scene = { add(){}, remove(){} };   // three.js Scene stand-in
let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? '  PASS' : '  FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
  if (!cond) failures++;
}

// Worst case for the bug that was found: many mobs converging on one point,
// so the separation push fights the stop radius.
for (const count of [6, 12, 20]) {
  const mobs = spawnRing(scene, 0, 0, { count, radius: 3.6, speedScale: 1.0, bruteBias: 0.2 });
  const px = 0, pz = 0;
  let minSeen = Infinity;
  let t = 0;
  for (let step = 0; step < 1800; step++) {      // 30 s at 60 fps
    t += 16.7;
    for (const m of mobs) m.step(1 / 60, px, pz, t, mobs);
    for (const m of mobs) {
      const d = Math.hypot(m.group.position.x - px, m.group.position.z - pz);
      if (d < minSeen) minSeen = d;
      if (!Number.isFinite(d)) { check(`finite position (n=${count})`, false); break; }
    }
  }
  check(`n=${count}: nothing breaches arm's length`,
        minSeen >= ARM_LENGTH - 1e-6,
        `min=${minSeen.toFixed(4)} m, limit=${ARM_LENGTH} m`);
  check(`n=${count}: all mobs converged (none stuck far out)`,
        mobs.every(m => m.distance < ARM_LENGTH + 1.4),
        `max=${Math.max(...mobs.map(m => m.distance)).toFixed(2)} m`);
  check(`n=${count}: feet stay on the floor plane`,
        mobs.every(m => Math.abs(m.group.position.y) < 1e-9));
}

// Degenerate case: player standing exactly on a mob.
{
  const mobs = spawnRing(scene, 0, 0, { count: 3, radius: 0.0, jitter: 0 });
  for (let i = 0; i < 120; i++) for (const m of mobs) m.step(1/60, 0, 0, i*16.7, mobs);
  const ds = mobs.map(m => Math.hypot(m.group.position.x, m.group.position.z));
  check('player inside a mob does not produce NaN',
        ds.every(d => Number.isFinite(d) && d >= ARM_LENGTH - 1e-6),
        `d=[${ds.map(d => d.toFixed(3)).join(', ')}]`);
}

// A moving player must still be respected.
{
  const mobs = spawnRing(scene, 0, 0, { count: 8, radius: 2.6 });
  let minSeen = Infinity, px = 0, pz = 0;
  for (let i = 0; i < 1800; i++) {
    px = Math.sin(i / 120) * 1.5; pz = Math.cos(i / 150) * 1.5;   // player walks
    for (const m of mobs) m.step(1/60, px, pz, i*16.7, mobs);
    for (const m of mobs) minSeen = Math.min(minSeen,
      Math.hypot(m.group.position.x - px, m.group.position.z - pz));
  }
  check('moving player: invariant still holds', minSeen >= ARM_LENGTH - 1e-6,
        `min=${minSeen.toFixed(4)} m`);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all invariants hold');
process.exit(failures ? 1 : 0);

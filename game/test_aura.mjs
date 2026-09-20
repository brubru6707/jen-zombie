// The green motes over the peaceful mobs: pooled, recycled, allocation-free,
// and they follow the right mobs. Headless -- no canvas, no GPU.
import * as THREE from './www/vendor/three.module.js';
import { Aura, AURA } from './www/js/aura.js';
import { Mob } from './www/js/mobs.js';

let failures = 0;
const check = (n, c, d = '') => { console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++; };
const scene = { add() {}, remove() {} };
const mob = (x, z, mood) => new Mob('walker', x, z, 0.4, 'meadow', 0, mood);
const near = (a, b, e = 1e-6) => Math.abs(a - b) < e;
/** Distance of mote i from a point, in XZ. */
const xzDist = (a, i, x, z) => Math.hypot(a.pos[i * 3] - x, a.pos[i * 3 + 2] - z);

console.log('--- who gets an aura ---');
{
  check('a calm mob does', Aura.wants(mob(0, -2, 'calm')));
  check('a hostile one does not', !Aura.wants(mob(0, -2, 'hostile')));
  const leaving = mob(0, -2, 'calm'); leaving.leave();
  check('one on its way out loses it', !Aura.wants(leaving));
  const dying = mob(0, -2, 'calm'); dying.hit(99);
  check('a dying one loses it', !Aura.wants(dying));
  check('rubbish is safe', !Aura.wants(null) && !Aura.wants({}));
}

console.log('--- the pool is fixed and the motes follow their mob ---');
{
  const a = new Aura(scene);
  const size = a.count;
  check('one pool, allocated once', size === AURA.perMob * AURA.maxMobs && a.pos.length === size * 3);
  const calm = mob(3, -4, 'calm'), hostile = mob(-3, -4, 'hostile');
  check('it reports how many it is decorating', a.update(1 / 60, [calm, hostile]) === 1);
  check('the pool did not grow', a.count === size && a.pos.length === size * 3);
  let onMob = 0;
  for (let i = 0; i < AURA.perMob; i++) if (xzDist(a, i, 3, -4) <= AURA.radius + 1e-6) onMob++;
  check('every mote of the first block hovers over the calm mob', onMob === AURA.perMob, `${onMob}/${AURA.perMob}`);
  const above = [];
  for (let i = 0; i < AURA.perMob; i++) above.push(a.pos[i * 3 + 1]);
  check('they sit above the floor, over the body', above.every(y => y > 0.2 && y < 3));
  let nearHostile = 0;
  for (let i = 0; i < a.count; i++) if (xzDist(a, i, -3, -4) < AURA.radius && a.pos[i * 3 + 1] > -900) nearHostile++;
  check('nothing hovers over the hostile mob', nearHostile === 0);
  let parked = 0;
  for (let i = AURA.perMob; i < a.count; i++) if (a.pos[i * 3 + 1] === AURA.parked) parked++;
  check('unused motes are parked out of sight', parked === a.count - AURA.perMob);
}

console.log('--- motion, recycling, and no NaN ---');
{
  const a = new Aura(scene);
  const calm = mob(0, -3, 'calm');
  a.update(1 / 60, [calm]);
  // Pin one mote to the start of a long life, or it may recycle mid-measurement
  // and the rise we are checking for is legitimately reset.
  a.age[0] = 0; a.lifeOf[0] = 4;
  a.update(1 / 60, [calm]);
  const y0 = a.pos[1];
  for (let i = 0; i < 20; i++) a.update(1 / 60, [calm]);
  check('they rise', a.pos[1] > y0, `${y0.toFixed(2)} -> ${a.pos[1].toFixed(2)}`);
  let maxY = 0, recycled = false;
  for (let i = 0; i < 600; i++) {
    a.update(1 / 60, [calm]);
    maxY = Math.max(maxY, a.pos[1]);
    if (a.age[0] < 1 / 30) recycled = true;
  }
  check('they are recycled rather than escaping upward', recycled && maxY < 3.5, `max y ${maxY.toFixed(2)}`);
  check('every coordinate stays finite over ten seconds',
        Array.from(a.pos).every(Number.isFinite) && Array.from(a.alpha).every(Number.isFinite));
  check('alpha stays within 0..1', Array.from(a.alpha).every(v => v >= 0 && v <= 1));
  check('a zero or NaN frame time is harmless',
        (a.update(0, [calm]), a.update(NaN, [calm]), Array.from(a.pos).every(Number.isFinite)));
  calm.group.position.set(5, 0, 5);
  a.update(1 / 60, [calm]);
  check('they move with the mob', xzDist(a, 0, 5, 5) <= AURA.radius + 1e-6);
}

console.log('--- the room turning over ---');
{
  const a = new Aura(scene);
  const calm = mob(0, -3, 'calm');
  a.update(1 / 60, [calm]);
  check('decorating one', a.stats().calm === 1);
  calm.leave();
  a.update(1 / 60, [calm]);
  check('the aura goes out the moment it starts leaving', a.stats().calm === 0);
  let lit = 0;
  for (let i = 0; i < a.count; i++) if (a.pos[i * 3 + 1] !== AURA.parked) lit++;
  check('and every mote is parked', lit === 0);
  const many = [];
  for (let i = 0; i < AURA.maxMobs + 5; i++) many.push(mob(i, -3, 'calm'));
  a.update(1 / 60, many);
  check('more calm mobs than the pool holds is capped, not overflowed',
        a.stats().calm === AURA.maxMobs && a.pos.length === a.count * 3);
  check('an empty room is fine', a.update(1 / 60, []) === 0);
}

console.log('--- disposal ---');
{
  const removed = [];
  const a = new Aura({ add() {}, remove(o) { removed.push(o); } });
  a.dispose();
  check('dispose is clean and repeatable', (a.dispose(), a.points === null));
  check('updating after disposal does not throw', a.update(1 / 60, [mob(0, -2, 'calm')]) === 1);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all aura contracts hold');
process.exit(failures ? 1 : 0);

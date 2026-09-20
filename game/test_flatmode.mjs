// 2D mode: the stick has to be the legs, and it has to stay honest when the
// controller is missing, lying, or pushed to the rails.
import { FlatRig, FLAT, axis } from './www/js/flatmode.js';

let failures = 0;
const check = (n, c, d = '') => { console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++; };
const near = (a, b, e = 1e-3) => Math.abs(a - b) <= e;

// A stand-in for THREE's camera: only what the rig actually touches.
const fakeCam = () => ({
  fov: 0, near: 0, far: 0,
  position: { x: 0, y: 0, z: 0, set(x, y, z) { this.x = x; this.y = y; this.z = z; } },
  rotation: { x: 0, y: 0, z: 0, order: '', set(x, y, z, o) { this.x = x; this.y = y; this.z = z; this.order = o; } },
  updates: 0,
  updateProjectionMatrix() { this.projUpdates = (this.projUpdates || 0) + 1; },
  updateMatrixWorld() { this.updates++; },
});
const pad = (x, y, connected = true) => ({ x, y, connected });
const run = (rig, p, secs, step = 1 / 60) => {
  for (let i = 0; i < Math.round(secs / step); i++) rig.update(step, p);
  return rig;
};

console.log('--- the deadzone does not eat the first usable push ---');
{
  check('inside the deadzone is a dead stick', axis(0) === 0 && axis(FLAT.deadzone) === 0 && axis(-0.05) === 0);
  check('just outside it starts from zero, not from a step', near(axis(FLAT.deadzone + 1e-6), 0, 1e-4));
  check('the rails still reach exactly 1', axis(1) === 1 && axis(-1) === -1);
  check('beyond the rails is clamped, not amplified', axis(3) === 1 && axis(-3) === -1);
  check('rubbish is zero, never NaN', axis(undefined) === 0 && axis('x') === 0 && axis(null) === 0);
}

console.log('--- pushing the stick walks you the way you are facing ---');
{
  const rig = new FlatRig(fakeCam());
  run(rig, pad(0, -1), 1.0);
  check('a second at full forward covers roughly the walking speed',
        rig.z < -1.2 && rig.z > -1.9, `z=${rig.z.toFixed(2)} (speed ${FLAT.speed})`);
  check('and does not drift sideways', near(rig.x, 0, 1e-6), `x=${rig.x}`);
  check('the odometer agrees with the distance', near(rig.status().walked, Math.abs(rig.z), 0.05));

  const back = new FlatRig(fakeCam());
  run(back, pad(0, 1), 1.0);
  check('pulling back walks backwards', back.z > 1.2);

  const turned = new FlatRig(fakeCam()).reset(0, 0, Math.PI / 2);
  run(turned, pad(0, -1), 1.0);
  check('facing a quarter turn, forward is now along x', turned.x < -1.2 && near(turned.z, 0, 0.05),
        `x=${turned.x.toFixed(2)} z=${turned.z.toFixed(2)}`);
}

console.log('--- the same axis that steers must not also strafe ---');
{
  const rig = new FlatRig(fakeCam());
  run(rig, pad(1, 0), 0.5);
  check('holding left or right turns you', Math.abs(rig.yaw) > 0.5, `yaw=${rig.yaw.toFixed(2)}`);
  check('and moves you nowhere at all', near(rig.x, 0, 1e-9) && near(rig.z, 0, 1e-9));
  check('the turn is counted for telemetry', rig.status().turnedDeg > 25);
}

console.log('--- a shove is not a teleport ---');
{
  const rig = new FlatRig(fakeCam());
  rig.update(1 / 60, pad(0, -1));
  const first = Math.hypot(rig.x, rig.z);
  check('the first frame at full stick moves millimetres, not metres',
        first < FLAT.speed / 60, `${(first * 1000).toFixed(1)} mm`);
  run(rig, pad(0, -1), 1.5);
  check('but it reaches full speed shortly after', near(rig.status().speed, FLAT.speed, 0.15),
        `${rig.status().speed} m/s`);
}

console.log('--- no controller means no input, never a jump ---');
{
  const rig = new FlatRig(fakeCam());
  run(rig, pad(1, -1, false), 2.0);
  check('a disconnected pad moves and turns nothing',
        near(rig.x, 0, 1e-9) && near(rig.z, 0, 1e-9) && near(rig.yaw, 0, 1e-9));
  rig.update(0.016, null);
  rig.update(0.016, undefined);
  rig.update(0.016, {});
  check('a missing or empty pad is survived', near(rig.x, 0, 1e-9));
  rig.update(NaN, pad(0, -1));
  rig.update(-5, pad(0, -1));
  rig.update(999, pad(0, -1));
  check('a rubbish or enormous dt cannot fling you across the room',
        Number.isFinite(rig.x) && Number.isFinite(rig.z) && Math.hypot(rig.x, rig.z) < 1,
        `${Math.hypot(rig.x, rig.z).toFixed(3)} m`);
}

console.log('--- the leash keeps the backdrop believable ---');
{
  const rig = new FlatRig(fakeCam());
  run(rig, pad(0, -1), 60);
  check('you cannot walk past the leash', Math.hypot(rig.x, rig.z) <= FLAT.leash + 1e-6,
        `${Math.hypot(rig.x, rig.z).toFixed(2)} m of ${FLAT.leash}`);
  check('and you are not stopped dead at it either', rig.status().speed > 0);
  run(rig, pad(0, 1), 3);
  check('walking back in works', Math.hypot(rig.x, rig.z) < FLAT.leash);
}

console.log('--- the camera is what everything downstream reads ---');
{
  const cam = fakeCam();
  const rig = new FlatRig(cam);
  check('eye height is set at standing height', cam.position.y === FLAT.eyeY);
  check('the projection is configured once, not every frame', cam.projUpdates === 1);
  run(rig, pad(0.4, -1), 0.5);
  check('the camera follows the rig', near(cam.position.x, rig.x) && near(cam.position.z, rig.z));
  check('yaw is applied in YXZ so there is no roll',
        cam.rotation.order === 'YXZ' && cam.rotation.x === 0 && cam.rotation.z === 0 &&
        near(cam.rotation.y, rig.yaw));
  check('the world matrix is refreshed every frame', cam.updates > 25);

  const headless = new FlatRig(null);
  headless.update(0.1, pad(0, -1));
  headless.look(50);
  check('a rig with no camera still integrates rather than throwing', headless.z !== 0);
}

console.log('--- dragging the screen turns you, for when the pad is absent ---');
{
  const rig = new FlatRig(fakeCam());
  const before = rig.yaw;
  rig.look(100);
  check('a drag right turns you', rig.yaw !== before);
  rig.look(-100);
  check('and dragging back returns you', near(rig.yaw, before, 1e-9));
  rig.look(NaN); rig.look('x');
  check('a rubbish drag is ignored', near(rig.yaw, before, 1e-9));
}

console.log('--- reset puts you back for a fresh run ---');
{
  const rig = new FlatRig(fakeCam());
  run(rig, pad(0.5, -1), 2);
  rig.reset(0, 0, 0);
  check('position, facing and momentum all clear',
        rig.x === 0 && rig.z === 0 && rig.yaw === 0 && rig.status().speed === 0);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all 2D movement contracts hold');
process.exit(failures ? 1 : 0);

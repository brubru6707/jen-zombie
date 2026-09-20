// The game without a headset: the same scene, the same mobs, the same
// dialogue, driven entirely from the handheld.
//
// Why this exists: in AR the player WALKS, so the stick was deliberately not
// movement -- it aimed. On a flat screen nobody walks, so the stick has to be
// the legs. That is the only real difference between the two modes; every
// other system below this (mobs, slashes, voices, the mic, the LEDs, the
// world cycle) is untouched and does not know which mode it is running in.
//
// One stick, two jobs, chosen to suit a zombie standing behind you:
//   Y  walk forward and back along the way you are facing
//   X  TURN, not strafe -- strafing leaves you unable to face anything, and
//      facing is what the throw needs.
//
// The camera it drives is an ordinary PerspectiveCamera, so `matrixWorld` is
// the same handle the weapon, the slashes and the speech bubble already take
// from the XR camera. Nothing downstream branches on the mode.

export const FLAT = {
  eyeY: 1.6,           // standing eye height, matching a typical local-floor pose
  speed: 1.8,          // m/s at full deflection
  turnRate: 2.1,       // rad/s at full deflection (~120 deg/s)
  deadzone: 0.12,      // the bridge already applies one; this is belt and braces
  lookPerPx: 0.0042,   // rad per pixel dragged, for the no-controller fallback
  accel: 9.0,          // m/s^2 towards the stick, so a shove is not instant
  fov: 72,
  near: 0.05,
  far: 60,
  // How far the player may wander from where they started. The mob ring is
  // re-placed around them, but letting them walk to the horizon makes the
  // backdrop's edge visible and the world feel like a plate.
  leash: 14,
};

const clamp = (v, lo, hi) => (v < lo ? lo : (v > hi ? hi : v));
/** Stick axis with a deadzone, rescaled so the first usable value is still 0. */
export function axis(v, dz = FLAT.deadzone) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 0;
  const a = Math.abs(n);
  if (a <= dz) return 0;
  return Math.sign(n) * clamp((a - dz) / (1 - dz), 0, 1);
}

/**
 * Position, facing and the camera that follows them. Integrated, not snapped,
 * so a stick held hard does not teleport the view.
 */
export class FlatRig {
  constructor(camera, opts = {}) {
    this.camera = camera || null;
    this.cfg = Object.assign({}, FLAT, opts);
    this.x = 0; this.z = 0; this.yaw = 0;
    this.vx = 0; this.vz = 0;
    this.moved = 0;          // metres walked, for telemetry
    this.turned = 0;         // radians turned
    if (this.camera) {
      this.camera.fov = this.cfg.fov;
      this.camera.near = this.cfg.near;
      this.camera.far = this.cfg.far;
      if (this.camera.updateProjectionMatrix) this.camera.updateProjectionMatrix();
    }
    this.apply();
  }

  reset(x = 0, z = 0, yaw = 0) {
    this.x = x; this.z = z; this.yaw = yaw;
    this.vx = 0; this.vz = 0;
    this.apply();
    return this;
  }

  /** A drag on the screen turns you, for when there is no controller. */
  look(dxPx) {
    const d = Number(dxPx);
    if (!Number.isFinite(d)) return this.yaw;
    this.yaw -= d * this.cfg.lookPerPx;
    this.turned += Math.abs(d * this.cfg.lookPerPx);
    this.apply();
    return this.yaw;
  }

  /**
   * One frame of movement. `pad` is the controller's state; a disconnected or
   * missing one simply means no input, never a jump.
   */
  update(dt, pad) {
    const step = clamp(Number(dt) || 0, 0, 0.1);
    const live = !!(pad && pad.connected);
    const fwd = live ? -axis(pad.y) : 0;    // stick forward reads negative
    const turn = live ? axis(pad.x) : 0;

    this.yaw -= turn * this.cfg.turnRate * step;
    this.turned += Math.abs(turn * this.cfg.turnRate * step);

    // Target velocity along the way you are facing, approached at a finite
    // rate so the picture does not snap on the first frame of a shove.
    const tx = Math.sin(this.yaw) * -fwd * this.cfg.speed;
    const tz = Math.cos(this.yaw) * -fwd * this.cfg.speed;
    const k = clamp(this.cfg.accel * step, 0, 1);
    this.vx += (tx - this.vx) * k;
    this.vz += (tz - this.vz) * k;

    const nx = this.x + this.vx * step;
    const nz = this.z + this.vz * step;
    const r = Math.hypot(nx, nz);
    if (r > this.cfg.leash) {
      // Slide along the leash rather than stopping dead at it.
      const s = this.cfg.leash / r;
      this.x = nx * s; this.z = nz * s;
      this.vx *= 0.5; this.vz *= 0.5;
    } else {
      this.moved += Math.hypot(nx - this.x, nz - this.z);
      this.x = nx; this.z = nz;
    }
    this.apply();
    return this;
  }

  /** Push the state onto the camera. Safe with no camera (tests). */
  apply() {
    const c = this.camera;
    if (!c) return;
    c.position.set(this.x, this.cfg.eyeY, this.z);
    if (c.rotation && c.rotation.set) c.rotation.set(0, this.yaw, 0, 'YXZ');
    if (c.updateMatrixWorld) c.updateMatrixWorld(true);
  }

  status() {
    return {
      x: +this.x.toFixed(2), z: +this.z.toFixed(2),
      yawDeg: Math.round((this.yaw * 180 / Math.PI) % 360),
      speed: +Math.hypot(this.vx, this.vz).toFixed(2),
      walked: +this.moved.toFixed(1),
      turnedDeg: Math.round(this.turned * 180 / Math.PI),
    };
  }
}

/**
 * Something to stand on. In AR the room is the backdrop; on a flat screen an
 * empty scene renders as sprites floating in black, which reads as broken
 * rather than as a game. A ground plane, a grid for the motion to register
 * against, and fog so the leash edge is never a visible seam.
 */
export function makeBackdrop(THREE, scene, opts = {}) {
  const size = opts.size || 120;
  const group = new THREE.Group();
  group.name = 'flat-backdrop';
  group.renderOrder = -1;

  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(size, size),
    new THREE.MeshBasicMaterial({ color: 0x11151b })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.01;                 // under the grid, never z-fighting
  group.add(ground);

  const grid = new THREE.GridHelper(size, size / 2, 0x2a3442, 0x1d2530);
  group.add(grid);
  scene.add(group);

  const fog = new THREE.Fog(0x0b0d10, 6, 34);
  scene.fog = fog;

  return {
    group, grid, ground, fog,
    /** Tint the floor to the world's accent so a swap reads in 2D too. */
    setAccent(hex) {
      try {
        const c = new THREE.Color(hex || '#8a8f98');
        grid.material.color = c.clone().multiplyScalar(0.34);
        ground.material.color = c.clone().multiplyScalar(0.10);
      } catch (e) { /* a bad accent must never stop the frame */ }
    },
    dispose() {
      try { scene.remove(group); scene.fog = null; } catch (e) {}
    },
  };
}

/**
 * A renderer for the flat mode. Deliberately NOT the AR one: that enables XR,
 * asks for a framebuffer scale and leaves the canvas transparent for
 * passthrough, all of which are wrong here.
 */
export function makeFlatRenderer(THREE, { antialias = true } = {}) {
  const canvas = document.createElement('canvas');
  canvas.id = 'flatcanvas';
  document.body.appendChild(canvas);
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: false, antialias });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(window.innerWidth, window.innerHeight, false);
  renderer.setClearColor(0x0b0d10, 1);
  const resize = () => {
    renderer.setSize(window.innerWidth, window.innerHeight, false);
  };
  addEventListener('resize', resize);
  renderer._jzResize = resize;
  return renderer;
}

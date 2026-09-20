// Shared WebXR plumbing for the zombie-ar stages.
// Console prefix is [AR] everywhere so it filters cleanly in chrome://inspect.
import * as THREE from '../vendor/three.module.js';

export const TAG = '[AR]';
export const log  = (...a) => console.log(TAG, ...a);
export const warn = (...a) => console.warn(TAG, ...a);

// Stage 0 measured these on the target device (SM-S156V, Chrome 153,
// ARCore 1.56). Kept here so later stages do not re-derive them.
export const STAGE0 = {
  grantedFeatures: ['viewer','camera-access','hit-test','local-floor','anchors','local','light-estimation','dom-overlay'],
  cameraImage: { width: 1920, height: 886 },   // 2.17:1 crop, NOT the on-screen framing
  framesBeforeCameraReady: 40,                 // XRView.camera was undefined until ~36-40
};

/* ---------------------------------------------------------------- HUD ---- */

export class Hud {
  constructor(rootId) {
    this.root = document.getElementById(rootId);
    this.rows = new Map();
    this.msgEl = null;
  }
  set(key, value, cls) {
    let el = this.rows.get(key);
    if (!el) {
      el = document.createElement('div');
      el.className = 'hrow';
      el.innerHTML = `<span class="hk"></span><span class="hv"></span>`;
      el.querySelector('.hk').textContent = key;
      this.root.appendChild(el);
      this.rows.set(key, el);
    }
    const v = el.querySelector('.hv');
    v.textContent = value;
    v.className = 'hv' + (cls ? ' ' + cls : '');
  }
  message(text, cls) {
    if (!this.msgEl) {
      this.msgEl = document.createElement('div');
      this.msgEl.className = 'hmsg';
      this.root.appendChild(this.msgEl);
    }
    this.msgEl.textContent = text || '';
    this.msgEl.className = 'hmsg' + (cls ? ' ' + cls : '');
  }
}

/* ------------------------------------------------------------ session ---- */

// Stage 0 proved all of these are granted, but they stay OPTIONAL except
// hit-test: a session that starts degraded beats a session that will not start.
export const OPTIONAL_FEATURES = ['anchors','dom-overlay','camera-access','local-floor','light-estimation'];

export async function startAR({ overlayRoot, requiredFeatures = ['hit-test'] }) {
  if (!navigator.xr) throw new Error('navigator.xr missing');
  const ok = await navigator.xr.isSessionSupported('immersive-ar');
  if (!ok) throw new Error('immersive-ar not supported');
  const init = {
    requiredFeatures,
    optionalFeatures: OPTIONAL_FEATURES.slice(),
  };
  if (overlayRoot) init.domOverlay = { root: overlayRoot };
  const session = await navigator.xr.requestSession('immersive-ar', init);
  log('session started; enabledFeatures =',
      session.enabledFeatures ? Array.from(session.enabledFeatures) : '(not reported)');
  return session;
}

/**
 * The renderer, tuned for a phone that is already running ARCore, a camera, a
 * microphone and a WiFi link on the same four cores.
 *
 *   antialias  OFF by default. MSAA on a 1080x2340 panel is the single most
 *              expensive thing we were asking of this GPU, and the art is
 *              deliberately hard-edged pixel sprites that gain nothing from it.
 *   fbScale    the XR framebuffer is allocated at this fraction of the
 *              headset's recommended size. 0.8 is 36% fewer pixels per frame
 *              and is not visible on sprite art; it MUST be set before
 *              setSession, which is why it lives here.
 *
 * Both are overridable (?aa=1, ?fb=1.0) so the trade can be re-judged on the
 * floor without a deploy.
 */
export function makeRenderer(session, { antialias = false, fbScale = 0.8 } = {}) {
  const canvas = document.createElement('canvas');
  document.body.appendChild(canvas);
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias });
  renderer.setPixelRatio(1);          // the XR layer decides the real size
  renderer.setSize(window.innerWidth, window.innerHeight, false);
  renderer.xr.enabled = true;
  try { renderer.xr.setFramebufferScaleFactor(fbScale); }
  catch (e) { warn('framebuffer scale not accepted:', e); }
  // Finding 1: floor height comes from the reference space, not from hit-test.
  renderer.xr.setReferenceSpaceType('local-floor');
  return renderer;
}

/**
 * Ask for a slower display rate. A 90 Hz panel gives 11 ms per frame; 60 Hz
 * gives 16.6 ms for the same work, which on this phone is the difference
 * between holding a rate and missing frames in bursts -- and it runs cooler,
 * which matters because the stalls get worse the longer a session runs.
 * Returns the rate actually asked for, or null. Never throws.
 */
export function requestFrameRate(session, target = 60) {
  try {
    const rates = session.supportedFrameRates;
    if (!rates || !rates.length || !session.updateTargetFrameRate) return null;
    let best = null;
    for (const r of rates) if (r <= target + 0.5 && (best === null || r > best)) best = r;
    if (best === null) return null;
    session.updateTargetFrameRate(best);
    log('target frame rate ->', best, 'Hz of', Array.from(rates).join('/'));
    return best;
  } catch (e) { warn('frame rate request refused (ignored):', e); return null; }
}

export function basicScene() {
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(70, window.innerWidth / window.innerHeight, 0.01, 40);
  // Light estimation is granted but not used yet; fixed lights keep stage
  // geometry legible in any room.
  scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.4));
  const dir = new THREE.DirectionalLight(0xffffff, 1.0);
  dir.position.set(1, 4, 2);
  scene.add(dir);
  return { scene, camera };
}

/* ----------------------------------------------------------- hit test ---- */

export async function makeHitTestSource(session) {
  try {
    const viewerSpace = await session.requestReferenceSpace('viewer');
    const source = await session.requestHitTestSource({ space: viewerSpace });
    log('hit-test source ready');
    return source;
  } catch (e) {
    warn('hit-test source failed:', e);
    return null;
  }
}

/* ------------------------------------------------- camera frame access ---- */

/**
 * Pull a camera image texture out of an XRFrame.
 *
 * FINDING 2 (Stage 0): XRView.camera is undefined for roughly the first 36-40
 * frames of a session. Callers must tolerate a miss and try again on a later
 * frame rather than pushing null into the pipeline.
 *
 * FINDING 3 (Stage 0): the image is 1920x886, a 2.17:1 crop. That is NOT the
 * framing the player sees on screen. Anything that captions this frame is
 * describing a wider, shorter view than the user believes they pointed at.
 */
export class CameraFrameSource {
  constructor(session, gl) {
    this.binding = null;
    this.misses = 0;
    this.lastSize = null;
    this.lastMiss = null;     // why the last tryGet returned null (diagnostics)
    this.hits = 0;
    try {
      this.binding = new XRWebGLBinding(session, gl);
    } catch (e) {
      warn('XRWebGLBinding unavailable, camera frames disabled:', e);
      this.lastMiss = 'no XRWebGLBinding: ' + (e && e.message || e);
    }
  }
  /** Returns {texture, width, height} or null. Never throws. */
  tryGet(frame, refSpace) {
    if (!this.binding) return null;
    try {
      const pose = frame.getViewerPose(refSpace);
      if (!pose) { this.misses++; this.lastMiss = 'no viewer pose'; return null; }
      this.lastMiss = 'no view.camera (' + pose.views.length + ' views)';
      for (const view of pose.views) {
        const cam = view.camera;
        if (!cam) continue;
        const texture = this.binding.getCameraImage(cam);
        if (!texture) continue;
        this.lastSize = { width: cam.width, height: cam.height };
        this.hits++; this.lastMiss = null;
        return { texture, width: cam.width, height: cam.height };
      }
      this.misses++;      // expected on a cold start; not an error
      return null;
    } catch (e) {
      this.misses++; this.lastMiss = 'threw: ' + (e && e.message || e);
      warn('camera frame read failed (will retry):', e);
      return null;
    }
  }
}

/* -------------------------------------------------------------- misc ----- */

export class Fps {
  constructor() { this.last = 0; this.acc = 0; this.n = 0; this.value = 0; }
  tick(t) {
    if (this.last) { this.acc += t - this.last; this.n++; }
    this.last = t;
    if (this.acc >= 500) { this.value = Math.round(1000 / (this.acc / this.n)); this.acc = 0; this.n = 0; }
    return this.value;
  }
}

/** Fire-and-forget POST. Network failure must never touch the frame loop. */
export function postResult(name, payload) {
  try {
    fetch('/result/' + name, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    }).then(r => log('posted', name, r.status)).catch(e => warn('post failed (ignored):', e));
  } catch (e) {
    warn('post threw (ignored):', e);
  }
}

/**
 * Live telemetry to the dashboard. Throttled, fire-and-forget, and it drops
 * frames rather than queueing: the phone must never wait on the network, and a
 * dead dashboard must never cost the headset a frame.
 */
export class Telemetry {
  constructor({ url = '/telemetry', hz = 5, onCommands = null } = {}) {
    this.url = url;
    this.minGap = 1000 / hz;
    this.last = 0;
    this.inFlight = false;
    this.sent = 0;
    this.failed = 0;
    // The server answers a telemetry POST with whatever commands the dashboard
    // has queued (trim, calibrate, preset). That is the control channel back to
    // the headset: no second connection, and it costs nothing while idle.
    this.onCommands = onCommands;
    this.commands = 0;
  }
  /** True when the next push would actually be sent. Lets the caller skip
   *  building a telemetry object 90 times a second to send 5 of them. */
  due(t) { return !this.inFlight && (t - this.last) >= this.minGap; }

  push(t, obj) {
    if (this.inFlight || (t - this.last) < this.minGap) return;
    this.last = t;
    this.inFlight = true;
    try {
      fetch(this.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(obj),
        cache: 'no-store',
      }).then(r => {
        this.sent++;
        if (!this.onCommands || !r || !r.ok) return null;
        return r.json().then(j => {
          const cmds = j && Array.isArray(j.commands) ? j.commands : null;
          if (cmds && cmds.length) { this.commands += cmds.length; this.onCommands(cmds); }
        }).catch(() => {});            // a body we cannot parse is not a failure
      })
        .catch(() => { this.failed++; })
        .finally(() => { this.inFlight = false; });
    } catch (e) {
      this.inFlight = false;
      this.failed++;
    }
  }
}

export { THREE };

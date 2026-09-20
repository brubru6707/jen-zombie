// Where the next world comes from: a camera frame POSTed to /world at the START
// of the countdown, or the canned GET when that is not possible.
//
//   request()  is what WorldCycle calls as fetchWorld; it returns a promise and
//              parks the job until a frame can be grabbed.
//   tick()     runs every frame with a grab function. The camera texture only
//              exists inside the XR frame callback, and XRView.camera is
//              undefined for the first ~40 frames of a session, so "not ready
//              yet" is normal: keep trying until cameraWaitMs, then GET.
//
// Failure behaviour, all of it silent for the player:
//   grab throws / encodes to nothing  -> GET (canned), reason recorded
//   POST rejected or non-2xx          -> GET (canned)
//   GET fails too                     -> the promise rejects; WorldCycle keeps
//                                        the current world and retries
// Every world carries `source` (upstream | canned-no-frame | canned-fallback |
// canned) and `via` (frame | get) so the dashboard can say what it is showing.
//
// DOM/WebGL free: the grab function is injected, so this runs under node.

export class WorldSource {
  constructor({ url = '/world', timeoutMs = 40000, cameraWaitMs = 6000,
                fetchImpl = (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : null),
                now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
                log = () => {}, warn = () => {} } = {}) {
    this.url = url; this.timeoutMs = timeoutMs; this.cameraWaitMs = cameraWaitMs;
    this.fetch = fetchImpl; this.now = now; this.log = log; this.warn = warn;
    this.pending = null;
    this.posts = 0; this.postFails = 0; this.gets = 0; this.getFails = 0;
    this.notReady = 0;                // frames where the camera was not available
    this.lastFetchMs = 0; this.lastSource = null; this.lastReason = null;
    this.lastCaptureMs = 0; this.maxCaptureMs = 0; this.lastBytes = 0;
  }

  /** WorldCycle.fetchWorld. Resolves to the world object. */
  request(params = {}) {
    return new Promise((resolve, reject) => {
      // A newer request supersedes an unstarted one (a skip can do this).
      if (this.pending && !this.pending.started) {
        this.pending.reject(new Error('superseded'));
      }
      this.pending = { params, resolve, reject, t0: this.now(), started: false, waited: 0 };
    });
  }

  _query(p) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(p.params || {})) if (v !== '' && v != null) q.set(k, String(v));
    const s = q.toString();
    return this.url + (s ? '?' + s : '');
  }

  /**
   * Once per frame. grabFn() returns null when no camera image is available
   * this frame, else { blob: Promise<Blob|null>, ms, bytes? }.
   */
  tick(now, grabFn) {
    const p = this.pending;
    if (!p || p.started) return;
    let g = null;
    try { g = grabFn ? grabFn() : null; }
    catch (e) { p.started = true; this._get(p, 'capture: ' + (e && e.message || e)); return; }
    if (g) {
      p.started = true;
      this.lastCaptureMs = g.ms || 0;
      if (this.lastCaptureMs > this.maxCaptureMs) this.maxCaptureMs = this.lastCaptureMs;
      Promise.resolve(g.blob).then(blob => {
        if (!blob || !blob.size) return this._get(p, 'empty frame');
        this.lastBytes = blob.size;
        return this._post(p, blob);
      }).catch(e => this._get(p, 'encode: ' + (e && e.message || e)));
      return;
    }
    p.waited++; this.notReady++;
    if (now - p.t0 > this.cameraWaitMs) {
      p.started = true;
      this._get(p, 'camera not ready after ' + Math.round(now - p.t0) + ' ms');
    }
  }

  _abortable() {
    const ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    const timer = setTimeout(() => { try { ctl && ctl.abort(); } catch (e) {} }, this.timeoutMs);
    return { signal: ctl ? ctl.signal : undefined, done: () => clearTimeout(timer) };
  }

  _post(p, blob) {
    if (!this.fetch) return this._get(p, 'no fetch');
    const t0 = this.now(); const ab = this._abortable();
    this.posts++;
    let req;
    try {
      req = this.fetch(this._query(p), { method: 'POST', headers: { 'Content-Type': 'image/jpeg' },
                                         body: blob, cache: 'no-store', signal: ab.signal });
    } catch (e) { ab.done(); this.postFails++; return this._get(p, 'post threw: ' + e); }
    return Promise.resolve(req)
      .then(r => (r && r.ok) ? r.json() : Promise.reject(new Error('HTTP ' + (r && r.status))))
      .then(w => { ab.done(); this._resolve(p, w, 'frame', Math.round(this.now() - t0)); })
      .catch(e => { ab.done(); this.postFails++; this.warn('world POST failed, using canned:', e); this._get(p, 'post: ' + (e && e.message || e)); });
  }

  _get(p, reason) {
    p.reason = reason;
    if (!this.fetch) { this._finish(p); p.reject(new Error('no fetch')); return; }
    const t0 = this.now(); const ab = this._abortable();
    this.gets++;
    let req;
    try { req = this.fetch(this._query(p), { cache: 'no-store', signal: ab.signal }); }
    catch (e) { ab.done(); this.getFails++; this._finish(p); p.reject(e); return; }
    return Promise.resolve(req)
      .then(r => (r && r.ok) ? r.json() : Promise.reject(new Error('HTTP ' + (r && r.status))))
      .then(w => { ab.done(); this._resolve(p, w, 'get', Math.round(this.now() - t0)); })
      .catch(e => { ab.done(); this.getFails++; this._finish(p); p.reject(e); });
  }

  _finish(p) { if (this.pending === p) this.pending = null; }

  _resolve(p, w, via, ms) {
    this._finish(p);
    if (!w || typeof w !== 'object') { p.reject(new Error('bad world')); return; }
    w.via = via;
    if (!w.source) w.source = via === 'frame' ? 'upstream' : 'canned';
    if (p.reason) w.reason = p.reason;
    this.lastFetchMs = ms; this.lastSource = w.source; this.lastReason = p.reason || null;
    this.log('world via', via, '->', w.source, w.biome, w.name, `(${ms} ms${p.reason ? ', ' + p.reason : ''})`);
    p.resolve(w);
  }

  stats() {
    return { posts: this.posts, postFails: this.postFails, gets: this.gets, getFails: this.getFails,
             notReadyFrames: this.notReady, pending: !!this.pending,
             lastFetchMs: this.lastFetchMs, lastSource: this.lastSource, lastReason: this.lastReason,
             captureMs: +this.lastCaptureMs.toFixed(1), maxCaptureMs: +this.maxCaptureMs.toFixed(1),
             lastBytes: this.lastBytes };
  }
}

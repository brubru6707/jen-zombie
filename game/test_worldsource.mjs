// Stage 4 contracts without a camera, a GPU or a server: the world request
// parks until a frame exists, POSTs it, and degrades to the canned GET on
// every failure; the grabber does the GL dance in the right order and puts
// the state back.
import { WorldSource } from './www/js/worldsource.js';
import { FrameGrabber } from './www/js/framegrab.js';

let failures = 0;
const check = (n, c, d = '') => { console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++; };
const settle = () => new Promise(r => setTimeout(r, 0));
const blob = n => ({ size: n });

function fakeFetch(script) {   // script(url, opt) -> {ok,status,json} | throws | Promise
  const calls = [];
  const f = (url, opt = {}) => { calls.push({ url, method: opt.method || 'GET', ctype: (opt.headers || {})['Content-Type'], body: opt.body, signal: opt.signal });
    const r = script(url, opt); return r instanceof Promise ? r : Promise.resolve(r); };
  return { f, calls };
}
const okWorld = (w = {}) => ({ ok: true, status: 200, json: async () => ({ biome: 'water', name: 'W', ...w }) });

console.log('--- frame path: park, grab, POST, resolve ---');
{
  let t = 0; const { f, calls } = fakeFetch((url, opt) => opt.method === 'POST' ? okWorld({ caption: 'a beach', fallback: false }) : okWorld({ source: 'canned-no-frame' }));
  const ws = new WorldSource({ fetchImpl: f, now: () => t, cameraWaitMs: 6000 });
  const p = ws.request({ seq: 3, delay: '' });
  ws.tick(t += 16, () => null); ws.tick(t += 16, () => null);
  check('no camera yet: nothing sent, request still pending', calls.length === 0 && ws.pending && ws.notReady === 2);
  ws.tick(t += 16, () => ({ blob: Promise.resolve(blob(4321)), ms: 7.5 }));
  await settle();
  check('frame available: exactly one POST', calls.length === 1 && calls[0].method === 'POST');
  check('POST is image/jpeg with the blob as body and the query string', calls[0].ctype === 'image/jpeg' && calls[0].body.size === 4321 && calls[0].url === '/world?seq=3');
  check('grab cost recorded', ws.lastCaptureMs === 7.5 && ws.maxCaptureMs === 7.5 && ws.lastBytes === 4321);
  const w = await p;
  check('resolves with the upstream world, via frame', w.via === 'frame' && w.source === 'upstream' && w.caption === 'a beach' && w.fallback === false);
  check('pending cleared, stats sane', !ws.pending && ws.posts === 1 && ws.gets === 0 && ws.lastSource === 'upstream');
  ws.tick(t += 16, () => { throw new Error('must not grab with nothing pending'); });
  check('tick with nothing pending is a no-op', true);
}

console.log('--- camera never ready -> canned GET after the wait ---');
{
  let t = 0; const { f, calls } = fakeFetch(() => okWorld({ source: 'canned-no-frame' }));
  const ws = new WorldSource({ fetchImpl: f, now: () => t, cameraWaitMs: 1000 });
  const p = ws.request({ seq: 1 });
  for (let i = 0; i < 70; i++) ws.tick(t += 16, () => null);
  await settle();
  check('one GET after cameraWaitMs, no POST', calls.length === 1 && calls[0].method === 'GET');
  const w = await p;
  check('canned world, reason says why', w.via === 'get' && w.source === 'canned-no-frame' && /camera not ready/.test(w.reason));
  check('ticks after the GET do not grab again', (ws.tick(t += 16, () => { throw new Error('x'); }), calls.length === 1));
}

console.log('--- failures degrade to GET, never reject while GET works ---');
{
  let t = 0; let postStatus = 500;
  const { f, calls } = fakeFetch((url, opt) => opt.method === 'POST' ? { ok: false, status: postStatus } : okWorld({ source: 'canned-no-frame' }));
  const ws = new WorldSource({ fetchImpl: f, now: () => t });
  let p = ws.request({ seq: 1 }); ws.tick(t, () => ({ blob: Promise.resolve(blob(10)), ms: 1 })); await settle(); await settle();
  let w = await p;
  check('POST 500 -> GET fallback', calls.length === 2 && calls[1].method === 'GET' && w.via === 'get' && /HTTP 500/.test(w.reason) && ws.postFails === 1);
  p = ws.request({ seq: 2 }); ws.tick(t, () => { throw new Error('gl lost'); }); await settle(); w = await p;
  check('grab throwing -> GET with the reason', w.via === 'get' && /capture: gl lost/.test(w.reason));
  p = ws.request({ seq: 3 }); ws.tick(t, () => ({ blob: Promise.resolve(null), ms: 1 })); await settle(); await settle(); w = await p;
  check('empty JPEG -> GET, never an empty POST', w.via === 'get' && /empty frame/.test(w.reason) && calls.filter(c => c.method === 'POST' && (!c.body || !c.body.size)).length === 0);
  p = ws.request({ seq: 4 }); ws.tick(t, () => ({ blob: Promise.reject(new Error('toBlob died')), ms: 1 })); await settle(); await settle(); w = await p;
  check('encode rejecting -> GET', w.via === 'get' && /encode/.test(w.reason));
  const { f: f2 } = fakeFetch((url, opt) => opt.method === 'POST' ? Promise.reject(new TypeError('network')) : okWorld());
  const ws2 = new WorldSource({ fetchImpl: f2, now: () => t });
  p = ws2.request({ seq: 5 }); ws2.tick(t, () => ({ blob: Promise.resolve(blob(5)), ms: 1 })); await settle(); await settle(); await settle(); w = await p;
  check('POST network error -> GET', w.via === 'get' && w.source === 'canned');
}

console.log('--- GET failing too: reject, WorldCycle keeps the current world ---');
{
  let t = 0; const { f } = fakeFetch(() => ({ ok: false, status: 503 }));
  const ws = new WorldSource({ fetchImpl: f, now: () => t, cameraWaitMs: 0 });
  const p = ws.request({ seq: 1 }); ws.tick(t + 1, () => null);
  let err = null; try { await p; } catch (e) { err = e; }
  check('rejects with the HTTP error', err && /503/.test(err.message) && ws.getFails === 1 && !ws.pending);
  const none = new WorldSource({ fetchImpl: null });
  const p2 = none.request({}); none.tick(1e9, () => ({ blob: Promise.resolve(blob(1)), ms: 1 }));
  let e2 = null; try { await p2; } catch (e) { e2 = e; }
  check('no fetch at all rejects cleanly', !!e2);
}

console.log('--- superseding and the abort timer ---');
{
  let t = 0; const { f, calls } = fakeFetch(() => okWorld());
  const ws = new WorldSource({ fetchImpl: f, now: () => t });
  const first = ws.request({ seq: 1 }); let e1 = null; first.catch(e => { e1 = e; });
  const second = ws.request({ seq: 2 }); await settle();
  check('a newer request supersedes an unstarted one', e1 && /superseded/.test(e1.message));
  ws.tick(t, () => ({ blob: Promise.resolve(blob(9)), ms: 2 })); await settle(); await second;
  check('...and only the newer one is sent', calls.length === 1 && calls[0].url === '/world?seq=2');
  const hang = new Promise(() => {});
  const { f: fh, calls: ch } = fakeFetch(() => hang);
  const ws2 = new WorldSource({ fetchImpl: fh, timeoutMs: 30 });
  ws2.request({ seq: 9 }); ws2.tick(1, () => ({ blob: Promise.resolve(blob(9)), ms: 2 })); await settle();
  await new Promise(r => setTimeout(r, 60));
  check('a hung POST is aborted at timeoutMs', ch.length === 1 && ch[0].signal && ch[0].signal.aborted === true);
}

console.log('--- FrameGrabber: GL order, downscale, state restored ---');
{
  const calls = []; const C = {};
  ['FRAMEBUFFER','COLOR_ATTACHMENT0','TEXTURE_2D','RGBA','UNSIGNED_BYTE','LINEAR','CLAMP_TO_EDGE','TEXTURE_MIN_FILTER','TEXTURE_MAG_FILTER','TEXTURE_WRAP_S','TEXTURE_WRAP_T','FRAMEBUFFER_COMPLETE','VERTEX_SHADER','FRAGMENT_SHADER','COMPILE_STATUS','LINK_STATUS','ARRAY_BUFFER','STATIC_DRAW','FRAMEBUFFER_BINDING','VIEWPORT','CURRENT_PROGRAM','VERTEX_ARRAY_BINDING','DEPTH_TEST','BLEND','SCISSOR_TEST','CULL_FACE','STENCIL_TEST','TEXTURE0','FLOAT','TRIANGLES'].forEach((k, i) => C[k] = 1000 + i);
  const PREV_FBO = { prev: 'fbo' }, PREV_PROG = { prev: 'prog' }, PREV_VAO = { prev: 'vao' };
  let compileOk = true;
  const gl = new Proxy({
    ...C,
    getParameter: p => p === C.FRAMEBUFFER_BINDING ? PREV_FBO : p === C.VIEWPORT ? new Int32Array([0, 0, 1080, 2340]) : p === C.CURRENT_PROGRAM ? PREV_PROG : p === C.VERTEX_ARRAY_BINDING ? PREV_VAO : null,
    getShaderParameter: () => compileOk, getProgramParameter: () => true, getShaderInfoLog: () => 'bad shader', getProgramInfoLog: () => '',
    checkFramebufferStatus: () => C.FRAMEBUFFER_COMPLETE,
    createShader: () => ({}), createProgram: () => ({ prog: 'mine' }), createBuffer: () => ({}), createFramebuffer: () => ({ fbo: 'mine' }), createTexture: () => ({}),
    getAttribLocation: () => 0, getUniformLocation: () => ({}),
  }, { get: (o, k) => (k in o) ? o[k] : (...a) => { calls.push([k, ...a]); } });
  const g = new FrameGrabber(gl, { maxWidth: 800 });
  check('1920x886 -> 800x369 (aspect kept)', g.targetSize(1920, 886).join('x') === '800x369');
  check('a small source is never upscaled', g.targetSize(640, 295).join('x') === '640x295');
  check('portrait 886x1920 -> 369x800 (long edge capped)', g.targetSize(886, 1920).join('x') === '369x800');
  const tex = { cam: true };
  const r = g.grab(tex, 1920, 886);
  const names = calls.map(c => c[0]);
  check('returns size and a cost', r.width === 800 && r.height === 369 && r.ms >= 0 && g.count === 1);
  const rp = calls.find(c => c[0] === 'readPixels');
  check('readPixels reads the SMALL target only', rp && rp[3] === 800 && rp[4] === 369 && rp[7].length === 800 * 369 * 4);
  const idx = n => names.indexOf(n);
  check('order: bind fbo -> draw -> readPixels', idx('drawArrays') > names.lastIndexOf('bindFramebuffer', idx('drawArrays')) && idx('readPixels') > idx('drawArrays'));
  check('camera texture bound on unit 0 for the draw', calls.some(c => c[0] === 'bindTexture' && c[2] === tex) && calls.some(c => c[0] === 'activeTexture' && c[1] === C.TEXTURE0));
  const lastFbo = calls.filter(c => c[0] === 'bindFramebuffer').pop();
  const lastProg = calls.filter(c => c[0] === 'useProgram').pop();
  const lastVao = calls.filter(c => c[0] === 'bindVertexArray').pop();
  const lastVp = calls.filter(c => c[0] === 'viewport').pop();
  check('previous framebuffer, program, VAO and viewport restored',
        lastFbo[2] === PREV_FBO && lastProg[1] === PREV_PROG && lastVao[1] === PREV_VAO && lastVp[3] === 1080 && lastVp[4] === 2340);
  check('camera texture unbound afterwards', calls.filter(c => c[0] === 'bindTexture').pop()[2] === null);
  const before = calls.length; g.grab(tex, 1920, 886);
  check('second grab reuses the program and target (no recompile, no texImage2D)', !calls.slice(before).some(c => c[0] === 'compileShader' || c[0] === 'texImage2D'));
  check('stats report cost and count', g.stats().captures === 2 && g.stats().size === '800x369');
  const g3 = new FrameGrabber(gl, { maxWidth: 800 }); const b3 = calls.length;
  check('warm() compiles and allocates without a grab', g3.warm(886, 1920) === true && calls.slice(b3).some(c => c[0] === 'linkProgram') && calls.slice(b3).some(c => c[0] === 'texImage2D') && g3.count === 0);
  const b4 = calls.length; g3.grab(tex, 886, 1920);
  check('a grab after warm() neither recompiles nor reallocates', !calls.slice(b4).some(c => c[0] === 'linkProgram' || c[0] === 'texImage2D') && g3.count === 1);
  compileOk = false; const g2 = new FrameGrabber(gl);
  let threw = null; try { g2.grab(tex, 10, 10); } catch (e) { threw = e; }
  check('a GL failure throws (caller falls back) and is counted', threw && /bad shader/.test(threw.message) && g2.fails === 1 && g2.stats().error);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all Stage 4 capture contracts hold');
process.exit(failures ? 1 : 0);

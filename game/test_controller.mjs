// Exercises the handheld input module and the LED meter without a bridge, a
// browser or a board: fake EventSource, fake fetch, fake clock.
import { Controller, LINK, SENS_NORMAL, SENS_HIGH, FLICK_ON, FLICK_OFF } from './www/js/controller.js';
import { LedMeter, ledsFromHostility } from './www/js/led.js';

let failures = 0;
const check = (n, c, d = '') => { console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++; };
const near = (a, b, e = 1e-9) => Math.abs(a - b) < e;
const settle = () => new Promise(r => setTimeout(r, 0));   // flush the fetch promise chain
const state = (o = {}) => ({ t: 'state', x: 0, y: 0, sw: 0, atk: 0, sens: SENS_NORMAL, connected: true, seq: 1, ts: 0, ...o });
const NEUTRAL = p => p.x === 0 && p.y === 0 && p.atk === 0 && p.sw === 0 &&
  !p.atkPressed && !p.swPressed && !p.sensChanged && p.xFlick === 0 && p.yFlick === 0;

console.log('--- no controller: neutral, no edges, nothing thrown ---');
{
  let t = 0; const c = new Controller({ now: () => t, EventSourceImpl: null });
  check('start() without EventSource is inert', c.start() === false && c.link === LINK.UNSUPPORTED);
  const p = c.poll();
  check('poll is neutral and disconnected', !p.connected && NEUTRAL(p) && !p.dropped && !p.restored);
  check('junk messages are ignored', c.apply(null) === false && c.apply({ t: 'nope' }) === false && c.apply('x') === false);
  check('status is sane with no frames', c.status().frames === 0 && c.status().ageMs === null);
}

console.log('--- link comes up: switch position is adopted, held buttons are not presses ---');
{
  let t = 0; const c = new Controller({ now: () => t, EventSourceImpl: null });
  c.apply(state({ atk: 1, sw: 1, sens: SENS_HIGH, x: 0.9 }));
  const p = c.poll();
  check('connected on the first frame', p.connected && p.restored);
  check('sens reported as changed so the preset syncs', p.sensChanged && p.sens === SENS_HIGH);
  check('a button held at connect is not a press', !p.atkPressed && !p.swPressed);
  check('a stick held at connect is not a flick', p.xFlick === 0);
  check('raw axes and buttons are still visible', p.atk === 1 && p.sw === 1 && near(p.x, 0.9));
  const p2 = c.poll();
  check('edges clear after one poll', !p2.restored && !p2.sensChanged && p2.connected);
}

console.log('--- buttons are latched between polls ---');
{
  let t = 0; const c = new Controller({ now: () => t, EventSourceImpl: null });
  c.apply(state()); c.poll();
  c.apply(state({ atk: 1 })); c.apply(state({ atk: 0 }));     // tap inside one frame
  const p = c.poll();
  check('a tap shorter than a frame still registers', p.atkPressed && p.atk === 0);
  check('no press on the next poll', !c.poll().atkPressed);
  c.apply(state({ atk: 1 })); c.apply(state({ atk: 1 })); c.apply(state({ atk: 1 }));
  check('holding is ONE press', c.poll().atkPressed && !c.poll().atkPressed);
  c.apply(state({ atk: 0 })); c.apply(state({ atk: 1 })); c.apply(state({ atk: 0 })); c.apply(state({ atk: 1 }));
  check('two taps in one frame collapse to one press (no queueing)', c.poll().atkPressed === true);
  c.apply(state({ sw: 1 })); c.apply(state({ sw: 0 }));
  check('stick button latches the same way', c.poll().swPressed);
}

console.log('--- sensitivity toggle ---');
{
  let t = 0; const c = new Controller({ now: () => t, EventSourceImpl: null });
  c.apply(state()); c.poll();
  c.apply(state({ sens: SENS_HIGH }));
  const p = c.poll();
  check('2048 -> 4095 is a change to high', p.sensChanged && p.sens === SENS_HIGH);
  c.apply(state({ sens: SENS_HIGH }));
  check('same position again is not a change', !c.poll().sensChanged);
  c.apply(state({ sens: SENS_NORMAL }));
  check('back to 2048 is a change to normal', c.poll().sensChanged && c.sens === SENS_NORMAL);
}

console.log('--- the sens button is momentary: the game acts on the PRESS ---');
{
  let t = 0; const c = new Controller({ now: () => t, EventSourceImpl: null });
  c.apply(state()); c.poll();
  c.apply(state({ sens: SENS_HIGH }));
  const p = c.poll();
  check('pressing it is one press event', p.sensPressed === true);
  c.apply(state({ sens: SENS_HIGH }));
  check('holding it down does NOT repeat (no need to hold for it to stay)', !c.poll().sensPressed);
  c.apply(state({ sens: SENS_NORMAL }));
  check('releasing it is not a press, so nothing is undone', !c.poll().sensPressed);
  c.apply(state({ sens: SENS_HIGH }));
  check('pressing again is the next press', c.poll().sensPressed === true);
  c.apply(state({ sens: SENS_NORMAL })); c.apply(state({ sens: SENS_HIGH })); c.apply(state({ sens: SENS_NORMAL }));
  check('a press and release inside one frame still registers once', c.poll().sensPressed === true);
  check('and only once', !c.poll().sensPressed);
  // Contact bounce: several crossings within a few ms is ONE press.
  t += 1000; c.apply(state({ sens: SENS_NORMAL }), t); c.poll(t);
  for (let i = 0; i < 6; i++) { t += 4; c.apply(state({ sens: SENS_HIGH }), t); t += 4; c.apply(state({ sens: SENS_NORMAL }), t); }
  check('a bouncing contact is one press, not six', c.poll(t).sensPressed === true && c.sensBounces === 5,
        `${c.sensBounces} bounces swallowed`);
  t += 200; c.apply(state({ sens: SENS_HIGH }), t);
  check('a deliberate press after the debounce still counts', c.poll(t).sensPressed === true);
  // The link dropping with the button held must not fire a press on return.
  t += 5000; c.poll();
  c.apply(state({ sens: SENS_HIGH }), t);
  check('reconnecting with the button already held is not a press', !c.poll(t).sensPressed);
  c.apply(state({ sens: SENS_NORMAL }), t); c.apply(state({ sens: SENS_HIGH }), t);
  check('...and the next real press still works', c.poll(t).sensPressed === true);
}

console.log('--- stick flicks (no locomotion) ---');
{
  let t = 0; const c = new Controller({ now: () => t, EventSourceImpl: null });
  c.apply(state()); c.poll();
  c.apply(state({ x: FLICK_ON }));
  check('push right past the threshold is one flick', c.poll().xFlick === 1);
  c.apply(state({ x: 0.95 }));
  check('holding it there does not repeat', c.poll().xFlick === 0);
  c.apply(state({ x: FLICK_OFF + 0.05 }));
  check('not re-armed until inside the release band', (c.apply(state({ x: 0.9 })), c.poll().xFlick === 0));
  c.apply(state({ x: 0.0 })); c.apply(state({ x: -0.8 }));
  check('centre then push left is a left flick', c.poll().xFlick === -1);
  c.apply(state({ x: 0 })); c.apply(state({ y: -0.7 }));
  check('y flicks independently (up is negative, screen oriented)', c.poll().yFlick === -1);
  c.apply(state({ x: 7, y: -7 }));
  check('axes are clamped to +/-1', c.x === 1 && c.y === -1);
}

console.log('--- staleness: silence is disconnection, neutral not last-known ---');
{
  let t = 0; const c = new Controller({ now: () => t, staleMs: 1500, EventSourceImpl: null });
  c.apply(state({ x: 0.8, atk: 1 })); c.poll();
  t = 1400;
  check('inside the window it is still live', c.poll().connected && near(c.poll().x, 0.8));
  t = 1600;
  const p = c.poll();
  check('past the window: gone, neutral, one drop', !p.connected && p.dropped && NEUTRAL(p) && c.drops === 1);
  check('reported once, not every frame', !c.poll().dropped && c.drops === 1);
  check('sens position survives the drop', c.sens === SENS_NORMAL && c.poll().sens === SENS_NORMAL);
  c.apply(state({ sens: SENS_HIGH, atk: 1 }));
  const r = c.poll();
  check('frames resume: restored, switch re-synced, held button ignored',
        r.connected && r.restored && r.sensChanged && r.sens === SENS_HIGH && !r.atkPressed);
}

console.log('--- bridge says the board is gone ---');
{
  let t = 0; const c = new Controller({ now: () => t, EventSourceImpl: null });
  c.apply(state({ x: 0.5 })); c.poll();
  c.apply(state({ x: 0.5, connected: false }));
  const p = c.poll();
  check('connected:false from the bridge is a drop with neutral input', !p.connected && p.dropped && NEUTRAL(p));
  check('heartbeats with connected:false keep it disconnected', (c.apply(state({ connected: false })), !c.poll().connected));
}

console.log('--- EventSource lifecycle (fake) ---');
{
  const made = [];
  class FakeES {
    constructor(url) { this.url = url; this.readyState = 0; made.push(this); }
    close() { this.readyState = 2; this.closed = true; }
  }
  let t = 0;
  const c = new Controller({ now: () => t, EventSourceImpl: FakeES, reconnectMs: 20, maxReconnectMs: 40 });
  check('start() opens the stream at /events', c.start() === true && made.length === 1 && made[0].url === '/events');
  check('link is connecting until onopen', c.link === LINK.CONNECTING);
  made[0].onopen(); check('onopen -> open', c.link === LINK.OPEN);
  made[0].onmessage({ data: JSON.stringify(state({ atk: 1 })) });
  made[0].onmessage({ data: '{not json' });
  check('messages feed apply(), bad JSON ignored', c.frames === 1 && c.poll().connected);
  made[0].readyState = 0; made[0].onerror();
  check('transient error: connecting, input neutral', c.link === LINK.CONNECTING && !c.poll().connected);
  made[0].readyState = 2; made[0].onerror();
  check('closed stream (404 on the Mac fallback): link closed', c.link === LINK.CLOSED && c.es === null);
  await new Promise(r => setTimeout(r, 35));
  check('reopens after backoff', made.length === 2);
  made[1].readyState = 2; made[1].onerror();
  await new Promise(r => setTimeout(r, 30));
  check('backoff grows (second wait is longer)', made.length === 2);
  await new Promise(r => setTimeout(r, 30));
  check('...and it reopens again', made.length === 3);
  c.stop();
  check('stop() closes and goes idle', made[2].closed && c.link === LINK.IDLE && !c.poll().connected);
  await new Promise(r => setTimeout(r, 60));
  check('no reopen after stop()', made.length === 3);
}

console.log('--- LED meter: climbing three-lamp map ---');
{
  const eq = (a, b) => a.length === 3 && a.every((v, i) => near(v, b[i], 1e-9));
  check('silence -> all off', eq(ledsFromHostility(0), [0, 0, 0]));
  check('NaN / negative -> all off', eq(ledsFromHostility(NaN), [0, 0, 0]) && eq(ledsFromHostility(-1), [0, 0, 0]));
  check('full -> all on, >1 clamped', eq(ledsFromHostility(1), [1, 1, 1]) && eq(ledsFromHostility(3), [1, 1, 1]));
  check('low hostility lights green only', eq(ledsFromHostility(0.17), [0.5, 0, 0]));
  check('mid hostility: green full, yellow half, red off', eq(ledsFromHostility(0.5), [1, 0.5, 0]));
  check('high hostility: red climbing', (() => { const l = ledsFromHostility(0.83); return l[0] === 1 && l[1] === 1 && l[2] > 0.4 && l[2] < 0.6; })());
  let mono = true, prev = [0, 0, 0];
  for (let h = 0; h <= 1.0001; h += 0.01) { const l = ledsFromHostility(h); if (l.some((v, i) => v < prev[i] - 1e-9)) mono = false; prev = l; }
  check('every lamp is monotonic as hostility rises', mono);
  check('two decimals, as the wire protocol wants', ledsFromHostility(0.1234).every(v => near(v * 100, Math.round(v * 100))));
}

console.log('--- LED meter: on change, <= 10 Hz, one in flight, never per frame ---');
{
  const posts = []; let resolveLast = null;
  const fetchImpl = (url, opt) => { posts.push({ url, body: JSON.parse(opt.body), keepalive: opt.keepalive }); return new Promise(r => { resolveLast = r; }); };
  const m = new LedMeter({ hz: 10, fetchImpl });
  check('first value goes out', m.update(0, 0.5, true) === true && posts.length === 1 && posts[0].url === '/led');
  check('payload shape is {led:[g,y,r]}', Array.isArray(posts[0].body.led) && posts[0].body.led.length === 3);
  resolveLast({ ok: true }); await settle();
  check('same value is not re-sent', m.update(50, 0.5, true) === false && posts.length === 1);
  check('a change inside the rate window waits', m.update(60, 0.9, true) === false && posts.length === 1 && m.pending);
  check('...and is sent once the window opens', m.update(100, 0.9, true) === true && posts.length === 2);
  resolveLast({ ok: true }); await settle();
  let n = 0;
  for (let i = 0; i < 60; i++) {           // ~1 s of frames, the board answering promptly
    if (m.update(200 + i * 16.7, Math.random(), true)) { n++; resolveLast({ ok: true }); await settle(); }
  }
  check('60 frames of noise, ~1 s, at most 11 posts', n <= 11 && n >= 8, `posts=${n}`);
  while (resolveLast) { const r = resolveLast; resolveLast = null; r({ ok: true }); await settle(); }
  const before = posts.length;
  m.update(5000, 0.7, true);                          // in flight now
  m.update(5200, 0.2, true);
  check('only one request in flight', posts.length === before + 1 && m.inFlight);
  resolveLast({ ok: true }); await settle();
  check('the pending change lands after the reply', m.update(5300, 0.2, true) === true);
  resolveLast({ ok: true }); await settle();
  const b2 = posts.length;
  check('disabled (no handheld): nothing sent', m.update(6000, 0.9, false) === false && posts.length === b2);
  check('re-enabled: current value sent', m.update(6100, 0.9, true) === true && posts.length === b2 + 1);
  resolveLast({ ok: true }); await settle();
  check('tiny hostility jitter is quantised away', m.update(7000, 0.171, true) === true && m.update(7200, 0.174, true) === false);
  resolveLast({ ok: true }); await settle();
  check('a routine update does NOT use fetch keepalive', posts[0].keepalive !== true,
        'the browser allows only a handful in flight; at 10 Hz most of them failed');
  check('off() posts all zeros', m.off(8000) === true && posts[posts.length - 1].body.led.every(v => v === 0));
  check('...but the one on the way out does, so it survives the page closing',
        posts[posts.length - 1].keepalive === true);
  resolveLast({ ok: true }); await settle();
  check('sent counter is accurate', m.sent === posts.length, `${m.sent}/${posts.length}`);

  // The board holds the last line for ever, so a steady meter must keep
  // speaking or the bridge cannot tell it from a game that has died.
  const ka = new LedMeter({ hz: 10, keepaliveMs: 2000, fetchImpl });
  const kaFrom = posts.length;
  check('a keepalive meter sends the first value', ka.update(0, 0.5, true) === true);
  resolveLast({ ok: true }); await settle();
  check('and does not repeat it while it is fresh', ka.update(500, 0.5, true) === false && posts.length === kaFrom + 1);
  check('but re-sends the unchanged value once it goes stale',
        ka.update(2100, 0.5, true) === true && posts.length === kaFrom + 2,
        'silence on this channel means the game is gone');
  resolveLast({ ok: true }); await settle();
  check('the keepalive carries the real value, not zeros',
        posts[posts.length - 1].body.led.some(v => v > 0));
  check('a disabled meter stays silent even when stale', ka.update(9000, 0.5, false) === false);
  const bad = new LedMeter({ fetchImpl: () => { throw new Error('boom'); } });
  check('a throwing fetch is counted, never thrown', bad.update(0, 0.5, true) === false && bad.failed === 1);
  const none = new LedMeter({ fetchImpl: null });
  check('no fetch at all is inert', none.update(0, 0.5, true) === false);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all handheld + LED contracts hold');
process.exit(failures ? 1 : 0);

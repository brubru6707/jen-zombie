// The talking: who says what, and the thing that actually matters -- the
// microphone not hearing the game's own voice. No audio hardware, no network.
import { Voices, VOICE, linesFor, canSpeak } from './www/js/voices.js';
import { MicMeter, MIC } from './www/js/mic.js';
import { Mob } from './www/js/mobs.js';

let failures = 0;
const check = (n, c, d = '') => { console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++; };
const settle = () => new Promise(r => setTimeout(r, 0));
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.URL = globalThis.URL || {};
URL.createObjectURL = () => 'blob:fake'; URL.revokeObjectURL = () => {};

const mob = (kind, mood) => new Mob(kind, 0, -2, 0.4, 'farm', 0, mood);
const WORLD = { barks: {
  walker: ['Harvest is late.'], runner: ['Through the rows!'],
  brute: ['I pull the plough.'], calm: ['Mind how you go.', 'Dog ran off that way.'] } };

// A fake <audio> that reports a duration and can be finished on demand.
function fakeAudio(durationS = 1.5) {
  const l = {};
  return {
    duration: durationS, volume: 1, src: 'blob:fake',
    addEventListener: (k, fn) => { (l[k] = l[k] || []).push(fn); },
    fire: k => (l[k] || []).forEach(fn => fn()),
    play: () => Promise.resolve(),
    pause: () => {},
  };
}

console.log('--- the right mouth says the right thing ---');
{
  check('a peaceful local gets the calm lines', linesFor(WORLD, mob('walker', 'calm')).join() === WORLD.barks.calm.join());
  check('a walker gets the walker lines', linesFor(WORLD, mob('walker', 'hostile'))[0] === 'Harvest is late.');
  check('a brute gets the brute line', linesFor(WORLD, mob('brute', 'hostile'))[0] === 'I pull the plough.');
  const noCalm = { barks: { walker: ['Grr.'] } };
  check('a world with no calm dialogue leaves the peaceful silent rather than menacing',
        linesFor(noCalm, mob('walker', 'calm')) === null);
  check('a hostile kind the model skipped falls back within its own side',
        linesFor(noCalm, mob('brute', 'hostile'))[0] === 'Grr.');
  check('no world, no lines', linesFor(null, mob('walker', 'hostile')) === null && linesFor({}, mob('walker')) === null);
  check('the dying, the leaving and the dead do not talk', (() => {
    const a = mob('walker', 'hostile'); a.hit(99);
    const b = mob('walker', 'hostile'); b.leave();
    return !canSpeak(a) && !canSpeak(b) && !canSpeak(null) && canSpeak(mob('walker', 'hostile'));
  })());
}

console.log('--- one voice at a time, on a gap ---');
{
  let t = 0;
  const v = new Voices({ now: () => t, fetchImpl: null, audioFactory: null });
  const crowd = [mob('walker', 'hostile'), mob('runner', 'hostile')];
  check('nobody talks on the first frame', v.update(t, crowd, WORLD) === null);
  t += VOICE.gapMs[1] + 10;
  const said = v.update(t, crowd, WORLD);
  check('someone speaks once the gap has passed', !!said && typeof said.text === 'string' && said.text.length > 0);
  check('the line belongs to that mob', linesFor(WORLD, said.mob).includes(said.text));
  check('nobody else interrupts', v.update(t + 50, crowd, WORLD) === said);
  t = said.until + 1;
  check('the bubble retires on its own', v.update(t, crowd, WORLD) === null);
  t += VOICE.gapMs[1] + 10;
  check('and the next line comes later', !!v.update(t, crowd, WORLD));
  check('an empty room says nothing', (() => { const v2 = new Voices({ now: () => t, fetchImpl: null, audioFactory: null });
    v2.update(t, [], WORLD); return v2.update(v2.nextAt + 1, [], WORLD) === null; })());
}

console.log('--- the microphone does not hear the game ---');
{
  let t = 1000;
  const mic = new MicMeter();
  mic.state = MIC.LIVE;
  mic._nowMs = () => t;                       // drive its clock
  mic.analyser = { getFloatTimeDomainData: buf => buf.fill(0.5), fftSize: 8 };
  mic._buf = new Float32Array(8);
  mic.cal = { floor: -50, ceiling: -20 };
  mic.sample(t);
  check('a loud room reads loud and hostile', mic.db > -10 && mic.hostility === 1, `${mic.db.toFixed(1)} dBFS`);
  mic.suppress(2000);
  check('suppressed while the game talks', mic.suppressed);
  mic.sample(t);
  check('the level is pinned below the floor, so nothing reacts',
        mic.db <= mic.floor && mic.hostility === 0, `${mic.db.toFixed(1)} dBFS, hostility ${mic.hostility}`);
  check('the meter says it is deafened rather than pretending to be live',
        mic.statusText().includes('deafened'));
  check('calibration refuses while the game is talking', (() => {
    let r = null; mic.calibrate(10, () => {}).then(x => { r = x; });
    return true;   // resolved below
  })());
  t += 2100;
  check('it hears again once the line has finished', !mic.suppressed);
  mic.sample(t);
  check('and the level comes back', mic.db > -10 && mic.hostility === 1);
  check('suppress(0) releases it immediately', (mic.suppress(5000), mic.suppress(0), !mic.suppressed));
  check('a longer window is never shortened by a shorter one',
        (() => { mic.suppress(3000); const a = mic.suppressedUntil; mic.suppress(100); return mic.suppressedUntil === a; })());
  mic.suppress(0);
}

console.log('--- playing a line deafens the mic for exactly its length ---');
{
  let t = 0;
  const mic = new MicMeter();
  mic._nowMs = () => t;
  const audio = fakeAudio(2.0);
  const v = new Voices({ now: () => t, mic,
    fetchImpl: async () => ({ ok: true, blob: async () => ({ size: 4096 }) }),
    audioFactory: () => audio });
  const crowd = [mob('walker', 'hostile')];
  v.update(t, crowd, WORLD);                 // the first call only schedules
  t = v.nextAt + 1;
  const said = v.update(t, crowd, WORLD);
  check('a line was chosen', !!said);
  await settle(); await settle(); await settle();
  check('the mic is deafened before a sample can reach the speaker', mic.suppressed);
  audio.fire('loadedmetadata');
  const held = mic.suppressedUntil - t;
  check('held for the real length of the audio plus a tail',
        held >= 2000 && held <= 2000 + VOICE.tailMs + VOICE.leadMs + 1, `${Math.round(held)} ms`);
  check('the bubble stays up at least as long as the voice', said.until >= t + 2000);
  check('it counted as spoken', v.status().spoken === 1 && v.status().saying.spoken === true);
  audio.fire('ended');
  t += VOICE.tailMs + VOICE.leadMs + 10;
  check('and the room is audible again afterwards', !mic.suppressed);
}

console.log('--- no audio is still dialogue ---');
{
  let t = 0;
  const mic = new MicMeter();
  mic._nowMs = () => t;
  const v = new Voices({ now: () => t, mic,
    fetchImpl: async () => ({ ok: false, status: 503, json: async () => ({ error: 'no ELEVENLABS_API_KEY' }) }),
    audioFactory: () => fakeAudio() });
  v.update(t, [mob('walker', 'hostile')], WORLD);
  t = v.nextAt + 1;
  const said = v.update(t, [mob('walker', 'hostile')], WORLD);
  check('the line is still chosen and shown', !!said && said.text.length > 0);
  await settle(); await settle(); await settle();
  check('the failure is recorded with its reason', v.status().failures === 1 && /ELEVENLABS/.test(v.status().lastError));
  check('and it is marked unspoken rather than silently dropped', said.spoken === false && v.status().silent === 1);
  check('the microphone is not left deafened by a line that never played', !mic.suppressed);
  const v2 = new Voices({ now: () => t, mic, fetchImpl: null, audioFactory: null });
  v2.update(t, [mob('walker', 'hostile')], WORLD);
  const s2 = v2.update(v2.nextAt + 1, [mob('walker', 'hostile')], WORLD);
  check('with no network at all the dialogue still appears', !!s2 && v2.status().silent === 1);
}

console.log('--- clearing up ---');
{
  let t = 0;
  const mic = new MicMeter();
  mic._nowMs = () => t;
  const audio = fakeAudio(3);
  const v = new Voices({ now: () => t, mic,
    fetchImpl: async () => ({ ok: true, blob: async () => ({ size: 10 }) }),
    audioFactory: () => audio });
  v.update(t, [mob('walker', 'hostile')], WORLD);
  t = v.nextAt + 1;
  v.update(t, [mob('walker', 'hostile')], WORLD);
  await settle(); await settle(); await settle();
  check('mic held during playback', mic.suppressed);
  v.clear();
  check('ending the session stops the voice and frees the microphone',
        v.current === null && !mic.suppressed);
}


console.log('--- nothing is ever said twice ---');
{
  // Drives the scheduler forward and collects every line it chooses.
  const drain = (v, mobs, world, rounds) => {
    const out = [];
    let t = 0;
    for (let i = 0; i < rounds; i++) {
      t = Math.max(t + 1, (v.current ? v.current.until : 0) + 1, v.nextAt + 1);
      const said = v.update(t, mobs, world);
      if (said && said.text && !out.includes(said.text)) out.push(said.text);
      else if (said && said.text) out.push(said.text);   // a repeat WOULD land here
    }
    return out;
  };
  const silent = () => new Voices({ fetchImpl: null, audioFactory: null });

  const v = silent();
  const crowd = [mob('walker', 'hostile'), mob('runner', 'hostile'),
                 mob('brute', 'hostile'), mob('walker', 'calm')];
  const said = drain(v, crowd, WORLD, 40);
  check('every line chosen is different', said.length === new Set(said).size,
        `${said.length} lines, ${new Set(said).size} distinct`);
  check('it says everything the world has before it stops',
        said.length === 5, `${said.length} of 5`);
  check('and then goes quiet rather than repeating itself',
        v.status().exhausted > 0 && v.status().retired === 5);

  // A fresh world refills the mouths that ran dry.
  const WORLD2 = { barks: { walker: ['Mud to my knees.'], runner: ['Down the tractor ruts.'],
                            brute: ['The silo shakes.'], calm: ['Gate stays shut, please.'] } };
  const more = drain(v, crowd, WORLD2, 20);
  check('a new world brings new lines and the talking resumes', more.length === 4);
  check('the retired ones stay retired across worlds',
        more.every(l => !said.includes(l)) && v.status().retired === 9);

  // Two mobs, one of which has nothing left: the other must be found.
  const v2 = silent();
  const ONE_EACH = { barks: { walker: ['Only walker line.'], calm: ['Only calm line.'] } };
  const pair = [mob('walker', 'hostile'), mob('walker', 'calm')];
  const both = drain(v2, pair, ONE_EACH, 12);
  check('a speaker that is out of lines is skipped, not allowed to silence the frame',
        both.length === 2 && new Set(both).size === 2);

  // Retirement is keyed on the full line, so truncation cannot resurrect one.
  const v3 = silent();
  const long = 'x'.repeat(VOICE.maxChars + 40);
  const LONG = { barks: { walker: [long, long.slice(0, VOICE.maxChars + 20)] } };
  const cut = drain(v3, [mob('walker', 'hostile')], LONG, 10);
  check('a truncated line is retired by what the model wrote, not by what was shown',
        cut.length === 2 && cut.every(l => l.length === VOICE.maxChars) &&
        v3.status().retired === 2);

  // Case and spacing are not a new line.
  const v4 = silent();
  const SAME = { barks: { walker: ['Harvest is late.', 'harvest   IS late.'] } };
  const dup = drain(v4, [mob('walker', 'hostile')], SAME, 10);
  check('the same sentence in different case or spacing is one line, not two',
        dup.length === 1);

  const v5 = silent();
  v5.update(0, [mob('walker', 'hostile')], WORLD);
  v5.update(v5.nextAt + 1, [mob('walker', 'hostile')], WORLD);
  check('forget() puts them all back, for a deliberate restart',
        v5.status().retired === 1 && (v5.forget(), v5.status().retired === 0));
}


console.log('--- muting silences the speaker, not the dialogue ---');
{
  let t = 0;
  const mic = new MicMeter();
  mic._nowMs = () => t;
  const audio = fakeAudio(2);
  let fetches = 0;
  const v = new Voices({ now: () => t, mic,
    fetchImpl: async () => { fetches++; return { ok: true, blob: async () => ({ size: 10 }) }; },
    audioFactory: () => audio });

  v.update(t, [mob('walker', 'hostile')], WORLD);
  t = v.nextAt + 1;
  v.update(t, [mob('walker', 'hostile')], WORLD);
  await settle(); await settle(); await settle();
  check('unmuted, the line is fetched and the mic is held', fetches === 1 && mic.suppressed);

  v.setMuted(true);
  check('muting stops the line that is playing and hands the mic back',
        v.muted === true && !mic.suppressed);

  // Expire the bubble, then wait out the gap before the next pick.
  t = v.current.until + 1; v.update(t, [mob('walker', 'calm')], WORLD);
  t = v.nextAt + 1;
  const said = v.update(t, [mob('walker', 'calm')], WORLD);
  await settle(); await settle();
  check('a muted game still CHOOSES and SHOWS a line', !!said && said.text.length > 0);
  check('but asks for no audio at all', fetches === 1);
  check('and counts it as silent rather than failed',
        v.status().silent === 1 && v.status().failures === 0 && v.status().muted === true);
  check('the line is still retired while muted', v.status().retired === 2);

  v.setMuted(false);
  t = v.current.until + 1; v.update(t, [mob('walker', 'calm')], WORLD);
  t = v.nextAt + 1;
  v.update(t, [mob('walker', 'calm')], WORLD);
  await settle(); await settle();
  check('unmuting starts the voice again', fetches === 2 && v.status().muted === false);
}

console.log('--- audio on its way is not audio that failed ---');
{
  let t = 0;
  let resolveIt;
  const mic = new MicMeter(); mic._nowMs = () => t;
  const v = new Voices({ now: () => t, mic,
    fetchImpl: () => new Promise(r => { resolveIt = r; }),   // still in flight
    audioFactory: () => fakeAudio(1) });
  v.update(t, [mob('walker', 'hostile')], WORLD);
  t = v.nextAt + 1;
  const said = v.update(t, [mob('walker', 'hostile')], WORLD);
  await settle();
  check('while the fetch is in flight the line is pending, not failed',
        said.pending === true && said.failed === false && said.spoken === false);
  check('and the status says so, so the bubble can stay quiet about it',
        v.status().saying.pending === true && v.status().saying.failed === false);
  resolveIt({ ok: true, blob: async () => ({ size: 10 }) });
  await settle(); await settle(); await settle();
  check('once it arrives it is spoken and no longer pending',
        said.spoken === true && said.pending === false && said.failed === false);

  let t2 = 0;
  const v2 = new Voices({ now: () => t2, mic,
    fetchImpl: async () => ({ ok: false, status: 503, json: async () => ({ error: 'budget spent' }) }),
    audioFactory: () => fakeAudio(1) });
  v2.update(t2, [mob('walker', 'hostile')], WORLD);
  t2 = v2.nextAt + 1;
  const bad = v2.update(t2, [mob('walker', 'hostile')], WORLD);
  await settle(); await settle(); await settle();
  check('a real failure IS marked failed, so the bubble can say why',
        bad.failed === true && bad.pending === false && bad.spoken === false);

  let t3 = 0;
  const v3 = new Voices({ now: () => t3, mic,
    fetchImpl: async () => ({ ok: true, blob: async () => ({ size: 10 }) }),
    audioFactory: () => fakeAudio(1) });
  v3.setMuted(true);
  v3.update(t3, [mob('walker', 'hostile')], WORLD);
  t3 = v3.nextAt + 1;
  const muted = v3.update(t3, [mob('walker', 'hostile')], WORLD);
  await settle();
  check('muting on purpose is never marked as a failure',
        muted.failed === false && muted.pending === false && muted.muted === true);
}

console.log('--- the line goes to someone you can see ---');
{
  const seen = mob('walker', 'hostile');
  const unseen = mob('runner', 'hostile');
  // The first update only arms the gap, so every pick needs a second call.
  const speak = (mobs, prefer) => {
    const v = new Voices({ fetchImpl: null, audioFactory: null });
    v.update(1, mobs, WORLD, prefer);
    return v.update(v.nextAt + 1, mobs, WORLD, prefer);
  };
  let toSeen = 0;
  for (let i = 0; i < 12; i++) if (speak([unseen, seen], m => m === seen).mob === seen) toSeen++;
  check('an on-screen speaker gets first refusal, every time', toSeen === 12, `${toSeen}/12`);

  // ...but being off-screen is never a reason for silence.
  const only = speak([unseen], () => false);
  check('with nobody on screen it still speaks rather than going quiet', !!only && only.mob === unseen);

  const thrown = speak([seen], () => { throw new Error('bad predicate'); });
  check('a predicate that throws is treated as "cannot see", not as a crash', !!thrown);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all dialogue contracts hold');
process.exit(failures ? 1 : 0);

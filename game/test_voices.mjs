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

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all dialogue contracts hold');
process.exit(failures ? 1 : 0);

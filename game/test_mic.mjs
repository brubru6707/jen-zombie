// Exercises the mic module's maths and calibration without a microphone.
import { OFF_HOSTILITY } from './www/js/mic.js';
import { MOOD_RULE as _MR } from './www/js/population.js';
const MOOD_RULE_DWELL = _MR.dwellMs;
const MOOD_RULE_CALM_BELOW = _MR.calmBelow;
import { MicMeter, MIC, PRESETS, PRESET_ORDER, ESP32_TOGGLE, presetFromToggle,
         rmsToDb, hostilityFrom, percentile, DEFAULT_CAL, CAL_SPAN_DB,
         CEILING_MAX_DB, MIN_SPAN_DB, DB_MIN } from './www/js/mic.js';

// localStorage stand-in so persistence is exercised in node.
let store = {};
globalThis.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};

let failures = 0;
const check = (n, c, d = '') => {
  console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++;
};
const near = (a, b, eps = 0.05) => Math.abs(a - b) < eps;

console.log('--- dBFS conversion ---');
check('full scale sine rms -> ~-3 dBFS', near(rmsToDb(Math.SQRT1_2), -3.01, 0.02), rmsToDb(Math.SQRT1_2).toFixed(2));
check('rms 1.0 -> 0 dBFS', near(rmsToDb(1), 0));
check('rms 0.1 -> -20 dBFS', near(rmsToDb(0.1), -20));
check('digital silence clamps, never -Infinity', rmsToDb(0) === DB_MIN, String(rmsToDb(0)));
check('negative/NaN rms is safe', rmsToDb(NaN) === DB_MIN && rmsToDb(-1) === DB_MIN);

console.log('--- floor/ceiling window ---');
const F = -49.3, C = -21.6;
check('at/below floor -> 0.00', hostilityFrom(F, F, C) === 0 && hostilityFrom(-80, F, C) === 0);
check('at/above ceiling -> 1.00', hostilityFrom(C, F, C) === 1 && hostilityFrom(-5, F, C) === 1);
check('midpoint -> 0.50', near(hostilityFrom((F + C) / 2, F, C), 0.5));
check('quarter point -> 0.25', near(hostilityFrom(F + (C - F) * 0.25, F, C), 0.25));
check('degenerate window: below -> 0, at/above -> 1, never NaN',
      hostilityFrom(-30, -20, -20) === 0 && hostilityFrom(-20, -20, -20) === 1 &&
      hostilityFrom(-5, -20, -20) === 1 && !Number.isNaN(hostilityFrom(NaN, F, C)),
      `below=${hostilityFrom(-30,-20,-20)} at=${hostilityFrom(-20,-20,-20)} above=${hostilityFrom(-5,-20,-20)}`);
check('inverted window (ceiling below floor) cannot produce NaN',
      !Number.isNaN(hostilityFrom(-30, -20, -40)));

console.log('--- sensitivity presets (six steps, cycled by one button) ---');
store = {};
{
  const m = new MicMeter();
  check('defaults to prior-art window', near(m.floor, DEFAULT_CAL.floor) && near(m.ceiling, DEFAULT_CAL.ceiling),
        `${m.floor} .. ${m.ceiling}`);
  const normalCeil = m.ceiling;
  m.setPreset('high');
  check('high preset LOWERS the ceiling', m.ceiling < normalCeil,
        `${normalCeil.toFixed(1)} -> ${m.ceiling.toFixed(1)}`);
  m.db = -34;
  const hHigh = m.hostility;
  m.setPreset('normal');
  const hNorm = m.hostility;
  check('same input is hotter on high than normal', hHigh > hNorm,
        `high=${hHigh.toFixed(2)} normal=${hNorm.toFixed(2)}`);
  check('six settings exist and OFF is the first of them', Object.keys(PRESETS).length === 6 &&
        PRESET_ORDER.length === 6 && PRESET_ORDER[0] === 'off' && PRESET_ORDER[3] === 'normal',
        PRESET_ORDER.join('/'));
  check('the listening steps run least to most sensitive (ceiling falls monotonically)',
        PRESET_ORDER.slice(1).every((n, i) => i === 0 ||
          PRESETS[n].ceilingShift < PRESETS[PRESET_ORDER.slice(1)[i-1]].ceilingShift),
        PRESET_ORDER.map(n => PRESETS[n].ceilingShift).join(','));
  check('two settings sit below normal and two above',
        PRESET_ORDER.slice(1, 3).every(n => PRESETS[n].ceilingShift > 0) &&
        PRESET_ORDER.slice(4).every(n => PRESETS[n].ceilingShift < 0));
  check('min is now far harder to trigger than it was (a loud room was pinning it)',
        PRESETS.min.ceilingShift >= 24, `+${PRESETS.min.ceilingShift} dB`);
  check('ESP32 2048 -> normal', presetFromToggle(2048) === 'normal');
  check('ESP32 4095 -> high', presetFromToggle(4095) === 'high');
  check('unknown toggle values still resolve', presetFromToggle(9) === 'normal' && presetFromToggle(3500) === 'high');
  m.applyEsp32Toggle(4095);
  check('applyEsp32Toggle switches preset', m.preset === 'high');
  m.setFineDb(-6);
  check('fine trim is screen-only and further lowers ceiling', m.ceiling < normalCeil - 6);
  m.setFineDb(-999);
  check('fine trim is clamped', m.fineDb === -24, String(m.fineDb));
  m.setFineDb(0); m.setPreset('normal');
  check('one press steps one preset and STAYS there', m.cyclePreset(1) === 'high' && m.preset === 'high');
  check('the next press steps again, it does not toggle back', m.cyclePreset(1) === 'max' && m.preset === 'max');
  check('the top wraps round to OFF', m.cyclePreset(1) === 'off');
  check('stepping down works too', m.cyclePreset(-1) === 'max' && m.cyclePreset(-1) === 'high');
  check('six presses return to where they started',
        (() => { const start = m.preset; for (let i = 0; i < 6; i++) m.cyclePreset(1); return m.preset === start; })());
  check('presetIndex tracks the position', (m.setPreset('off'), m.presetIndex === 0) && (m.setPreset('max'), m.presetIndex === 5));
  m.setPreset('normal');
  check('a cycled preset persists', (() => { m.cyclePreset(1); const m2 = new MicMeter(); return m2.preset === 'high'; })());
  check('ceiling can never cross the floor',
        (() => { m.cal = { floor: -40, ceiling: -39 }; m.setFineDb(-24); return m.ceiling > m.floor; })());
}

console.log('--- calibration ---');
{
  store = {};
  const m = new MicMeter();
  m.state = MIC.LIVE;
  m.analyser = { getFloatTimeDomainData(){}, fftSize: 8 };
  m._buf = new Float32Array(8);
  // Fake a quiet room at about -62 dBFS with one cough at -20.
  let n = 0;
  m.sample = function (now) {
    this.db = (n++ === 7) ? -20 : -62 + (Math.random() - 0.5);
    if (this.calibrating) this._calSamples.push(this.db);
    return this.db;
  };
  const ticks = [];
  const p = m.calibrate(600, left => ticks.push(left));
  const pump = setInterval(() => m.sample(0), 10);
  const res = await p;
  clearInterval(pump);
  check('calibration succeeded', res.ok === true, JSON.stringify(res).slice(0, 110));
  check('countdown ticked', ticks.length >= 3, `${ticks.length} ticks, first ${Math.round(ticks[0])} ms`);
  check('floor sits just above measured ambient', res.floor > res.ambient && res.floor < res.ambient + 6,
        `ambient=${res.ambient.toFixed(1)} floor=${res.floor.toFixed(1)}`);
  check('a single cough did not drag the floor up', res.floor < -50,
        `floor=${res.floor.toFixed(1)}`);
  check('ceiling is a fixed span above the floor', near(res.ceiling - res.floor, CAL_SPAN_DB),
        `span=${(res.ceiling - res.floor).toFixed(1)}`);
  check('ceiling stays reachable', res.ceiling <= CEILING_MAX_DB + 1e-9,
        `ceiling=${res.ceiling.toFixed(1)}`);
  check('quiet room now yields hostility 0', hostilityFrom(-62, res.floor, res.ceiling) === 0);
  check('a shout now yields hostility 1', hostilityFrom(-20, res.floor, res.ceiling) === 1);

  check('calibration persisted', !!store['jz.mic.cal.v1']);
  const m2 = new MicMeter();
  check('reloaded meter restores the calibrated window',
        near(m2.floor, res.floor) && near(m2.cal.ceiling, res.ceiling),
        `${m2.floor.toFixed(1)} .. ${m2.cal.ceiling.toFixed(1)}`);
  m2.setPreset('high'); m2.setFineDb(-3);
  const m3 = new MicMeter();
  check('preset and fine trim persist too', m3.preset === 'high' && m3.fineDb === -3);
  m3.resetCal();
  const m4 = new MicMeter();
  check('reset returns to defaults', near(m4.floor, DEFAULT_CAL.floor) && m4.preset === 'normal');
}

console.log('--- calibration in a NOISY room (the hackathon-floor case) ---');
{
  store = {};
  const m = new MicMeter();
  m.state = MIC.LIVE;
  m.analyser = { getFloatTimeDomainData(){}, fftSize: 8 };
  m._buf = new Float32Array(8);
  // Busy room: ambient around -32 dBFS, the level that produced an unreachable
  // -1.0 dBFS ceiling on the real device.
  m.sample = function () {
    this.db = -32 + (Math.random() - 0.5) * 2;
    if (this.calibrating) this._calSamples.push(this.db);
    return this.db;
  };
  const p = m.calibrate(500, () => {});
  const pump = setInterval(() => m.sample(0), 10);
  const res = await p;
  clearInterval(pump);
  check('noisy calibration succeeded', res.ok === true);
  check('ceiling is capped at a reachable level', res.ceiling <= CEILING_MAX_DB + 1e-9,
        `ceiling=${res.ceiling.toFixed(1)} dBFS (was -1.0 before the cap)`);
  check('the cap was reported', res.cappedCeiling === true);
  check('a usable span survives the cap', res.ceiling - res.floor >= MIN_SPAN_DB - 1e-9,
        `span=${(res.ceiling - res.floor).toFixed(1)} dB`);
  check('a shout still reaches 1.00', hostilityFrom(-6, res.floor, res.ceiling) === 1);
  check('room tone still reads 0.00', hostilityFrom(-33, res.floor, res.ceiling) === 0,
        `h(-33)=${hostilityFrom(-33, res.floor, res.ceiling).toFixed(2)}`);
  check('speech in between is graded, not binary',
        hostilityFrom((res.floor + res.ceiling) / 2, res.floor, res.ceiling) > 0.4);
}

console.log('--- a stale/unreachable stored window is repaired on load ---');
{
  store = { 'jz.mic.cal.v1': JSON.stringify({ floor: -28.7, ceiling: -1.0,
            preset: 'normal', fineDb: 0 }) };      // what the device actually had
  const m = new MicMeter();
  check('stored unreachable ceiling is clamped on load', m.cal.ceiling <= CEILING_MAX_DB + 1e-9,
        `-1.0 -> ${m.cal.ceiling.toFixed(1)} dBFS`);
  check('span preserved after clamping', m.cal.ceiling - m.cal.floor >= MIN_SPAN_DB - 1e-9,
        `span=${(m.cal.ceiling - m.cal.floor).toFixed(1)} dB`);
  check('a shout now reaches 1.00 with the repaired window',
        hostilityFrom(-6, m.floor, m.ceiling) === 1);
}

console.log('--- failure states are distinguishable from silence ---');
{
  const m = new MicMeter();
  check('idle is not "live"', m.live === false && m.statusText() === 'idle');
  m.state = MIC.DENIED;
  check('denied reports DENIED, not a level', m.statusText() === 'DENIED');
  m.state = MIC.LOST;
  check('lost reports LOST', m.statusText() === 'LOST');
  m.state = MIC.LIVE;
  m.track = { readyState: 'ended' };
  m.analyser = { getFloatTimeDomainData(){} }; m._buf = new Float32Array(4);
  m.sample(0);
  check('a dead track flips to LOST rather than reading silence', m.state === MIC.LOST, m.error);
  const m5 = new MicMeter();
  m5.state = MIC.LIVE;
  m5.analyser = { getFloatTimeDomainData(){ throw new Error('boom'); } };
  m5._buf = new Float32Array(4);
  const before = m5.db;
  check('a throwing read does not propagate', (m5.sample(0), true) && m5.state === MIC.LOST);
}

console.log('--- rolling peak ---');
{
  const m = new MicMeter({ peakWindowMs: 2000 });
  m.state = MIC.LIVE;
  m._buf = new Float32Array(4);
  let level = 0.0001;
  m.analyser = { getFloatTimeDomainData: b => b.fill(level) };
  m.sample(0);
  level = 0.3; m.sample(100);                    // a shout
  const peakAfterShout = m.peak;
  level = 0.0001;
  for (let t = 200; t <= 1900; t += 100) m.sample(t);
  check('peak survives after the shout ends', near(m.peak, peakAfterShout, 0.01),
        `peak=${m.peak.toFixed(1)} dBFS, live=${m.db.toFixed(1)}`);
  m.sample(2600);
  check('peak expires after the window', m.peak < peakAfterShout - 10,
        `peak=${m.peak.toFixed(1)}`);
}

console.log('--- bursty noise must still be able to turn the room over ---');
store = {};
{
  // Driven through sample() with a fake analyser, exactly like the rolling
  // peak test above: setting .db by hand would not fill the peak buffer.
  const m = new MicMeter({ peakWindowMs: 2000 });
  m.state = MIC.LIVE;
  m._buf = new Float32Array(4);
  let level = 0.0002;                                   // room tone, under the floor
  m.analyser = { getFloatTimeDomainData: b => b.fill(level) };
  m.setPreset('normal');
  m.sample(0);
  check('quiet room is not hostile on either measure',
        m.hostility === 0 && m.hostilityPeak === 0);

  level = 0.35; m.sample(100);                          // one burst of blowing
  check('the burst is hostile', m.hostility > 0.9, m.hostility.toFixed(2));

  level = 0.0002;                                       // the gap between breaths
  for (let t = 200; t <= 1600; t += 100) m.sample(t);
  check('the instantaneous value collapses to zero between bursts',
        m.hostility === 0, String(m.hostility));
  check('but the PEAK still remembers it, so a 2 s dwell can accumulate',
        m.hostilityPeak > 0.9, m.hostilityPeak.toFixed(2));

  // ...and it does not remember forever, or the room could never calm down.
  m.sample(2700);
  check('once the window passes, the peak forgets and the room can go calm',
        m.hostilityPeak === 0, String(m.hostilityPeak));
  check('the peak window is at least as long as the room dwell, or bursts never add up',
        m.peakWindowMs >= MOOD_RULE_DWELL, `${m.peakWindowMs}ms vs ${MOOD_RULE_DWELL}ms`);

  level = 0.35; m.sample(2800);
  m.setPreset('off');
  check('switched off, the peak measure reads silence too -- a remembered shout '
      + 'must not keep the room hostile after the mic is off',
        m.hostilityPeak === OFF_HOSTILITY && m.hostilityPeak === 0);
  m.setPreset('normal');
  m.suppress(1000);
  check('and while the game is talking it reads zero, not the remembered peak',
        m.hostilityPeak === 0);
}

console.log('--- OFF is a stop, not just the quietest setting ---');
store = {};
{
  const m = new MicMeter();
  m.setPreset('max'); m.db = -10;
  check('listening, a loud room is hostile', m.listening && m.hostility > 0.9);
  m.setPreset('off');
  check('switched off, the meter stops reporting the room',
        !m.listening && m.hostility === OFF_HOSTILITY, String(m.hostility));
  m.db = -100;
  const quiet = m.hostility;
  m.db = 0;
  check('and nothing the room does moves it, however loud', m.hostility === quiet);
  // A value BETWEEN the thresholds argues for neither mood, so the room would
  // keep whatever it had when OFF was pressed -- including hostile, forever.
  // Off has to mean silence or the switch does nothing you can predict.
  check('OFF reads as silence, not as a frozen middle value',
        OFF_HOSTILITY === 0, String(OFF_HOSTILITY));
  check('so it is below the calm threshold and the room WILL settle down',
        OFF_HOSTILITY <= MOOD_RULE_CALM_BELOW,
        `${OFF_HOSTILITY} vs calmBelow ${MOOD_RULE_CALM_BELOW}`);
  m.setPreset('normal');
  check('turning it back on resumes metering the room', m.listening && m.hostility === 1);
  m.setPreset('off');
  m.suppress(1000);
  check('a muted-for-dialogue window still reads zero, not the off value',
        m.suppressed && m.hostility === 0);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all mic metering contracts hold');
process.exit(failures ? 1 : 0);

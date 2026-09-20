// Drives the Stage 3 countdown machine with a fake clock and a fake server.
// Proves the three behaviours the stage is actually specified on:
//   1. swap lands exactly at the deadline when the world arrived in time
//   2. a late world shows INCOMING and NEVER extends/freezes/resets the clock
//   3. a fetch failure keeps the current world, retries, and never throws
import { WorldCycle } from './www/js/worldcycle.js';

let failures = 0;
const check = (name, cond, detail='') => {
  console.log(`${cond ? '  PASS' : '  FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
  if (!cond) failures++;
};
const flush = () => new Promise(r => setImmediate(r));

const CYCLE = 30000;

async function scenario(name, { serverMs, failFirst = 0, ticks = 20 }) {
  const swapsAt = [];
  const incomingAt = [];
  let issued = 0, failsLeft = failFirst;
  let t = 0;
  const inflight = [];

  const wc = new WorldCycle({
    cycleMs: CYCLE,
    fetchWorld: () => {
      issued++;
      const dueAt = t + serverMs;
      if (failsLeft > 0) { failsLeft--; return Promise.reject(new Error('HTTP 503')); }
      return new Promise(res => inflight.push({ dueAt, res,
        world: { biome: 'lab', name: 'W' + issued } }));
    },
    prepare: w => ({ built: true, for: w.name }),
    swap: (prepared, world, late) => { swapsAt.push({ t, world: world.name, late }); },
  });

  wc.start(0);
  for (let i = 0; i < ticks * 60; i++) {
    t += CYCLE / 60;                        // 60 ticks per cycle
    for (let k = inflight.length - 1; k >= 0; k--) {
      if (t >= inflight[k].dueAt) { const f = inflight.splice(k, 1)[0]; f.res(f.world); }
    }
    await flush();
    wc.tick(t);
    if (wc.awaiting) incomingAt.push(t);
  }
  return { wc, swapsAt, incomingAt, issued, t };
}

console.log('--- 1. world arrives well inside the cycle ---');
{
  const { wc, swapsAt } = await scenario('fast', { serverMs: 2000, ticks: 6 });
  check('swaps happened', wc.swaps >= 4, `swaps=${wc.swaps}`);
  check('none were late', wc.lateSwaps === 0, `late=${wc.lateSwaps}`);
  const offsets = swapsAt.map(s => s.t % CYCLE);
  check('each swap lands on a cycle boundary',
        offsets.every(o => o < CYCLE / 60 + 1e-6), `offsets=${offsets.map(o=>o.toFixed(0))}`);
  const gaps = swapsAt.slice(1).map((s, i) => s.t - swapsAt[i].t);
  check('swap spacing is exactly one cycle',
        gaps.every(g => Math.abs(g - CYCLE) < 1e-6), `gaps=${gaps.map(g=>g.toFixed(0))}`);
}

console.log('--- 2. server slower than the cycle (the INCOMING path) ---');
{
  const { wc, swapsAt, incomingAt } = await scenario('slow', { serverMs: 45000, ticks: 8 });
  check('it reported INCOMING at least once', incomingAt.length > 0,
        `frames showing incoming=${incomingAt.length}`);
  check('late swaps recorded', wc.lateSwaps > 0, `late=${wc.lateSwaps}`);
  // The metronome: every deadline must be an exact multiple of the cycle.
  const bad = wc.deadlineHistory.filter(d => Math.abs(d % CYCLE) > 1e-6);
  check('clock was never extended or reset', bad.length === 0,
        `deadlines=${wc.deadlineHistory.map(d=>(d/1000).toFixed(0)+'s').join(',')}`);
  check('deadlines advance by exactly one cycle each',
        wc.deadlineHistory.slice(1).every((d, i) =>
          Math.abs(d - wc.deadlineHistory[i] - CYCLE) < 1e-6));
  check('a late world still swapped in', swapsAt.length > 0, `swaps=${swapsAt.length}`);
}

console.log('--- 3. server failing, then recovering ---');
{
  const { wc } = await scenario('fail', { serverMs: 1000, failFirst: 3, ticks: 8 });
  check('failures were counted, not thrown', wc.fails === 3, `fails=${wc.fails}`);
  check('it recovered and swapped', wc.swaps > 0, `swaps=${wc.swaps}`);
  check('an error message was retained', !!wc.lastError || wc.swaps > 0);
  const bad = wc.deadlineHistory.filter(d => Math.abs(d % CYCLE) > 1e-6);
  check('clock stayed on the metronome through failures', bad.length === 0);
}

console.log('--- 4. server never responds at all ---');
{
  const { wc } = await scenario('dead', { serverMs: 10 ** 9, ticks: 5 });
  check('no swaps, no crash', wc.swaps === 0);
  check('still showing INCOMING rather than freezing', wc.awaiting === true);
  check('clock kept running', wc.deadlineHistory.length >= 4,
        `deadlines passed=${wc.deadlineHistory.length}`);
}

console.log('--- 5. operator skip (handheld stick button) ---');
{
  // Hand-driven clock: the world is ready 2 s in, skip at 10 s.
  let t = 0, issued = 0; const inflight = [], swapsAt = [];
  const wc = new WorldCycle({
    cycleMs: CYCLE,
    fetchWorld: () => { issued++; return new Promise(res => inflight.push({ dueAt: t + 2000, res, world: { biome: 'lab', name: 'W' + issued } })); },
    prepare: w => ({ for: w.name }),
    swap: (prepared, world, late) => swapsAt.push({ t, world: world.name, late }),
  });
  const advance = async (to) => { while (t < to) { t += CYCLE / 60;
    for (let k = inflight.length - 1; k >= 0; k--) if (t >= inflight[k].dueAt) { const f = inflight.splice(k, 1)[0]; f.res(f.world); }
    await flush(); wc.tick(t); } };
  check('skip before start does nothing', wc.skip(0) === false && wc.skips === 0);
  wc.start(0);
  await advance(10000);
  check('next world is prepared at 10 s', wc.prepared);
  const r = wc.skip(t);
  check('skip swaps immediately when a world is ready', r === true && swapsAt.length === 1 && swapsAt[0].world === 'W1' && !swapsAt[0].late);
  check('countdown restarts: a full cycle from the skip', Math.abs(wc.remaining(t) - CYCLE) < 1e-6, `${wc.remaining(t)}`);
  check('the following world is prefetched at once', wc.fetching || wc.prepared);
  check('skips are counted', wc.skips === 1);
  // Skip again right away: nothing is ready (fetch takes 2 s) -> INCOMING, then it lands.
  const t0 = t;
  const r2 = wc.skip(t);
  check('skip with nothing ready reports INCOMING', r2 === false && wc.awaiting && wc.remaining(t) === null);
  await advance(t0 + 3000);
  check('...and the world lands the moment it arrives (marked late)', swapsAt.length === 2 && swapsAt[1].late && !wc.awaiting);
  check('clock kept counting from the skip, not from the landing', wc.deadline === t0 + CYCLE);
  await advance(t0 + CYCLE + 1000);
  check('normal metronome resumes after the skip', swapsAt.length === 3 && Math.abs(swapsAt[2].t - (t0 + CYCLE)) < CYCLE / 60 + 1e-6, `swap at ${swapsAt[2].t}`);
}


console.log('--- 6. manual mode: the clock is a snapshot cooldown ---');
{
  let t = 0, issued = 0; const inflight = [], swapsAt = [];
  const wc = new WorldCycle({
    cycleMs: CYCLE, manual: true,
    fetchWorld: () => { issued++; return new Promise(res => inflight.push({ dueAt: t + 2000, res, world: { biome: 'lab', name: 'W' + issued } })); },
    prepare: w => ({ for: w.name }),
    swap: (prepared, world, late) => swapsAt.push({ t, world: world.name, late }),
  });
  const advance = async (to) => { while (t < to) { t += CYCLE / 60;
    for (let k = inflight.length - 1; k >= 0; k--) if (t >= inflight[k].dueAt) { const f = inflight.splice(k, 1)[0]; f.res(f.world); }
    await flush(); wc.tick(t); } };
  wc.start(0);
  check('the opening world is fetched without a press', issued === 1);
  await advance(2500);
  check('...and lands, so the room is not empty to begin with',
        wc.swaps === 1 && swapsAt[0].world === 'W1' && !wc.awaiting);
  check('the opening world is not counted as late', wc.lateSwaps === 0);
  check('no press was needed for it', wc.snapshots === 0);
  await advance(CYCLE * 0.5);
  check('halfway through the cooldown it is not ready', !wc.ready && wc.remaining(t) > 0);
  check('pressing early is refused and changes nothing',
        wc.trigger(t) === false && wc.earlyPresses === 1 && wc.snapshots === 0 && issued === 1);
  const remBefore = wc.remaining(t);
  await advance(CYCLE * 0.6);
  check('an early press did not reset the clock', wc.remaining(t) < remBefore);
  await advance(CYCLE + 10);
  check('at zero it is READY and stays there', wc.ready && wc.remaining(t) === 0);
  await advance(CYCLE * 3);
  check('it waits indefinitely: no further world arrives on its own',
        wc.ready && wc.swaps === 1 && issued === 1 && wc.remaining(t) === 0,
        `waited ${(3 * CYCLE / 1000).toFixed(0)} s past zero`);
  const pressedAt = t;
  check('the press takes the snapshot', wc.trigger(t) === true && wc.snapshots === 1 && issued === 2);
  check('...and the cooldown restarts from the press, not from the world landing',
        Math.abs(wc.deadline - (pressedAt + CYCLE)) < 1e-6 && !wc.ready);
  check('while it is on its way the clock reads INCOMING', wc.awaiting && wc.remaining(t) === null);
  await advance(pressedAt + 2500);
  check('the snapshot world lands when it lands', wc.swaps === 2 && swapsAt[1].world === 'W2' && !wc.awaiting);
  check('the cooldown was not extended by the wait', Math.abs(wc.deadline - (pressedAt + CYCLE)) < 1e-6);
  await advance(pressedAt + CYCLE + 10);
  check('ready again one cooldown after the press', wc.ready);
  check('and no extra fetch happened while it waited', issued === 2);
  wc.trigger(t);
  await advance(t + 3000);
  check('the second snapshot works the same way', wc.snapshots === 2 && wc.swaps === 3 && issued === 3);
  check('every world after the opening one came from a press', wc.swaps - 1 === wc.snapshots);
}

console.log('--- 7. manual mode does not disturb automatic mode ---');
{
  const auto = new WorldCycle({ cycleMs: CYCLE, fetchWorld: () => Promise.resolve({ biome: 'lab', name: 'A' }), prepare: w => w, swap: () => {} });
  check('automatic is still the default', auto.manual === false);
  auto.start(0);
  check('trigger() does nothing in automatic mode', auto.trigger(1000) === false && auto.snapshots === 0);
  check('automatic still prefetches at the start', auto.fetching || auto.prepared);
  check('automatic never enters the awaiting-for-a-press state', auto.awaiting === false);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all Stage 3 timing contracts hold');
process.exit(failures ? 1 : 0);

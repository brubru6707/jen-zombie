// Hearts, the five-second mercy window, the room turning over with the noise.
// All of it is rules rather than rendering, so it is checked here instead of
// by standing in a room being hit.
import { Player, PLAYER, Score, POINTS } from './www/js/player.js';
import { Population, MOOD, MOOD_RULE } from './www/js/population.js';
import { Mob, ATTACK_MS, LEAVE_DISTANCE, CALM_DISTANCE, STOP_DISTANCE } from './www/js/mobs.js';

let failures = 0;
const check = (n, c, d = '') => { console.log(`${c ? '  PASS' : '  FAIL'}  ${n}${d ? '  ' + d : ''}`); if (!c) failures++; };

console.log('--- three hearts ---');
{
  const p = new Player();
  check('starts on three', p.hearts === 3 && p.maxHearts === 3 && p.alive);
  check('a hit costs one', p.damage(1000).hearts === 2);
  check('it is recorded as a hit, not a block', p.hits === 1 && p.blocked === 0);
  check('and you are not overrun yet', p.alive && p.hearts === 2);
}

console.log('--- five seconds of mercy ---');
{
  const p = new Player();
  p.damage(1000);
  check('immune straight after a hit', p.immune(1000) && p.immune(1200));
  const r = p.damage(1200);
  check('a second blow inside the window costs nothing',
        !r.hit && r.blocked && p.hearts === 2 && p.blocked === 1);
  let blocked = 0;
  for (let t = 1000; t < 6000; t += 100) if (p.damage(t).blocked) blocked++;
  check('a whole crowd swinging for five seconds costs nothing', p.hearts === 2 && blocked > 40,
        `${blocked} blows absorbed`);
  check('still immune at 4.9 s', p.immune(5900));
  check('the window is exactly five seconds', !p.immune(6001) && PLAYER.immunityMs === 5000);
  check('the next hit lands once it lapses', p.damage(6100).hit && p.hearts === 1);
  check('...and starts a fresh window', p.immune(6200) && !p.damage(6200).hit);
}

console.log('--- the red wash ---');
{
  const p = new Player();
  p.damage(1000);
  check('strongest at the moment of impact', p.flash(1000) === 1);
  check('halfway through it is halfway gone', Math.abs(p.flash(1000 + PLAYER.flashMs / 2) - 0.5) < 1e-9);
  check('it is over well before the immunity is', p.flash(1000 + PLAYER.flashMs + 1) === 0 && p.immune(1700));
  check('no tint when nothing has happened', new Player().flash(5000) === 0);
}

console.log('--- being overrun, and coming back ---');
{
  const p = new Player();
  let now = 0;
  for (const t of [1000, 7000, 13000]) { now = t; p.damage(t); }
  check('three hits empties the hearts', p.hearts === 0 && !p.alive && p.overruns === 1);
  check('nothing can hit you once you are down', !p.damage(14000).hit);
  check('you do not come back immediately', p.tick(14000) === false && !p.alive);
  check('you come back after the revive delay', p.tick(now + PLAYER.reviveMs + 1) === true);
  check('...on full hearts', p.hearts === 3 && p.alive);
  check('...and briefly untouchable, so you are not instantly overrun again',
        p.immune(now + PLAYER.reviveMs + 2));
  check('tick does nothing while alive', p.tick(99999) === false);
}

console.log('--- the settle-in grace ---');
{
  const p = new Player();
  p.reset(10000, 3000);
  check('a mob swinging as you arrive cannot take a heart', !p.damage(11000).hit && p.hearts === 3);
  check('but it can once you have had a look round', p.damage(13500).hit && p.hearts === 2);
}

console.log('--- hostile mobs attack on a cadence, calm ones never do ---');
{
  const hostile = new Mob('walker', 0, -2, 0.5, 'street', 0, 'hostile');
  check('a mob that has not arrived does not swing', hostile.attack(1000) === false);
  hostile.arrived = true;
  check('an arrived mob swings', hostile.attack(1000) === true && hostile.attacks === 1);
  check('and not again immediately', hostile.attack(1100) === false && hostile.attack(1000 + ATTACK_MS - 1) === false);
  check('it swings again once its cadence is up', hostile.attack(1000 + ATTACK_MS) === true);
  const calm = new Mob('walker', 0, -2, 0.5, 'street', 0, 'calm');
  calm.arrived = true;
  check('a calm mob never swings', calm.attack(1000) === false && calm.attack(99999) === false);
  check('a calm mob keeps its distance', calm.stopAt === CALM_DISTANCE && calm.stopAt > STOP_DISTANCE);
  hostile.leave();
  check('one on its way out stops swinging', hostile.attack(50000) === false);
  const dyingMob = new Mob('walker', 0, -2, 0.5, 'street', 0, 'hostile');
  dyingMob.arrived = true; dyingMob.hit(99);
  check('a dying mob stops swinging', dyingMob.attack(90000) === false);
}

console.log('--- leaving the room ---');
{
  const m = new Mob('walker', 0, -3, 0.6, 'street', 0, 'hostile');
  m.arrived = true;
  check('leave() is accepted', m.leave() === true && m.leaving && !m.arrived);
  let steps = 0;
  while (!m.dead && steps < 3000) { m.step(1 / 60, 0, 0, steps * 16.7, [m]); steps++; }
  check('it walks away and eventually stops existing', m.dead === true, `${(steps / 60).toFixed(1)} s`);
  check('it left outward, not through the player',
        Math.hypot(m.group.position.x, m.group.position.z) >= LEAVE_DISTANCE - 1e-6);
  const dead = new Mob('walker', 0, -3, 0.6, 'street', 0, 'hostile');
  dead.hit(99);
  check('a dying mob cannot be told to leave', dead.leave() === false);
}

console.log('--- the room follows the noise ---');
{
  const p = new Population();
  check('it starts hostile', p.mood === MOOD.HOSTILE);
  check('a quiet instant is not enough', p.update(500, 0.0) === null && p.mood === MOOD.HOSTILE);
  check('sustained quiet turns the room over', p.update(MOOD_RULE.dwellMs, 0.0) === MOOD.CALM && p.mood === MOOD.CALM);
  check('it does not keep announcing it', p.update(5000, 0.0) === null);
  check('one shout does not undo it', p.update(200, 0.9) === null && p.mood === MOOD.CALM);
  check('sustained noise sends the calm ones away', p.update(MOOD_RULE.dwellMs, 0.9) === MOOD.HOSTILE);
  check('it counted both turnovers', p.changes === 2);

  const q = new Population();
  q.update(9000, 0.0);                        // -> calm
  let flips = 0;
  for (let i = 0; i < 400; i++) {             // hovering right on the boundary
    const h = MOOD_RULE.calmBelow + 0.02 * Math.sin(i / 3);
    if (q.update(16.7, h)) flips++;
  }
  check('hovering on the threshold does not flip-flop the room', flips === 0, `${flips} changes in 6.7 s`);

  const r = new Population();
  check('the dead zone between the thresholds holds the room as it is',
        r.update(9000, (MOOD_RULE.calmBelow + MOOD_RULE.hostileAbove) / 2) === null);
  check('the two thresholds are far apart, deliberately',
        MOOD_RULE.hostileAbove - MOOD_RULE.calmBelow > 0.2);
  check('a NaN hostility (dead mic) is treated as silence, not as noise',
        (() => { const z = new Population(); return z.update(9000, NaN) === MOOD.CALM; })());
  const t = new Population();
  t.update(1000, 0.0);
  check('progress shows how close the turnover is', t.progress > 0.4 && t.progress < 0.6,
        t.progress.toFixed(2));
}

console.log('--- points ---');
{
  const sc = new Score();
  check('starts at zero', sc.value === 0 && sc.text === '0000');
  check('a walker is worth the base rate', sc.kill('walker') === POINTS.walker);
  check('a runner is worth more', (() => { const s2 = new Score(); return s2.kill('runner') > POINTS.walker; })());
  check('a brute is worth the most of the three',
        POINTS.brute > POINTS.runner && POINTS.runner > POINTS.walker);
  check('an unknown kind still scores rather than breaking',
        (() => { const s2 = new Score(); return s2.kill('gremlin') === POINTS.walker; })());
  check('a snapshot pays more than any single kill', POINTS.snapshot > POINTS.brute);
  const before = sc.value;
  check('taking a snapshot scores it', sc.snapshot() === before + POINTS.snapshot && sc.snapshots === 1);
  check('taking a hit costs points', sc.hit() < before + POINTS.snapshot && sc.hitsTaken === 1);
  const poor = new Score();
  poor.hit(); poor.hit(); poor.hit();
  check('but the score never goes negative', poor.value === 0, String(poor.value));
  check('kills and hits are both counted', poor.hitsTaken === 3 && poor.kills === 0);
  const best = new Score();
  best.kill('brute'); best.kill('brute');
  const peak = best.value;
  best.hit(); best.hit(); best.hit();
  check('the best score is remembered after losing points', best.best === peak && best.value < peak);
  check('the readout is padded so the HUD does not jiggle',
        new Score().text === '0000' && (() => { const s2 = new Score(); s2.snapshot(); return s2.text.length === 4; })());
  best.reset();
  check('a new run starts clean', best.value === 0 && best.kills === 0 && best.best === 0);
  const st = new Score(); st.kill('brute'); st.snapshot(); st.hit();
  check('status reports everything the dashboard shows',
        st.status().value === Math.max(0, POINTS.brute + POINTS.snapshot + POINTS.hit) &&
        st.status().kills === 1 && st.status().snapshots === 1 && st.status().hitsTaken === 1,
        JSON.stringify(st.status()));
}

console.log('--- pinning the room ---');
{
  const p = new Population();
  check('pinning calm turns it over at once', p.pin(MOOD.CALM) === MOOD.CALM && p.mood === MOOD.CALM);
  check('pinning what it already is changes nothing', p.pin(MOOD.CALM) === null);
  check('a pinned room ignores the microphone entirely',
        p.update(60000, 1.0) === null && p.mood === MOOD.CALM, 'a minute of shouting');
  check('unpinning hands it back to the microphone',
        p.pin(null) === null && p.update(MOOD_RULE.dwellMs, 1.0) === MOOD.HOSTILE);
  check('rubbish is not a pin', (() => { const q = new Population(); q.pin('banana'); return q.pinned === null; })());
  check('status says whether it is pinned', p.status().pinned === null && new Population().pin && true);
}

console.log('--- losing all three hearts costs 50, and the hearts come back ---');
{
  const p = new Player();
  const sc = new Score();
  p.reset(0, 0);
  for (let i = 0; i < 6; i++) sc.kill('brute');       // 150 banked
  check('a full run is banked first', sc.value === 150, String(sc.value));

  let now = 0, overrunAt = null;
  for (let h = 0; h < 3; h++) {
    now += PLAYER.immunityMs + 10;
    const r = p.damage(now);
    if (r.overrun) { sc.overrun(); overrunAt = now; } else sc.hit();
  }
  check('three hits empties the hearts', p.hearts === 0 && !p.alive);
  check('the first two hits cost 20 each and the death costs 50, not 70',
        sc.value === 150 - 20 - 20 - 50, `${sc.value} (expected ${150 - 90})`);
  check('the death is counted separately from ordinary hits',
        sc.overruns === 1 && sc.hitsTaken === 3);

  check('the hearts are NOT back immediately', !p.tick(overrunAt + 100) && !p.alive);
  const revived = p.tick(overrunAt + PLAYER.reviveMs + 10);
  check('after the revive delay all three hearts return',
        revived && p.alive && p.hearts === p.maxHearts, `hearts=${p.hearts}`);
  check('and the score survives the revive, minus the 50', sc.value === 60);

  // A death can never drive the score negative -- the chain settlement mints
  // from this number and must never be asked for a refund.
  const poor = new Score();
  poor.kill('walker');                                  // 10
  poor.overrun();
  check('a death with almost nothing banked floors at zero, never negative',
        poor.value === 0, String(poor.value));
  poor.overrun();
  check('and dying again at zero stays at zero', poor.value === 0);
  check('but the deaths are still counted', poor.overruns === 2);

  const fresh = new Score();
  fresh.overrun();
  fresh.reset();
  check('reset clears the death count too', fresh.overruns === 0 && fresh.value === 0);
  check('status reports overruns for the dashboard', new Score().status().overruns === 0);
  check('POINTS.overrun is the single source of the number', POINTS.overrun === -50);
}

console.log('--- the room cannot deadlock between mood and crowd ---');
{
  const pop = new Population({ mood: MOOD.CALM });
  // pin() RETURNS the new mood, and the caller must act on it. A caller that
  // ignores the return -- the debug console, a ?mood= URL -- leaves the mood
  // saying one thing while the crowd on screen is the other, and from then on
  // update() has nothing to report. This is the deadlock the page's
  // reconciler exists to break; these assertions pin the contract down.
  const returned = pop.pin(MOOD.HOSTILE);
  check('pinning returns the new mood, so it cannot be changed silently',
        returned === MOOD.HOSTILE);
  check('and the mood really did change', pop.mood === MOOD.HOSTILE);
  pop.pin(null);
  check('unpinning leaves the mood where the pin put it', pop.mood === MOOD.HOSTILE);
  check('and stops ignoring the microphone', pop.status().pinned === null);

  // Now the trap: maximum hostility reports NOTHING, because it already agrees.
  let out = null;
  for (let i = 0; i < 10; i++) out = out || pop.update(1000, 1.0);
  check('shouting at a room that already thinks it is hostile reports no change',
        out === null, String(out));

  // The other direction still works, so the reconciler is a safety net and not
  // a replacement for the rule.
  let calm = null;
  for (let i = 0; i < 10; i++) calm = calm || pop.update(1000, 0.0);
  check('going quiet still turns the room over normally', calm === MOOD.CALM);
}

console.log(failures ? `\n  ${failures} FAILURE(S)` : '\n  all player and room contracts hold');
process.exit(failures ? 1 : 0);

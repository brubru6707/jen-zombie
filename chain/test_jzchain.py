#!/usr/bin/env python3
"""
The settlement logic, with no network and no phone.

Everything here is about the two things that can actually hurt: settling the
WRONG NUMBER, and a failure that gets rendered as a success. The chain calls
are not mocked out of laziness -- they are excluded because an RPC test would
pass or fail on devnet's mood rather than on this code.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("JZ_SOLANA_RPC", "https://api.devnet.solana.com")
import jzchain as J

fails = 0


def check(name, cond, detail=""):
    global fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")
    if not cond:
        fails += 1


def tel(t, score, overruns=0, alive=True, **kw):
    """A telemetry sample shaped exactly like the Pi's /telemetry."""
    d = {"t": t, "player": {"hearts": 3, "alive": alive, "overruns": overruns,
                            "score": {"value": score, "best": score, "kills": 0,
                                      "snapshots": 0, "hitsTaken": 0}}}
    d.update(kw)
    return {"data": d}


print("--- a run is settled once, for what it actually earned ---")
{}
rt = J.RunTracker()
rt.observe(tel(0, 0), 1.0)
check("attaching at the start of a session claims nothing retroactively",
      rt.unsettled == 0 and rt.settled == 0)
rt.observe(tel(5000, 10), 2.0)
rt.observe(tel(9000, 35), 3.0)
check("points accumulate as unsettled while you are alive", rt.unsettled == 35, rt.unsettled)
out = rt.observe(tel(12000, 35, overruns=1, alive=False), 4.0)
check("death settles exactly the run's score, once", out == [(35, "death")], out)
check("and nothing is left outstanding", rt.unsettled == 0)
out = rt.observe(tel(13000, 35, overruns=1, alive=False), 5.0)
check("the same death does not settle twice", out == [])
out = rt.observe(tel(14000, 35, overruns=1, alive=True), 6.0)
check("reviving does not settle either", out == [])

print("--- a second death banks only what came after the first ---")
rt.observe(tel(20000, 95, overruns=1), 7.0)
out = rt.observe(tel(21000, 95, overruns=2), 8.0)
check("the delta, not the total", out == [(60, "death")], out)
check("the running total is right", rt.settled == 95)

print("--- a hit costs points, and the chain is never asked for a refund ---")
rt2 = J.RunTracker()
rt2.observe(tel(0, 0), 1.0)
rt2.observe(tel(1000, 50), 2.0)
out = rt2.observe(tel(2000, 30, overruns=1), 3.0)   # -20 from a hit, then death
check("settles the score as it stands after the penalty", out == [(30, "death")], out)
rt3 = J.RunTracker()
rt3.observe(tel(0, 0), 1.0)
rt3.observe(tel(1000, 40), 2.0)
rt3.observe(tel(2000, 40, overruns=1), 3.0)
rt3.observe(tel(3000, 20, overruns=1), 4.0)         # hits after settling
out = rt3.observe(tel(4000, 20, overruns=2), 5.0)
check("a score that falls BELOW what was already minted settles nothing",
      out == [] and rt3.unsettled == 0, f"unsettled={rt3.unsettled}")
check("and it never goes negative", rt3.unsettled >= 0)

print("--- a new session is a new run ---")
rt4 = J.RunTracker()
rt4.observe(tel(0, 0), 1.0)
rt4.observe(tel(8000, 70), 2.0)
out = rt4.observe(tel(120, 0), 3.0)                 # t jumped backwards: reload
check("the old session's outstanding points are flushed, not lost",
      out == [(70, "session ended")], out)
check("and the new session starts clean", rt4.settled == 0 and rt4.score == 0)
rt5 = J.RunTracker()
rt5.observe(tel(45000, 220), 1.0)                   # attached mid-run
check("attaching to a session already in progress does not claim its history",
      rt5.unsettled == 0, f"unsettled={rt5.unsettled}")

print("--- the phone going quiet does not strand the points ---")
rt6 = J.RunTracker()
rt6.observe(tel(0, 0), 100.0)
rt6.observe(tel(3000, 45), 101.0)
check("nothing is flushed while it is still posting", rt6.idle_flush(102.0) is None)
check("still nothing just before the timeout",
      rt6.idle_flush(101.0 + J.SESSION_IDLE_S - 1) is None)
flushed = rt6.idle_flush(101.0 + J.SESSION_IDLE_S + 1)
check("after the timeout the remainder is settled", flushed == (45, "session went quiet"), flushed)

print("--- rubbish in never means a wrong number out ---")
rt7 = J.RunTracker()
for junk in (None, {}, [], "nope", {"data": None}, {"data": {}},
             {"data": {"player": None}}, {"data": {"player": {"score": None}}},
             {"data": {"player": {"score": {"value": "many"}}}},
             {"data": {"t": "soon", "player": {"score": {"value": 10}}}}):
    check(f"survives {str(junk)[:34]!r}", rt7.observe(junk, 1.0) == [])
check("and nothing was invented from it", rt7.unsettled == 0)

print("--- what was minted is never confused with what was merely skipped ---")
rt8 = J.RunTracker()
rt8.observe(tel(50000, 525), 1.0)          # attached to a run already in progress
check("nothing is claimed from a run already under way", rt8.unsettled == 0)
check("and the skipped points are reported as skipped, not as settled",
      rt8.pre_existing == 525 and rt8.minted == 0,
      f"pre_existing={rt8.pre_existing} minted={rt8.minted}")
rt8.observe(tel(52000, 600), 2.0)
out = rt8.observe(tel(53000, 600, overruns=1), 3.0)
check("only the points earned after attaching are settled", out == [(75, "death")], out)
check("minted counts tokens, watermark counts the floor",
      rt8.minted == 75 and rt8.watermark == 600,
      f"minted={rt8.minted} watermark={rt8.watermark}")
rt9 = J.RunTracker()
rt9.observe(tel(0, 0), 1.0)
check("a run watched from the start has nothing predating it", rt9.pre_existing == 0)
rt9.observe(tel(1000, 40), 2.0)
rt9.take(40)   # the manual path still claims explicitly
check("and then minted is the whole score", rt9.minted == 40 and rt9.pre_existing == 0)
out9 = rt9.observe(tel(10, 0), 3.0)
check("a reload that restarts the clock is a NEW run, even seconds in",
      rt9.minted == 0 and rt9.runs_seen == 2, f"minted={rt9.minted} runs={rt9.runs_seen}")
check("and the run it replaced owed nothing, so nothing was flushed", out9 == [], out9)

rtA = J.RunTracker()
rtA.observe(tel(0, 0), 1.0)
rtA.observe(tel(900, 60), 2.0)
outA = rtA.observe(tel(20, 0), 3.0)      # reload while 60 points were outstanding
check("a reload mid-run flushes what was owed", outA == [(60, "session ended")], outA)
check("and the flush is NOT credited to the new run",
      rtA.minted == 0 and rtA.watermark == 0 and rtA.unsettled == 0,
      f"minted={rtA.minted} watermark={rtA.watermark}")

print("--- a failure is kept and shown as a failure ---")
tmp = tempfile.mkdtemp()
q = J.Queue(os.path.join(tmp, "q.json"))
item = q.add(120, "death")
check("a new settlement is pending, never assumed sent",
      item["state"] == "pending" and item["signature"] is None)
check("it is offered for work immediately", q.next_pending(time.time()) is item)
q.mark(item, state="failed", attempts=1, last_try=time.time(), error="429 rate limited")
t0 = time.time()
check("a failure is NOT dropped", len(q.snapshot()) == 1)
check("it is not disguised as success",
      q.totals()["settled_runs"] == 0 and q.totals()["failed"] == 1)
check("last_confirmed ignores it -- the dashboard cannot show a failure as the last tx",
      q.last_confirmed() is None)
check("it is not retried instantly", q.next_pending(t0 + 1) is None)
check("it is retried after the backoff", q.next_pending(t0 + 5) is item)
q.mark(item, state="failed", attempts=4, last_try=t0)
check("the backoff grows with the attempts", q.next_pending(t0 + 20) is None)
check("but is capped so it never gives up for good", q.next_pending(t0 + 130) is item)
q.mark(item, state="confirmed", signature="5" * 88, settled_at=time.time())
check("only a confirmed settlement counts", q.totals()["settled_points"] == 120)
check("and it is the one the dashboard links to", q.last_confirmed()["signature"] == "5" * 88)
check("a confirmed item is never retried", q.next_pending(t0 + 10_000) is None)

q2 = J.Queue(os.path.join(tmp, "q.json"))
check("the queue survives a restart, so a crash cannot lose a run",
      len(q2.snapshot()) == 1 and q2.snapshot()[0]["amount"] == 120)
check("and ids keep counting from where they were", q2.add(5, "x")["id"] == 2)

print("--- devnet, and nothing else ---")
check("solscan links carry the cluster and are devnet",
      "cluster=devnet" in J.solscan("abc") and J.solscan("abc").startswith("https://solscan.io/tx/"))
check("no signature means no link, not a broken one", J.solscan(None) is None)
check("the token has no decimals -- a score is whole points", J.DECIMALS == 0)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "jzchain.py")).read()
check("a non-devnet RPC is refused at startup rather than trusted",
      'REFUSING non-devnet RPC' in src)

print("--- the RPC client gives up quietly instead of raising ---")
r = J.Rpc(url="http://127.0.0.1:1/nope", attempts=2, timeout=1)
res, err = r.call("getHealth")
check("an unreachable endpoint returns an error, it does not throw",
      res is None and isinstance(err, str) and err)
check("and the failure is counted for the dashboard", r.failures == 1 and r.last_error)

print("--- the telemetry read cannot take the process down ---")
res, err = J.fetch_telemetry("https://127.0.0.1:1/telemetry", timeout=1)
check("an unreachable Pi is an error string, not an exception", res is None and err)

print()
print(f"  {fails} FAILURE(S)" if fails else "  all settlement contracts hold")
sys.exit(1 if fails else 0)

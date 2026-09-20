#!/usr/bin/env python3
"""
OPTIONAL, AND NOT APPLIED. Adds an on-chain card to the game's dashboard.

This is a separate script because it is the one part of the Solana work that
would touch `game/`, which the brief put off limits, AND because deploying it
restarts jzbridge -- which drops whatever session is live. Neither is a thing to
do unasked with judges in the building.

What it changes: ONE card appended to game/www/dashboard.html. It fetches the
chain service over CORS and fails silently to a dash if the service is not
running, so the dashboard is no worse off if Solana is down.

    python3 chain/add_to_dashboard.py --check     # show what it would do
    python3 chain/add_to_dashboard.py             # patch the file
    controller-bridge/deploy.sh                   # then deploy (restarts jzbridge)

--revert puts it back.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "game", "www", "dashboard.html")
API = os.environ.get("JZ_CHAIN_API", "http://localhost:8100/api")

MARK = "<!-- jz-chain-card -->"

CARD = """  <div class="card">
    <h2>ON CHAIN &middot; SOLANA DEVNET</h2>
    <div class="r"><span class="k">unsettled score</span><span class="v" id="c-unsettled">&mdash;</span></div>
    <div class="r"><span class="k">token balance</span><span class="v" id="c-balance">&mdash;</span></div>
    <div class="r"><span class="k">settled</span><span class="v" id="c-total">&mdash;</span></div>
    <div class="r"><span class="k">failed</span><span class="v" id="c-failed">&mdash;</span></div>
    <div class="r"><span class="k">last signature</span><span class="v" id="c-sig" style="font-size:11px">&mdash;</span></div>
    <div style="margin-top:8px"><a id="c-solscan" href="#" target="_blank" rel="noopener"
       style="display:block;padding:10px;border:1px solid #1f6feb;border-radius:10px;
              text-align:center;font-weight:700;text-decoration:none;color:#4aa3ff">OPEN IN SOLSCAN</a></div>
    <div class="ctl" style="margin-top:8px"><button id="c-settle">SETTLE NOW</button>
      <span class="v" id="c-msg" style="font-size:12px">&mdash;</span></div>
  </div>
"""

SCRIPT = """
// ---- on-chain settlement, read from the Mac-side service over CORS. --------
// Entirely optional: if the service is not running every field reads as a dash
// and nothing else on this page is affected.
const CHAIN_API = %r;
async function pollChain() {
  const set2 = (id, t, cls) => { const e = document.getElementById(id); if (!e) return;
                                 e.textContent = t; e.className = 'v' + (cls ? ' ' + cls : ''); };
  let d = null;
  try { d = await (await fetch(CHAIN_API, { cache: 'no-store' })).json(); }
  catch (e) { set2('c-unsettled', 'service not running', 'dim'); return; }
  try {
    set2('c-unsettled', String(d.run.unsettled), d.run.unsettled ? 'warn' : 'dim');
    set2('c-balance', d.token.balance === null ? '\\u2014' : `${d.token.balance} ${d.token.name}`, 'ok');
    set2('c-total', `${d.totals.settled_runs} runs, ${d.totals.settled_points} pts`, 'dim');
    set2('c-failed', String(d.totals.failed), d.totals.failed ? 'bad' : 'dim');
    set2('c-sig', d.last ? d.last.signature : '\\u2014', 'dim');
    const a = document.getElementById('c-solscan');
    if (a) { a.href = (d.last && d.last.url) || '#'; a.style.opacity = d.last ? 1 : 0.35; }
  } catch (e) { /* a shape change must not break the rest of the dashboard */ }
}
{
  const b = document.getElementById('c-settle');
  if (b) b.addEventListener('click', async () => {
    const m = document.getElementById('c-msg');
    try {
      const j = await (await fetch(CHAIN_API.replace(/\\/api$/, '/settle'), { method: 'POST' })).json();
      if (m) m.textContent = j.ok ? `queued ${j.amount}` : (j.error || 'nothing to settle');
    } catch (e) { if (m) m.textContent = 'service not running'; }
    pollChain();
  });
}
pollChain(); setInterval(pollChain, 2000);
""" % API


def find_anchor(s):
    """Just before the wide card that holds the model conversation."""
    m = re.search(r'\n  <div class="card wide">', s)
    return m


def apply(revert=False, check=False):
    if not os.path.exists(DASH):
        print(f"no dashboard at {DASH}")
        return 2
    s = open(DASH).read()
    if revert:
        if MARK not in s:
            print("not patched; nothing to revert")
            return 1
        s = re.sub(re.escape(MARK) + r".*?" + re.escape(MARK), "", s, flags=re.S)
        open(DASH, "w").write(s)
        print("reverted")
        return 0
    if MARK in s:
        print("already patched -- run with --revert first")
        return 1
    anchor = find_anchor(s)
    if not anchor:
        print("could not find where the cards end; not touching the file")
        return 2
    at = anchor.start()
    out = s[:at] + "\n" + MARK + "\n" + CARD + MARK + "\n" + s[at:]
    # The script goes at the end of the existing module, not in a new tag.
    tail = out.rfind("</script>")
    out = out[:tail] + MARK + SCRIPT + MARK + "\n" + out[tail:]
    if check:
        print(f"would insert a card at line {s[:at].count(chr(10)) + 1} of {DASH}")
        print(f"would add {SCRIPT.count(chr(10))} lines of script reading {API}")
        print("nothing written (--check)")
        return 0
    open(DASH, "w").write(out)
    print(f"patched {DASH}")
    print("now run controller-bridge/deploy.sh -- NOTE: that restarts jzbridge "
          "and will drop any live session")
    return 0


if __name__ == "__main__":
    sys.exit(apply(revert="--revert" in sys.argv, check="--check" in sys.argv))

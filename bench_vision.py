#!/usr/bin/env python3
"""
Vision-leg benchmark: Gemini Flash tiers vs the llama baseline.

Same discipline as the Nemotron benchmark:
  * ONE attempt per trial, no retries -- success rate is the failure rate.
  * Latency over successes only.
  * All providers interleaved in one shuffled batch, so load variation hits
    them equally rather than favouring whoever ran first.

Caption usability is a GATE, not a nicety.  The caption feeds Nemotron's biome
choice, so a fast caption that drops the water/sand distinction is a
regression, not a win.

    python3 bench_vision.py --screen          # quick model shortlist
    python3 bench_vision.py --n 5             # full run, all 8 images
"""
import argparse, importlib.util, json, math, os, random, statistics, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
_s = importlib.util.spec_from_file_location("pipeline", os.path.join(HERE, "pipeline.py"))
P = importlib.util.module_from_spec(_s); _s.loader.exec_module(P)

BUDGET_S = 120.0

# The two captions that decide the water/desert split.
WATER_WORDS = ("wave", "sea", "ocean", "surf", "water", "tide", "breaker")
GATES = {
    "water_beach_surf.jpg":     ("must name water", True),
    "desert_beach_drysand.jpg": ("must NOT name water", False),
}

CANDIDATES = [
    {"key": "llama (baseline)", "provider": "nvidia", "model": P.VISION_MODEL},
    {"key": "flash-lite-latest", "provider": "gemini", "model": "models/gemini-flash-lite-latest"},
    {"key": "3.5-flash-lite",    "provider": "gemini", "model": "models/gemini-3.5-flash-lite"},
    {"key": "3.1-flash-lite",    "provider": "gemini", "model": "models/gemini-3.1-flash-lite"},
    {"key": "2.5-flash-lite",    "provider": "gemini", "model": "models/gemini-2.5-flash-lite"},
    {"key": "3.8-flash",         "provider": "gemini", "model": "models/gemini-3.8-flash"},
    {"key": "flash-latest",      "provider": "gemini", "model": "models/gemini-flash-latest"},
]


def stats(xs):
    if not xs:
        return {"median": None, "p95": None, "max": None}
    s = sorted(xs)
    k = max(0, math.ceil(0.95 * len(s)) - 1)
    return {"median": int(statistics.median(s)), "p95": s[k], "max": s[-1]}


def one_call(cand, data_url, nvidia_key, gemini_key):
    """Single attempt. Returns (caption|None, ms, error|None). Never raises."""
    deadline = time.monotonic() + BUDGET_S
    saved = P.MAX_ATTEMPTS
    P.MAX_ATTEMPTS = 1          # measurement: no retries
    t0 = time.monotonic()
    try:
        if cand["provider"] == "gemini":
            cap, ms = P.caption_image_gemini(gemini_key, data_url, deadline, model=cand["model"])
        else:
            cap, ms = P.caption_image_nvidia(nvidia_key, data_url, deadline)
        return cap, ms, (None if cap else "failed")
    except Exception as e:
        return None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {e}"
    finally:
        P.MAX_ATTEMPTS = saved


def gate(fname, caption):
    """None if no gate for this image, else (ok_bool, reason)."""
    if fname not in GATES or not caption:
        return None
    desc, want_water = GATES[fname]
    has = any(w in caption.lower() for w in WATER_WORDS)
    return (has == want_water), desc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--screen", action="store_true", help="2 images, n=3, gemini only")
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default="bench_vision.json")
    ap.add_argument("--pool", type=int, default=3)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds between trial starts; Gemini free tier is RPM-limited")
    args = ap.parse_args()

    nvidia_key = P.load_api_key()
    gemini_key = P.load_env_value("GEMINI_API_KEY")
    if not gemini_key:
        print("GEMINI_API_KEY missing"); return 2

    cands = CANDIDATES
    if args.only:
        keep = set(args.only.split(","))
        cands = [c for c in cands if c["key"] in keep]
    images = [os.path.basename(p) for p in P.list_images()]
    n = args.n
    if args.screen:
        images = ["water_beach_surf.jpg", "desert_beach_drysand.jpg"]
        cands = [c for c in cands if c["provider"] == "gemini"]
        n = 3

    print(f"{len(cands)} candidates x {len(images)} images x {n} runs = "
          f"{len(cands)*len(images)*n} trials, single attempt, interleaved\n")

    encoded = {}
    for fn in images:
        du = P.encode_image(os.path.join(P.IMAGES_DIR, fn))
        if du:
            encoded[fn] = du
    print()

    trials = [(c, fn, i) for c in cands for fn in encoded for i in range(n)]
    random.shuffle(trials)
    out, lock, done = [], threading.Lock(), [0]

    gate_lock = threading.Lock()
    last = [0.0]

    def run(t):
        cand, fn, i = t
        if args.delay > 0:
            with gate_lock:
                wait = last[0] + args.delay - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                last[0] = time.monotonic()
        cap, ms, err = one_call(cand, encoded[fn], nvidia_key, gemini_key)
        g = gate(fn, cap)
        rec = {"model": cand["key"], "provider": cand["provider"], "image": fn,
               "ms": ms, "ok": cap is not None, "caption": cap, "error": err,
               "gate_ok": (g[0] if g else None)}
        with lock:
            out.append(rec); done[0] += 1
            mark = "ok  " if rec["ok"] else "FAIL"
            gm = "" if g is None else ("  GATE-OK" if g[0] else "  GATE-FAIL")
            print(f"  [{done[0]:3d}/{len(trials)}] {cand['key']:18s} {fn:26s} "
                  f"{ms:6d} ms {mark}{gm}", flush=True)
        return rec

    with ThreadPoolExecutor(max_workers=args.pool) as ex:
        list(ex.map(run, trials))

    print()
    hdr = (f"{'model':20s} {'median':>8s} {'p95':>8s} {'max':>8s} {'succ':>7s} "
           f"{'gate':>9s}  caption usable?")
    print(hdr); print("-" * (len(hdr) + 20))
    rows = []
    for c in cands:
        mine = [r for r in out if r["model"] == c["key"]]
        oks = [r for r in mine if r["ok"]]
        s = stats([r["ms"] for r in oks])
        gated = [r for r in mine if r["gate_ok"] is not None]
        gok = sum(1 for r in gated if r["gate_ok"])
        verdict = "n/a" if not gated else ("YES" if gok == len(gated) else f"NO ({gok}/{len(gated)})")
        rows.append({"model": c["key"], "provider": c["provider"], "model_id": c["model"],
                     "runs": len(mine), "ok": len(oks),
                     "success_rate": len(oks)/len(mine) if mine else 0, **s,
                     "gate_ok": gok, "gate_total": len(gated)})
        print(f"{c['key']:20s} {str(s['median'] or '-'):>8s} {str(s['p95'] or '-'):>8s} "
              f"{str(s['max'] or '-'):>8s} {len(oks)/max(1,len(mine))*100:6.0f}% "
              f"{gok}/{len(gated):<7d}  {verdict}")

    print("\n--- gate captions (the water/desert split) ---")
    for c in cands:
        for fn in GATES:
            caps = {r["caption"] for r in out if r["model"] == c["key"] and r["image"] == fn and r["caption"]}
            for cap in list(caps)[:2]:
                print(f"  {c['key']:18s} {fn:26s} {cap[:104]}")
    try:
        with open(os.path.join(HERE, "logs", args.out), "w") as f:
            json.dump({"rows": rows, "records": out}, f, indent=2)
        print(f"\n[saved] logs/{args.out}")
    except Exception as e:
        print(f"[save] ignored: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

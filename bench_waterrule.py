#!/usr/bin/env python3
"""
Focused A/B on the water tie-break rule.

The rule as specified fixed water_beach_surf but regressed
desert_beach_drysand: the model reads a bare "beach" as water even when no
water word is present.  This measures three rule variants against the three
captions the pair actually produces.  Same model/params throughout; the only
variable is the rule text.
"""
import importlib.util, json, os, random, sys, threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
_s = importlib.util.spec_from_file_location("pipeline", os.path.join(HERE, "pipeline.py"))
P = importlib.util.module_from_spec(_s); _s.loader.exec_module(P)
_b = importlib.util.spec_from_file_location("bench", os.path.join(HERE, "bench.py"))
B = importlib.util.module_from_spec(_b); _b.loader.exec_module(B)

MODEL = os.environ.get("JZ_NEMOTRON_MODEL") or "nvidia/nemotron-3-super-120b-a12b"

RULES = {
    "none": None,
    "as-specified": P.WATER_RULE,
    "tightened": (P.WATER_RULE +
                  " The word \"beach\" on its own is not water: a beach with no water "
                  "named is desert."),
}

CAPTIONS = [
    ("desert", "The environment in this photo is a sandy beach with a grassy hill in the background."),
    ("desert", "The environment in this photo is a sandy beach with ripples in the sand."),
    ("water",  "The environment in this photo is a rocky beach with waves crashing against the shore."),
    ("water",  "The photo shows a rocky beach with waves crashing against the shore and a few sailboats in the distance."),
    ("desert", "The environment in this photo is a desert."),
]

N = int(os.environ.get("JZ_N", "5"))


def system_for(rule):
    base = P.build_nemotron_system(barks=True, water_rule=False)
    if rule is None:
        return base
    head, sep, tail = base.partition("\nReply with ONLY a JSON object")
    return head + "\n" + rule + "\n" + sep + tail


def main():
    key = P.load_api_key()
    if not key:
        print("no API key"); return 2
    trials = []
    for rname, rule in RULES.items():
        for exp, cap in CAPTIONS:
            for i in range(N):
                trials.append((rname, rule, exp, cap, i))
    random.shuffle(trials)
    out, lock, done = [], threading.Lock(), [0]

    def one(t):
        rname, rule, exp, cap, i = t
        body = {"model": MODEL, "max_tokens": 4096, "temperature": 0.1,
                "reasoning_effort": "none",
                "messages": [{"role": "system", "content": system_for(rule)},
                             {"role": "user", "content": f"Caption: {cap}\n\nReturn the JSON object now."}]}
        res = B.grade(B.post_once(key, body, f"rule:{rname}#{i}", retries=3), exp)
        rec = {"rule": rname, "expected": exp, "caption": cap,
               "biome": res.get("biome"), "ok": res.get("json_ok"),
               "correct": res.get("correct"), "ms": res.get("ms"), "error": res.get("error")}
        with lock:
            out.append(rec); done[0] += 1
            print(f"  [{done[0]:3d}/{len(trials)}] {rname:13s} {exp:7s} -> "
                  f"{str(rec['biome']):7s} {'ok' if rec['ok'] else 'FAIL'}", flush=True)
        return rec

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, trials))

    print()
    hdr = f"{'rule':14s} {'overall':>9s}  " + "  ".join(f"{c[:26]:>26s}" for _, c in
          [(e, c.split('is a ')[-1].split('shows a ')[-1]) for e, c in CAPTIONS])
    print(hdr); print("-" * len(hdr))
    for rname in RULES:
        mine = [r for r in out if r["rule"] == rname and r["ok"]]
        good = sum(1 for r in mine if r["correct"])
        cells = []
        for exp, cap in CAPTIONS:
            sub = [r for r in mine if r["caption"] == cap]
            g = sum(1 for r in sub if r["correct"])
            picks = sorted({r["biome"] for r in sub})
            cells.append(f"{g}/{len(sub)} {','.join(picks):>14s}")
        print(f"{rname:14s} {good}/{len(mine):<8d}  " + "  ".join(f"{c:>26s}" for c in cells))
    print("\nlegend: correct/ok  and the set of biomes chosen")
    with open(os.path.join(HERE, "logs", "bench_waterrule.json"), "w") as f:
        json.dump({"model": MODEL, "n": N, "records": out}, f, indent=2)
    print(f"[saved] logs/bench_waterrule.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
bench.py -- measurement rig for picking the fastest Nemotron configuration
that still gets the biome right.

Self-contained: imports helpers from harness.py in this same folder.
Stdlib + Pillow only.

Subcommands:
  models     Step 1 -- list reachable nemotron model ids, probe each one
  configs    Step 2 -- N runs per prompt/param configuration, pinned caption
  race       Step 3 -- single request vs 3-way raced request
  accuracy   accuracy gate -- run a config across every image in images/

Measurement rules that differ from harness.py on purpose:
  * ONE attempt per trial, no retries.  Retries would conflate "how long does
    a good response take" with "how long does the endpoint stay broken".
    The 503 rate falls out as the success rate instead.
  * Latency stats are over SUCCESSFUL trials only; success rate is separate.
    A fast 503 is not a fast answer.
  * "Success" means HTTP 200, a JSON object was extractable, and its biome
    resolved to one of the ten keys.  That is what the demo actually needs.
Nothing here raises; every trial records its own failure reason.
"""

import argparse
import importlib.util
import json
import math
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location("pipeline", os.path.join(HERE, "pipeline.py"))
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
BENCH_LOG = os.path.join(HERE, "logs", "bench_chat.jsonl")
CAPTION_CACHE = os.path.join(HERE, "logs", "bench_captions.json")
RESULTS_DIR = os.path.join(HERE, "logs")

REQUEST_TIMEOUT_S = 90.0
ACCURACY_RETRIES = 3   # the gate asks 'which biome', so ride out 503s
POOL = 3
BENCH_IMAGE = "lab_electronics_bench.jpg"


def blog(kind, payload):
    """Bench-only jsonl log.  Never raises, never costs a trial."""
    try:
        os.makedirs(os.path.dirname(BENCH_LOG), exist_ok=True)
        rec = {"ts": time.time(), "kind": kind}
        rec.update(payload)
        with open(BENCH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[blog] ignored: {e}")


def _post_attempt(api_key, body, label, timeout=REQUEST_TIMEOUT_S):
    """One attempt.  Returns a dict; never raises."""
    out = {"label": label, "model": body.get("model"), "status": None,
           "ms": 0, "content": None, "error": None, "usage": None}
    try:
        data = json.dumps(body).encode("utf-8")
    except Exception as e:
        out["error"] = f"unserialisable body: {e}"
        return out
    req = urllib.request.Request(
        H.ENDPOINT, data=data, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    t0 = time.monotonic()
    raw = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["status"] = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        out["error"] = f"HTTP {e.code}: {raw[:200].strip()}"
    except urllib.error.URLError as e:
        out["error"] = f"URL error: {e.reason}"
    except TimeoutError:
        out["error"] = f"timed out after {timeout:.0f}s"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["ms"] = int((time.monotonic() - t0) * 1000)

    if out["error"] is None:
        try:
            parsed = json.loads(raw)
            out["content"] = parsed["choices"][0]["message"]["content"]
            out["usage"] = parsed.get("usage")
        except Exception as e:
            out["error"] = f"unparseable reply ({e}): {raw[:200]!r}"
    blog("attempt", {"label": label, "model": out["model"], "status": out["status"],
                     "ms": out["ms"], "error": out["error"], "usage": out["usage"],
                     "content": (out["content"] or "")[:1500]})
    return out


RETRY_BACKOFF = (1.0, 3.0, 6.0, 10.0)


def post_once(api_key, body, label, timeout=REQUEST_TIMEOUT_S, retries=0):
    """
    Default is ONE attempt: latency measurements must not hide the 503 rate.
    The accuracy gate passes retries>0, because there the question is "what
    biome does this config pick", and a 503 is not an answer to that.
    """
    out = None
    for attempt in range(retries + 1):
        out = _post_attempt(api_key, body, label, timeout)
        if out["error"] is None:
            return out
        transient = out["status"] in (429, 500, 502, 503, 504) or "timed out" in (out["error"] or "")
        if not transient or attempt == retries:
            return out
        time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    return out


def grade(res, expected_biome):
    """Add verdict fields to a post_once result.  Never raises."""
    res["json_ok"] = False
    res["biome"] = None
    res["correct"] = None
    if res.get("error") or not res.get("content"):
        return res
    try:
        obj = H.extract_json(res["content"])
        if not isinstance(obj, dict):
            res["error"] = "no JSON object in reply"
            return res
        biome, _how = H.resolve_biome(obj.get("biome"))
        if biome is None:
            res["error"] = f"biome unresolvable: {obj.get('biome')!r}"
            return res
        res["json_ok"] = True
        res["biome"] = biome
        res["name"] = obj.get("name")
        res["has_barks"] = bool(H.clamp_barks(obj.get("barks")))
        if expected_biome:
            res["correct"] = (biome == expected_biome)
    except Exception as e:
        res["error"] = f"grade failed: {type(e).__name__}: {e}"
    return res


def stats(ms_list):
    """median / p95 / max over a list of ms.  p95 = nearest-rank."""
    if not ms_list:
        return {"n": 0, "median": None, "p95": None, "max": None, "min": None}
    xs = sorted(ms_list)
    k = max(0, math.ceil(0.95 * len(xs)) - 1)
    return {"n": len(xs), "median": int(statistics.median(xs)),
            "p95": xs[k], "max": xs[-1], "min": xs[0]}


def load_caption_cache():
    try:
        with open(CAPTION_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_caption_cache(cache):
    try:
        os.makedirs(os.path.dirname(CAPTION_CACHE), exist_ok=True)
        with open(CAPTION_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"[caption cache] ignored: {e}")


def get_caption(api_key, fname, cache):
    """Caption once, then reuse so the vision leg is not a variable."""
    if fname in cache:
        return cache[fname]
    path = os.path.join(H.IMAGES_DIR, fname)
    data_url = H.encode_image(path)
    if data_url is None:
        return None
    deadline = time.monotonic() + 240
    caption, ms = H.caption_image(api_key, data_url, deadline)
    if caption is None:
        print(f"[caption] FAILED for {fname}")
        return None
    print(f"[caption] {fname} ({ms} ms): {caption}")
    cache[fname] = caption
    save_caption_cache(cache)
    return caption


NO_REASON_INSTRUCTION = ("Respond with only the JSON object. Do not explain. "
                         "Do not show your reasoning. Output nothing before the opening brace.")

USER_TMPL = "Caption: {caption}\n\nReturn the JSON object now."


def build_body(cfg, caption):
    """Turn a config dict into a request body."""
    system = H.build_nemotron_system(barks=cfg.get("barks", True),
                                     water_rule=cfg.get("water_rule", False))
    if cfg.get("extra_instruction"):
        system = system + "\n" + cfg["extra_instruction"]
    user = USER_TMPL.format(caption=caption)
    if cfg.get("system_override") is not None:
        user = system + "\n\n" + user
        system = cfg["system_override"]
    body = {
        "model": cfg.get("model") or H.NEMOTRON_MODEL,
        "max_tokens": cfg.get("max_tokens", 4096),
        "temperature": cfg.get("temperature", 0.4),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    body.update(cfg.get("extra_body", {}))
    return body


def base_configs(alt_model=None):
    cfgs = [
        {"key": "A", "desc": "baseline (control): temp 0.4, barks on"},
        {"key": "B", "desc": 'system = "detailed thinking off"',
         "system_override": "detailed thinking off"},
        {"key": "C", "desc": "explicit no-reasoning instruction",
         "extra_instruction": NO_REASON_INSTRUCTION},
        {"key": "D1", "desc": 'reasoning_effort = "low"',
         "extra_body": {"reasoning_effort": "low"}},
        {"key": "D2", "desc": 'reasoning_effort = "none"',
         "extra_body": {"reasoning_effort": "none"}},
        {"key": "E", "desc": 'chat_template_kwargs {"thinking": false}',
         "extra_body": {"chat_template_kwargs": {"thinking": False}}},
        {"key": "A-nb", "desc": "baseline, barks OFF", "barks": False},
        {"key": "C-nb", "desc": "no-reasoning instruction + barks OFF",
         "barks": False, "extra_instruction": NO_REASON_INSTRUCTION},
    ]
    # G -- combinations, all at the shipped temperature and with the water rule,
    # since that is what would actually go live.
    cfgs += [
        {"key": "G", "desc": 'reasoning_effort=none + temp 0.1 + water rule',
         "extra_body": {"reasoning_effort": "none"}, "temperature": 0.1, "water_rule": True},
        {"key": "G2", "desc": 'reasoning_effort=none + thinking:false + temp 0.1 + water rule',
         "extra_body": {"reasoning_effort": "none", "chat_template_kwargs": {"thinking": False}},
         "temperature": 0.1, "water_rule": True},
        {"key": "G4", "desc": 'reasoning_effort=none + temp 0.1 + water rule, barks OFF',
         "extra_body": {"reasoning_effort": "none"}, "temperature": 0.1, "water_rule": True,
         "barks": False},
    ]
    if alt_model:
        cfgs.append({"key": "F", "desc": f"alt model {alt_model}", "model": alt_model})
        cfgs.append({"key": "G3", "desc": f"{alt_model} + reasoning_effort=none + temp 0.1 + water rule",
                     "model": alt_model, "extra_body": {"reasoning_effort": "none"},
                     "temperature": 0.1, "water_rule": True})
    return cfgs


def run_trials(api_key, cfgs, caption, expected, n, pool=POOL, tag="cfg"):
    """Shuffle all (config, run) trials so no config owns a quiet patch."""
    trials = []
    for cfg in cfgs:
        for i in range(n):
            trials.append((cfg, i))
    random.shuffle(trials)
    results = {cfg["key"]: [] for cfg in cfgs}
    lock = threading.Lock()
    done = [0]
    total = len(trials)

    def one(item):
        cfg, i = item
        try:
            body = build_body(cfg, caption)
            res = post_once(api_key, body, f"{tag}:{cfg['key']}#{i}")
            res = grade(res, expected)
        except Exception as e:
            res = {"label": f"{tag}:{cfg['key']}#{i}", "ms": 0, "status": None,
                   "error": f"trial crashed: {type(e).__name__}: {e}",
                   "json_ok": False, "biome": None, "correct": None}
        with lock:
            results[cfg["key"]].append(res)
            done[0] += 1
            ok = "ok" if res.get("json_ok") else "FAIL"
            bio = res.get("biome") or (res.get("error") or "")[:60]
            print(f"  [{done[0]:3d}/{total}] {cfg['key']:6s} {res['ms']:6d} ms  {ok:4s}  {bio}",
                  flush=True)
        return res

    with ThreadPoolExecutor(max_workers=pool) as ex:
        list(ex.map(one, trials))
    return results


def summarise(results, cfgs_by_key):
    rows = []
    for key, res_list in results.items():
        oks = [r for r in res_list if r.get("json_ok")]
        s = stats([r["ms"] for r in oks])
        correct = [r for r in oks if r.get("correct") is True]
        wrong = [r for r in oks if r.get("correct") is False]
        errs = {}
        for r in res_list:
            if not r.get("json_ok"):
                e = (r.get("error") or "unknown")
                tagl = "503" if "503" in e else ("timeout" if "timed out" in e else
                       ("no-json" if "JSON" in e or "biome" in e else e[:40]))
                errs[tagl] = errs.get(tagl, 0) + 1
        rows.append({
            "key": key,
            "desc": cfgs_by_key.get(key, {}).get("desc", ""),
            "model": cfgs_by_key.get(key, {}).get("model") or H.NEMOTRON_MODEL,
            "runs": len(res_list), "ok": len(oks),
            "success_rate": (len(oks) / len(res_list)) if res_list else 0.0,
            "median": s["median"], "p95": s["p95"], "max": s["max"], "min": s["min"],
            "correct": len(correct), "wrong": len(wrong),
            "wrong_biomes": sorted({r["biome"] for r in wrong}),
            "errors": errs,
            "out_tokens": [r["usage"].get("completion_tokens") for r in oks
                           if isinstance(r.get("usage"), dict) and r["usage"].get("completion_tokens")],
        })
    return rows


def print_table(rows, expected):
    print()
    hdr = (f"{'cfg':6s} {'median':>8s} {'p95':>8s} {'max':>8s} {'succ':>7s} "
           f"{'ok/n':>7s} {'biome':>16s} {'outtok':>7s}  desc")
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(rows, key=lambda x: (x["median"] is None, x["median"] or 0)):
        med = f"{r['median']}" if r["median"] is not None else "-"
        p95 = f"{r['p95']}" if r["p95"] is not None else "-"
        mx = f"{r['max']}" if r["max"] is not None else "-"
        acc = f"{r['correct']}/{r['ok']}"
        if r["wrong"]:
            acc += " WRONG:" + ",".join(r["wrong_biomes"])
        ot = r["out_tokens"]
        otm = str(int(statistics.median(ot))) if ot else "-"
        print(f"{r['key']:6s} {med:>8s} {p95:>8s} {mx:>8s} "
              f"{r['success_rate']*100:6.0f}% {r['ok']}/{r['runs']:>3d} {acc:>16s} {otm:>7s}  {r['desc']}")
        if r["errors"]:
            print(f"{'':6s}   failures: {r['errors']}")
    print(f"\n(expected biome for this caption: {expected})")


def save_results(name, payload):
    try:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        p = os.path.join(RESULTS_DIR, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"\n[saved] {p}")
    except Exception as e:
        print(f"[save] ignored: {e}")


def cmd_models(api_key, args):
    print("GET", MODELS_URL)
    req = urllib.request.Request(MODELS_URL, headers={"Authorization": f"Bearer {api_key}",
                                                      "Accept": "application/json"})
    ids = []
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
            status = resp.status
        print(f"  {status} in {int((time.monotonic()-t0)*1000)} ms")
        ids = [m.get("id", "") for m in payload.get("data", [])]
    except Exception as e:
        print(f"  model list failed: {type(e).__name__}: {e}")
        return 1
    print(f"  {len(ids)} model ids total")
    nem = sorted(i for i in ids if "nemotron" in i.lower())
    print(f"\n{len(nem)} ids containing 'nemotron':")
    for i in nem:
        print("   ", i)

    print(f"\nProbing each with a trivial request (max_tokens=16), pool={POOL} ...")
    probe_results = {}
    lock = threading.Lock()

    def probe(mid):
        body = {"model": mid, "max_tokens": 16, "temperature": 0.1,
                "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
        res = post_once(api_key, body, f"probe:{mid}", timeout=90)
        verdict = "works"
        if res["error"]:
            if res["status"] == 404:
                verdict = "404 not found"
            elif res["status"] == 403:
                verdict = "403 forbidden"
            elif res["status"] == 503:
                verdict = "503 busy"
            elif res["status"] == 400:
                verdict = f"400 {res['error'][:80]}"
            elif "timed out" in (res["error"] or ""):
                verdict = "timeout"
            else:
                verdict = (res["error"] or "error")[:80]
        with lock:
            probe_results[mid] = {"verdict": verdict, "ms": res["ms"],
                                  "status": res["status"],
                                  "reply": (res["content"] or "")[:120],
                                  "usage": res["usage"]}
            print(f"  {mid:58s} {verdict:24s} {res['ms']:6d} ms", flush=True)
        return mid

    with ThreadPoolExecutor(max_workers=POOL) as ex:
        list(ex.map(probe, nem))

    working = {k: v for k, v in probe_results.items() if v["verdict"] == "works"}
    print("\nWorking nemotron ids, fastest trivial response first:")
    for k, v in sorted(working.items(), key=lambda x: x[1]["ms"]):
        print(f"  {v['ms']:6d} ms  {k}")
    if not working:
        print("  (none worked on this pass -- 503s may be load, re-run)")
    save_results("bench_models.json", {"all_ids": ids, "nemotron_ids": nem,
                                       "probe": probe_results})
    return 0


def cmd_configs(api_key, args):
    cache = load_caption_cache()
    caption = get_caption(api_key, args.image, cache)
    if caption is None:
        print("cannot benchmark without a caption")
        return 1
    expected = H.expected_biome(args.image)
    print(f"\nPINNED CAPTION ({args.image}, expected {expected}):\n  {caption}\n")

    cfgs = base_configs(alt_model=args.alt_model)
    if args.only:
        keep = set(args.only.split(","))
        cfgs = [c for c in cfgs if c["key"] in keep]
    cfgs_by_key = {c["key"]: c for c in cfgs}
    print(f"{len(cfgs)} configs x {args.n} runs = {len(cfgs)*args.n} trials, "
          f"single attempt each, pool={POOL}\n")
    results = run_trials(api_key, cfgs, caption, expected, args.n, tag="cfg")
    rows = summarise(results, cfgs_by_key)
    print_table(rows, expected)
    save_results(args.out, {"image": args.image, "caption": caption,
                            "expected": expected, "n": args.n, "rows": rows,
                            "raw": results})
    return 0


def raced_call(api_key, body, expected, n_way, label):
    """Fire n_way identical requests, return the first graded success."""
    t0 = time.monotonic()
    results = []
    lock = threading.Lock()
    winner = {"res": None}
    done_evt = threading.Event()

    def one(i):
        try:
            res = grade(post_once(api_key, body, f"{label}#w{i}"), expected)
        except Exception as e:
            res = {"ms": 0, "error": f"crashed: {e}", "json_ok": False, "biome": None}
        with lock:
            results.append(res)
            if res.get("json_ok") and winner["res"] is None:
                winner["res"] = res
                done_evt.set()
            elif len(results) == n_way:
                done_evt.set()
        return res

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(n_way)]
    for t in threads:
        t.start()
    done_evt.wait(timeout=REQUEST_TIMEOUT_S + 10)
    elapsed = int((time.monotonic() - t0) * 1000)
    w = winner["res"]
    out = {"ms": elapsed, "json_ok": bool(w), "biome": w.get("biome") if w else None,
           "correct": w.get("correct") if w else None,
           "requests_spent": n_way,
           "error": None if w else "all racers failed",
           "leg_ms": [r.get("ms") for r in results],
           "leg_errors": [r.get("error") for r in results if r.get("error")]}
    blog("race", out)
    return out


def cmd_race(api_key, args):
    cache = load_caption_cache()
    caption = get_caption(api_key, args.image, cache)
    if caption is None:
        return 1
    expected = H.expected_biome(args.image)
    cfg = {"key": args.config, "desc": "race base"}
    for c in base_configs(alt_model=args.alt_model):
        if c["key"] == args.config:
            cfg = c
            break
    if args.alt_model and args.config == "F":
        cfg["model"] = args.alt_model
    body = build_body(cfg, caption)
    print(f"racing config {args.config} on {args.image} (expected {expected})")
    print(f"model: {body['model']}   n={args.n} rounds, {args.ways}-way race vs single\n")

    singles, raced = [], []
    for i in range(args.n):
        s = grade(post_once(api_key, body, f"race:single#{i}"), expected)
        singles.append(s)
        print(f"  single #{i}: {s['ms']:6d} ms  {'ok' if s.get('json_ok') else 'FAIL'}  "
              f"{s.get('biome') or (s.get('error') or '')[:50]}", flush=True)
        r = raced_call(api_key, body, expected, args.ways, f"race:{args.ways}way#{i}")
        raced.append(r)
        print(f"  raced  #{i}: {r['ms']:6d} ms  {'ok' if r['json_ok'] else 'FAIL'}  "
              f"{r.get('biome') or ''}   legs={r['leg_ms']}", flush=True)

    def block(name, lst, per_round_requests):
        oks = [x for x in lst if x.get("json_ok")]
        s = stats([x["ms"] for x in oks])
        corr = sum(1 for x in oks if x.get("correct") is True)
        return {"mode": name, "rounds": len(lst), "ok": len(oks),
                "success_rate": len(oks) / len(lst) if lst else 0,
                "median": s["median"], "p95": s["p95"], "max": s["max"], "min": s["min"],
                "correct": corr, "requests": per_round_requests * len(lst)}

    a = block("single", singles, 1)
    b = block(f"{args.ways}-way raced", raced, args.ways)
    print()
    hdr = f"{'mode':14s} {'median':>8s} {'p95':>8s} {'max':>8s} {'succ':>7s} {'correct':>8s} {'reqs':>6s}"
    print(hdr); print("-" * len(hdr))
    for r in (a, b):
        print(f"{r['mode']:14s} {str(r['median'] or '-'):>8s} {str(r['p95'] or '-'):>8s} "
              f"{str(r['max'] or '-'):>8s} {r['success_rate']*100:6.0f}% "
              f"{r['correct']}/{r['ok']:<6d} {r['requests']:>6d}")
    save_results(args.out, {"image": args.image, "caption": caption, "expected": expected,
                            "config": args.config, "model": body["model"],
                            "single": a, "raced": b,
                            "raw": {"single": singles, "raced": raced}})
    return 0


def cmd_accuracy(api_key, args):
    cache = load_caption_cache()
    images = [os.path.basename(p) for p in H.list_images()]
    if not images:
        print("images/ is empty")
        return 0
    cfgs = base_configs(alt_model=args.alt_model)
    keep = set(args.only.split(",")) if args.only else {c["key"] for c in cfgs}
    cfgs = [c for c in cfgs if c["key"] in keep]
    if args.water_rule:
        for c in cfgs:
            c["water_rule"] = True
    if args.temperature is not None:
        for c in cfgs:
            c["temperature"] = args.temperature
    print(f"accuracy gate: {len(cfgs)} config(s) x {len(images)} images x {args.n} run(s)"
          f"  water_rule={args.water_rule} temp={args.temperature}\n")

    captions = {}
    for fn in images:
        c = get_caption(api_key, fn, cache)
        if c:
            captions[fn] = c
    print()

    trials = []
    for cfg in cfgs:
        for fn, cap in captions.items():
            for i in range(args.n):
                trials.append((cfg, fn, cap, i))
    random.shuffle(trials)
    out = []
    lock = threading.Lock()
    done = [0]

    def one(item):
        cfg, fn, cap, i = item
        exp = H.expected_biome(fn)
        try:
            res = grade(post_once(api_key, build_body(cfg, cap), f"acc:{cfg['key']}:{fn}#{i}",
                                  retries=ACCURACY_RETRIES), exp)
        except Exception as e:
            res = {"ms": 0, "error": f"crashed: {e}", "json_ok": False, "biome": None, "correct": None}
        rec = {"config": cfg["key"], "image": fn, "expected": exp,
               "biome": res.get("biome"), "correct": res.get("correct"),
               "ms": res.get("ms"), "ok": res.get("json_ok"), "error": res.get("error")}
        with lock:
            out.append(rec)
            done[0] += 1
            mark = "MATCH" if rec["correct"] else ("MISMATCH" if rec["ok"] else "FAILED")
            print(f"  [{done[0]:3d}/{len(trials)}] {cfg['key']:6s} {fn:28s} "
                  f"{str(exp):7s} -> {str(rec['biome']):7s} {mark:9s} {rec['ms']:6d} ms",
                  flush=True)
        return rec

    with ThreadPoolExecutor(max_workers=POOL) as ex:
        list(ex.map(one, trials))

    print()
    for cfg in cfgs:
        mine = [r for r in out if r["config"] == cfg["key"]]
        scored = [r for r in mine if r["ok"] and r["expected"]]
        good = [r for r in scored if r["correct"]]
        bad = [r for r in scored if not r["correct"]]
        failed = [r for r in mine if not r["ok"]]
        ms = stats([r["ms"] for r in mine if r["ok"]])
        print(f"{cfg['key']:6s} accuracy {len(good)}/{len(scored)}  failed={len(failed)}  "
              f"median={ms['median']} p95={ms['p95']}  {cfg['desc']}")
        for r in bad:
            print(f"        MISMATCH {r['expected']} -> {r['biome']}   {r['image']}")
        for r in failed:
            print(f"        FAILED   {r['image']}: {(r['error'] or '')[:80]}")
    save_results(args.out, {"n": args.n, "water_rule": args.water_rule,
                            "temperature": args.temperature, "records": out})
    return 0


def main():
    ap = argparse.ArgumentParser(description="benchmark rig for the jen-zombie pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("models"); p.set_defaults(fn=cmd_models)

    p = sub.add_parser("configs"); p.set_defaults(fn=cmd_configs)
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--image", default=BENCH_IMAGE)
    p.add_argument("--alt-model", default=None)
    p.add_argument("--only", default=None, help="comma list of config keys")
    p.add_argument("--out", default="bench_configs.json")

    p = sub.add_parser("race"); p.set_defaults(fn=cmd_race)
    p.add_argument("--n", type=int, default=12)
    p.add_argument("--ways", type=int, default=3)
    p.add_argument("--image", default=BENCH_IMAGE)
    p.add_argument("--config", default="A")
    p.add_argument("--alt-model", default=None)
    p.add_argument("--out", default="bench_race.json")

    p = sub.add_parser("accuracy"); p.set_defaults(fn=cmd_accuracy)
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--only", default=None)
    p.add_argument("--alt-model", default=None)
    p.add_argument("--water-rule", action="store_true")
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--out", default="bench_accuracy.json")

    args = ap.parse_args()
    api_key = H.load_api_key()
    if not api_key:
        print("NVIDIA_API_KEY not found in environment or .env")
        return 2
    random.seed()
    try:
        return args.fn(api_key, args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1
    except Exception as e:
        print(f"bench failed: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

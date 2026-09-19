#!/usr/bin/env python3
"""
Standalone test harness for the photo -> world pipeline.

  1. vision model captions the photo in one sentence
  2. Nemotron reads the caption and picks a biome + difficulty scales

Loops over images/ (filename prefix = expected biome), prints each result,
and on exit prints a MATCH/MISMATCH tally.  Stdlib + Pillow only.

Rules this file follows:
  * No pipeline function raises.  On failure it returns None and prints why.
  * Every HTTP request has a wall-clock timeout; the whole run has a budget.
  * Every request/reply is appended to logs/model_chat.jsonl (best effort).
"""

import argparse
import base64
import io
import json
import math
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(HERE, "images")
LOG_DIR = os.path.join(HERE, "logs")
LOG_PATH = os.path.join(LOG_DIR, "model_chat.jsonl")

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
VISION_MODEL = os.environ.get("JZ_VISION_MODEL") or "meta/llama-3.2-11b-vision-instruct"
NEMOTRON_MODEL = os.environ.get("JZ_NEMOTRON_MODEL") or "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"

# Image handling
MAX_EDGE = 640
JPEG_QUALITY = 68
CAPTION_MAX_CHARS = 320

# Timeouts / retries
REQUEST_TIMEOUT_S = 60.0       # wall clock per HTTP request
MAX_ATTEMPTS = 6               # per call (retries on 503 / transient errors)
# This endpoint returns 503 "Worker local total request limit reached" under
# load and can stay saturated for tens of seconds, so back off generously.
BACKOFF_S = (2.0, 5.0, 10.0, 20.0, 30.0)
RUN_BUDGET_S = 240.0           # hard ceiling for one full pipeline run

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")

BIOMES = ["lab", "rocky", "cave", "desert", "forest", "meadow", "snow", "water", "street", "farm"]

BIOME_MENU = '''  "lab": a desk, bench or room with electronics, wiring, computers, tools, lab equipment, circuit boards or machinery
  "rocky": rock, stone, boulders, cliffs, gravel, a quarry or a rocky hillside
  "cave": a cave, tunnel, cellar, basement, dungeon or any dark enclosed stone interior
  "desert": desert, sand dunes, dry earth, arid ground, a beach without water
  "forest": forest, woods, many trees, undergrowth, a wooded park
  "meadow": grass, lawn, a field, a park, open green outdoor space
  "snow": snow, ice, a ski slope, a winter or mountain scene, anything white and cold
  "water": a pool, lake, river, sea, harbour or any large body of water
  "street": a street, road, city, town, buildings, pavement, an urban scene
  "farm": a farm, crops, tilled soil, a barn, hay, fields in rows'''

ENEMY_TYPES = ["walker", "runner", "brute"]

VISION_SYSTEM = (
    "You describe photographs for a game. Reply with exactly ONE plain sentence "
    "describing the physical environment and terrain in the photo: the ground, "
    "the surroundings, whether there is water, sand, snow, rock, grass, trees, "
    "buildings or equipment. No preamble, no list, no markdown."
)

NEMOTRON_SYSTEM = f"""You turn a one-sentence photo caption into a game level for a zombie shooter.
Pick exactly one biome from this menu. Match the caption to the description, not to the word.

{BIOME_MENU}

Reply with ONLY a JSON object, no prose, no markdown fences, with exactly these fields:
  "name":         short level name, a few words
  "biome":        one of the ten keys above, exact string
  "count_scale":  float 0.5-2.0  (how many enemies)
  "speed_scale":  float 0.5-2.0  (how fast they move)
  "detect_scale": float 0.5-2.0  (how far they notice the player)
  "brute_bias":   float 0.0-0.5  (share of big slow enemies)
  "hp_bonus":     int 0-3        (extra enemy hit points)
  "barks":        object mapping each enemy type ({", ".join(ENEMY_TYPES)}) to an array of 2-3 short taunt lines
Each bark line MUST be 8 words or fewer and in character for the chosen biome
(a beach zombie says beach things, a lab zombie says lab things)."""

DEFAULTS = {
    "name": "Unnamed Level",
    "biome": "meadow",
    "count_scale": 1.0,
    "speed_scale": 1.0,
    "detect_scale": 1.0,
    "brute_bias": 0.15,
    "hp_bonus": 0,
}

RANGES = {
    "count_scale": (0.5, 2.0),
    "speed_scale": (0.5, 2.0),
    "detect_scale": (0.5, 2.0),
    "brute_bias": (0.0, 0.5),
    "hp_bonus": (0, 3),
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_api_key():
    """NVIDIA_API_KEY from env first, then a .env file next to this script."""
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if key:
        return key
    env_path = os.path.join(HERE, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "NVIDIA_API_KEY":
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[config] could not read .env: {e}")
    return None


# --------------------------------------------------------------------------
# logging (best effort, never raises)
# --------------------------------------------------------------------------

def log_event(kind, payload):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {"ts": time.time(), "kind": kind}
        rec.update(payload)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        # Logging must never cost a run.  Mention it once per failure and move on.
        print(f"[log] write failed (ignored): {e}")


def redact_for_log(body):
    """Copy of the request body with inline image data shortened."""
    try:
        clone = json.loads(json.dumps(body))
        for msg in clone.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        part["image_url"]["url"] = f"<data url, {len(url)} chars>"
        return clone
    except Exception:
        return {"_unloggable": True}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def chat_completion(api_key, body, label, deadline):
    """
    POST to the endpoint.  Retries on 503 (and other transient errors) with a
    short backoff, respecting the run deadline.  Returns (content_str, elapsed_ms)
    or (None, elapsed_ms) after printing why.
    """
    started = time.monotonic()
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    logged_body = redact_for_log(body)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"[{label}] run budget exhausted before attempt {attempt}; giving up")
            return None, int((time.monotonic() - started) * 1000)
        timeout = min(REQUEST_TIMEOUT_S, remaining)

        log_event("request", {"label": label, "attempt": attempt, "model": body.get("model"),
                              "timeout_s": round(timeout, 1), "body": logged_body})
        req = urllib.request.Request(ENDPOINT, data=data, headers=headers, method="POST")
        t0 = time.monotonic()
        status = None
        raw = ""
        err_text = None
        retryable = False
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            err_text = f"HTTP {status}: {raw[:300].strip() or e.reason}"
            retryable = status in (429, 500, 502, 503, 504)
        except urllib.error.URLError as e:
            err_text = f"URL error: {e.reason}"
            retryable = True
        except TimeoutError:
            err_text = f"timed out after {timeout:.0f}s"
            retryable = True
        except Exception as e:
            err_text = f"{type(e).__name__}: {e}"
            retryable = "timed out" in str(e).lower()
        call_ms = int((time.monotonic() - t0) * 1000)

        if err_text is None:
            try:
                parsed = json.loads(raw)
                content = parsed["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError("content is not a string")
            except Exception as e:
                err_text = f"unparseable reply ({e}): {raw[:300]!r}"
                retryable = False

        log_event("reply", {"label": label, "attempt": attempt, "status": status,
                            "elapsed_ms": call_ms, "error": err_text,
                            "raw": raw[:4000] if raw else ""})

        if err_text is None:
            return content, int((time.monotonic() - started) * 1000)

        print(f"[{label}] attempt {attempt}/{MAX_ATTEMPTS} failed: {err_text}")
        if not retryable or attempt == MAX_ATTEMPTS:
            break
        wait = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
        if time.monotonic() + wait >= deadline:
            print(f"[{label}] no budget left for backoff; giving up")
            break
        print(f"[{label}] retrying in {wait:.0f}s ...")
        time.sleep(wait)

    return None, int((time.monotonic() - started) * 1000)


# --------------------------------------------------------------------------
# step 0: image
# --------------------------------------------------------------------------

def encode_image(path):
    """Downscale to MAX_EDGE, JPEG q=JPEG_QUALITY, return data URL or None."""
    try:
        from PIL import Image, ImageOps
    except Exception as e:
        print(f"[image] Pillow not importable: {e}")
        return None
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            w, h = im.size
            long_edge = max(w, h)
            if long_edge > MAX_EDGE:
                scale = MAX_EDGE / float(long_edge)
                im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        print(f"[image] {os.path.basename(path)}: {w}x{h} -> {im.size[0]}x{im.size[1]}, "
              f"{len(buf.getvalue()) // 1024} KB jpeg, {len(b64) // 1024} KB base64")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        print(f"[image] failed to encode {path}: {type(e).__name__}: {e}")
        return None


# --------------------------------------------------------------------------
# step 1: caption
# --------------------------------------------------------------------------

def caption_image(api_key, data_url, deadline):
    """Returns (caption_str, elapsed_ms) or (None, elapsed_ms)."""
    body = {
        "model": VISION_MODEL,
        "max_tokens": 120,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "Describe the environment in this photo in one sentence."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    }
    try:
        content, ms = chat_completion(api_key, body, "vision", deadline)
        if content is None:
            return None, ms
        caption = " ".join(content.split()).strip()
        if not caption:
            print("[vision] empty caption returned")
            return None, ms
        if len(caption) > CAPTION_MAX_CHARS:
            caption = caption[:CAPTION_MAX_CHARS]
        return caption, ms
    except Exception as e:
        print(f"[vision] unexpected error: {type(e).__name__}: {e}")
        return None, 0


# --------------------------------------------------------------------------
# step 2: world
# --------------------------------------------------------------------------

def _strip_reasoning(text):
    """Reasoning models may wrap thinking in <think>...</think>; drop it."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<think>.*", "", text, flags=re.S)  # unterminated
    return text.strip()


def extract_json(text):
    """Find the first JSON object in the text. Returns dict or None."""
    text = _strip_reasoning(text)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def clamp_number(value, lo, hi, default, as_int=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        v = max(lo, min(hi, v))
        return int(round(v)) if as_int else round(v, 3)
    except Exception:
        return default


def resolve_biome(raw):
    """Exact match, then substring match either direction, else None."""
    if not isinstance(raw, str):
        return None, "not a string"
    s = raw.strip().lower()
    if s in BIOMES:
        return s, "exact"
    for b in BIOMES:
        if b in s:
            return b, f"substring ({raw!r} contains {b!r})"
    # a few common synonyms the menu wording implies
    synonyms = {
        "rock": "rocky", "stone": "rocky", "boulder": "rocky", "cliff": "rocky", "quarry": "rocky",
        "wood": "forest", "tree": "forest", "jungle": "forest",
        "grass": "meadow", "field": "meadow", "lawn": "meadow", "park": "meadow",
        "ice": "snow", "winter": "snow", "glacier": "snow",
        "sea": "water", "ocean": "water", "lake": "water", "river": "water", "pool": "water", "harbour": "water", "harbor": "water",
        "city": "street", "urban": "street", "road": "street", "town": "street",
        "sand": "desert", "dune": "desert", "arid": "desert",
        "tunnel": "cave", "basement": "cave", "cellar": "cave", "dungeon": "cave",
        "barn": "farm", "crop": "farm",
        "workshop": "lab", "office": "lab", "electronics": "lab",
    }
    for word, b in synonyms.items():
        if word in s:
            return b, f"synonym ({raw!r} ~ {word!r})"
    return None, f"no match for {raw!r}"


def clamp_barks(raw):
    """Drop lines over 8 words, cap at 3 per enemy type. Returns dict (may be empty)."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for enemy, lines in raw.items():
        if not isinstance(enemy, str):
            continue
        if isinstance(lines, str):
            lines = [lines]
        if not isinstance(lines, list):
            continue
        kept = []
        for line in lines:
            if not isinstance(line, str):
                continue
            line = " ".join(line.split()).strip()
            if not line:
                continue
            if len(line.split()) > 8:
                print(f"[world] dropped bark for {enemy!r} (>8 words): {line!r}")
                continue
            kept.append(line)
            if len(kept) == 3:
                break
        if kept:
            out[enemy.strip().lower()] = kept
    return out


def normalise_world(obj):
    """Clamp everything, resolve biome. Returns dict or None (prints why)."""
    world = dict(DEFAULTS)
    notes = []

    name = obj.get("name")
    if isinstance(name, str) and name.strip():
        world["name"] = " ".join(name.split())[:60]
    else:
        notes.append("name missing -> default")

    biome, how = resolve_biome(obj.get("biome"))
    if biome is None:
        print(f"[world] biome unresolvable: {how}")
        return None
    if how != "exact":
        notes.append(f"biome resolved by {how}")
    world["biome"] = biome

    for field, (lo, hi) in RANGES.items():
        raw = obj.get(field)
        as_int = field == "hp_bonus"
        val = clamp_number(raw, lo, hi, DEFAULTS[field], as_int=as_int)
        if raw is None:
            notes.append(f"{field} missing -> {val}")
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool) \
                and not (isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw))) \
                and float(raw) != float(val):
            notes.append(f"{field} clamped {raw} -> {val}")
        elif not isinstance(raw, (int, float)):
            notes.append(f"{field} invalid {raw!r} -> {val}")
        world[field] = val

    world["barks"] = clamp_barks(obj.get("barks"))
    if not world["barks"]:
        notes.append("barks missing/empty")

    for n in notes:
        print(f"[world] note: {n}")
    return world


def choose_world(api_key, caption, deadline):
    """Returns (world_dict, elapsed_ms) or (None, elapsed_ms)."""
    body = {
        "model": NEMOTRON_MODEL,
        # This is a reasoning model and thinks out loud before the JSON, often
        # without <think> tags.  Too small a budget truncates mid-reasoning and
        # the reply then contains no JSON at all.
        "max_tokens": 4096,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": NEMOTRON_SYSTEM},
            {"role": "user", "content": f"Caption: {caption[:CAPTION_MAX_CHARS]}\n\nReturn the JSON object now."},
        ],
    }
    try:
        content, ms = chat_completion(api_key, body, "nemotron", deadline)
        if content is None:
            return None, ms
        obj = extract_json(content)
        if obj is None:
            print(f"[nemotron] reply contained no JSON object: {_strip_reasoning(content)[:200]!r}")
            return None, ms
        world = normalise_world(obj)
        return world, ms
    except Exception as e:
        print(f"[nemotron] unexpected error: {type(e).__name__}: {e}")
        return None, 0


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def list_images():
    try:
        names = sorted(os.listdir(IMAGES_DIR))
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[images] could not list {IMAGES_DIR}: {e}")
        return []
    return [os.path.join(IMAGES_DIR, n) for n in names
            if n.lower().endswith(IMAGE_EXTS) and not n.startswith(".")]


def expected_biome(path):
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    prefix = re.split(r"[_\-\s.]", stem, maxsplit=1)[0]
    return prefix if prefix in BIOMES else None


def run_once(api_key, path, tally):
    fname = os.path.basename(path)
    expected = expected_biome(path)
    print("=" * 72)
    print(f"IMAGE     {fname}")
    print(f"EXPECTED  {expected if expected else '(no biome prefix on filename)'}")
    deadline = time.monotonic() + RUN_BUDGET_S

    data_url = encode_image(path)
    if data_url is None:
        print("RESULT    FAILED at image encode -> no world")
        tally["failed"].append((fname, "image encode"))
        return

    caption, vision_ms = caption_image(api_key, data_url, deadline)
    print(f"VISION    {vision_ms} ms")
    if caption is None:
        print("RESULT    FAILED at vision call -> no world")
        tally["failed"].append((fname, "vision"))
        return
    print(f"CAPTION   {caption}")

    world, nemo_ms = choose_world(api_key, caption, deadline)
    print(f"NEMOTRON  {nemo_ms} ms")
    if world is None:
        print("RESULT    FAILED at nemotron call -> no world")
        tally["failed"].append((fname, "nemotron"))
        return

    print(f"CHOSEN    {world['biome']}    name=\"{world['name']}\"")
    print(f"SCALES    count={world['count_scale']}  speed={world['speed_scale']}  "
          f"detect={world['detect_scale']}  brute_bias={world['brute_bias']}  hp_bonus={world['hp_bonus']}")
    if world["barks"]:
        print("BARKS")
        for enemy, lines in world["barks"].items():
            for i, line in enumerate(lines):
                print(f"          {enemy + ':' if i == 0 else '':8s} {line}")
    else:
        print("BARKS     (none)")

    if expected is None:
        print("RESULT    UNSCORED (no expected prefix)")
        tally["unscored"] += 1
    elif world["biome"] == expected:
        print("RESULT    MATCH")
        tally["matched"] += 1
    else:
        print(f"RESULT    MISMATCH  expected {expected} -> chosen {world['biome']}")
        tally["mismatches"].append((fname, expected, world["biome"]))
    print(f"TOTAL     vision {vision_ms} ms + nemotron {nemo_ms} ms")


def print_tally(tally):
    scored = tally["matched"] + len(tally["mismatches"])
    print()
    print("#" * 72)
    print(f"TALLY     runs={tally['runs']}  matched={tally['matched']}/{scored}  "
          f"mismatched={len(tally['mismatches'])}  failed={len(tally['failed'])}  unscored={tally['unscored']}")
    if tally["mismatches"]:
        print("MISMATCHES (expected -> chosen):")
        for fname, exp, got in tally["mismatches"]:
            print(f"    {exp:8s} -> {got:8s}   {fname}")
    if tally["failed"]:
        print("FAILED RUNS (stage):")
        for fname, stage in tally["failed"]:
            print(f"    {stage:8s}    {fname}")
    print("#" * 72)


def main():
    ap = argparse.ArgumentParser(description="photo -> caption -> biome test harness")
    ap.add_argument("--once", action="store_true", help="run a single image and exit")
    ap.add_argument("--interval", type=float, default=30.0, help="seconds between runs (default 30)")
    ap.add_argument("--image", help="force one image file (implies --once unless --loop)")
    ap.add_argument("--loop", action="store_true", help="with --image, keep re-running that file")
    args = ap.parse_args()

    api_key = load_api_key()
    if not api_key:
        print("NVIDIA_API_KEY not found in environment or .env -- nothing to do.")
        print("Put NVIDIA_API_KEY=nvapi-... in a .env file next to harness.py (see .env.example).")
        return 2

    print(f"vision model:   {VISION_MODEL}")
    print(f"nemotron model: {NEMOTRON_MODEL}")
    print(f"log file:       {LOG_PATH}")

    if args.image:
        if not os.path.isfile(args.image):
            print(f"--image {args.image!r} is not a file.")
            return 2
        images = [os.path.abspath(args.image)]
        once = not args.loop
    else:
        images = list_images()
        if not images:
            print(f"images/ is empty (looked in {IMAGES_DIR}).")
            print("Drop photos in there named <expected_biome>_whatever.jpg -- see images/README.md.")
            return 0
        once = args.once
        print(f"images:         {len(images)} file(s) in images/")

    tally = {"runs": 0, "matched": 0, "mismatches": [], "failed": [], "unscored": 0}

    # Ctrl-C: let the KeyboardInterrupt propagate so the tally prints in finally.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    try:
        while True:
            path = random.choice(images)
            tally["runs"] += 1
            try:
                run_once(api_key, path, tally)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # belt and braces -- run_once should never raise, but the loop must survive
                print(f"RESULT    FAILED with unexpected {type(e).__name__}: {e}")
                tally["failed"].append((os.path.basename(path), "harness"))
            if once:
                break
            print(f"\n... next run in {args.interval:g}s (Ctrl-C for tally)\n")
            time.sleep(max(0.0, args.interval))
    except KeyboardInterrupt:
        print("\ninterrupted.")
    finally:
        print_tally(tally)
    return 0


if __name__ == "__main__":
    sys.exit(main())

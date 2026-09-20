#!/usr/bin/env python3
"""
HTTP service around the photo -> world pipeline.

    POST /world   body = a JPEG (raw bytes, Content-Type: image/jpeg)
                  200  = the world JSON the game renders
    GET  /health  200, says whether both API keys are present and reachable
    GET  /frames  200, the last few camera frames this service was given, with
                  their timestamps, so a dashboard can show the photo that goes
                  with a particular exchange rather than whatever is newest.
    GET  /frame?id=<ms>    200 image/jpeg, one of them.
    GET  /say?text=&role=  200 audio/mpeg, the line spoken by ElevenLabs.
                  Cached on disk by (voice, model, text), so a repeated line is
                  free. Refuses past a character budget; the game falls back to
                  showing the line as text, which is why every failure here is
                  reported rather than hidden.
    GET  /voices  200, which voice plays which role, and what has been spent
    GET  /trace   200, the last few model exchanges as readable text: what was
                  sent to Gemini and to Nemotron and what each said back. It
                  reads pipeline's own logs/model_chat.jsonl, so nothing is
                  recorded twice and nothing extra is held in memory.

The Pi reverse-proxies to this over the wired link, so it binds 0.0.0.0.
The pipeline itself is not reimplemented here: pipeline.py owns the model
calls, the retries, the water rule, the clamping and the jsonl logging, and
this file only calls into it.  Stdlib + Pillow, same as the rest.

Three things this adds on top of the harness:

  * a hard wall-clock budget per request (JZ_WORLD_BUDGET_S, default 25s).
    pipeline's deadline is cooperative -- every HTTP attempt and every backoff
    checks it -- but the request is ALSO run on a worker thread the handler
    only waits on until the budget expires, so a wedged call cannot hang the
    game.  The game shows "INCOMING" and holds if we are slow; it has nothing
    to show if we never answer.
  * a fallback world instead of an error.  A request always ends in a valid
    world, and a world that did not come from the models is marked
    "fallback": true and logged loudly.  It is never disguised as a real one
    and it is never cached.
  * a cache keyed on the sha256 of the JPEG bytes, so a repeated frame does
    not re-bill either model.
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# pipeline.py reads its configuration into module constants at import time, so
# the shipped config has to be in the environment BEFORE the import below.
# setdefault, not assignment: a real environment variable still wins, which is
# how bench runs and the JZ_VISION_PROVIDER=nvidia fallback stay reachable.
os.environ.setdefault("JZ_VISION_PROVIDER", "gemini")
os.environ.setdefault("JZ_NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b-a12b")
os.environ.setdefault("JZ_REASONING_EFFORT", "none")

sys.path.insert(0, HERE)
import pipeline  # noqa: E402  (must follow the environment defaults above)

HOST = os.environ.get("JZ_WORLD_HOST", "0.0.0.0")
PORT = int(os.environ.get("JZ_WORLD_PORT", "8099"))
# The contract: 25 seconds of wall clock, end to end, per request.
BUDGET_S = float(os.environ.get("JZ_WORLD_BUDGET_S", "25"))
# Slack for the worker to notice its own deadline and unwind before we stop
# waiting on it. Only the outer number is a promise to the caller.
GRACE_S = 1.5
MAX_BODY = 12 * 1024 * 1024      # a phone camera frame is ~1-3 MB
CACHE_MAX = 128
HEALTH_TTL_S = 30.0              # health probes are cached; /health is free to poll
PROBE_TIMEOUT_S = 4.0
# Used only to print the address the Pi should dial, by asking the routing
# table which of our interfaces faces it.
PI_ADDR = os.environ.get("JZ_PI_ADDR", "10.42.1.1")

NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
GEMINI_MODELS_URL = pipeline.GEMINI_BASE + "/models?pageSize=1"

# A fallback world still has to be playable, so it is the pipeline's own
# defaults (biome meadow, every scale 1.0) plus barks, so mobs are not mute.
# The name is deliberately not flavour text: on screen it reads as what it is.
FALLBACK_NAME = "Fallback Level"
FALLBACK_BARKS = {
    "walker": ["Still coming.", "No hurry."],
    "runner": ["Faster than you.", "Keep running."],
    "brute":  ["Nowhere to go.", "Stand still."],
    "calm": ["Nothing left here.", "Mind how you go.", "Quiet, for now."],
}

_started = time.monotonic()
_seq_lock = threading.Lock()
_seq = 0

_cache = OrderedDict()           # sha256 hex -> world dict (successful only)
_cache_lock = threading.Lock()
_stats = {"requests": 0, "cache_hits": 0, "fallbacks": 0, "ok": 0}
_stats_lock = threading.Lock()

_health_lock = threading.Lock()
_health_cached = {"at": 0.0, "value": None}


def now_ms():
    return int(time.time() * 1000)


def next_rid():
    global _seq
    with _seq_lock:
        _seq += 1
        return f"r{_seq:04d}"


def bump(key, n=1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


# --------------------------------------------------------------------------
# world building -- everything here is pipeline.py's work, just sequenced
# --------------------------------------------------------------------------

def encode_jpeg_bytes(raw, rid):
    """
    pipeline.encode_image() takes a path and owns the downscale, the EXIF
    transpose, the quality and the log line. Rather than keep a second copy of
    that, spill the upload to a temp file and hand it the path.
    Returns a data URL or None (it prints why).
    """
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=f"jzworld_{rid}_", suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return pipeline.encode_image(tmp)
    except Exception as e:
        print(f"[image] could not stage upload: {type(e).__name__}: {e}")
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def build_world(data_url, deadline, rid):
    """
    caption -> world, on pipeline's own legs. Never raises.
    Returns (world|None, reason|None, caption, vision_ms, nemotron_ms).
    """
    try:
        api_key = pipeline.load_api_key()
        if not api_key:
            return None, "NVIDIA_API_KEY not found in environment or .env", "", 0, 0

        caption, vision_ms = pipeline.caption_image(api_key, data_url, deadline)
        if caption is None:
            return None, "vision leg returned no caption", "", vision_ms, 0
        print(f"[{rid}] caption ({vision_ms} ms): {caption}")

        world, nemo_ms = pipeline.choose_world(api_key, caption, deadline)
        if world is None:
            return None, "nemotron leg returned no usable world", caption, vision_ms, nemo_ms

        world["caption"] = caption
        world["fallback"] = False
        world["ms"] = {"vision": int(vision_ms), "nemotron": int(nemo_ms)}
        return world, None, caption, vision_ms, nemo_ms
    except Exception as e:
        # pipeline's functions do not raise, but a world that fails to build
        # must still be a fallback and not a 500.
        return None, f"unexpected {type(e).__name__}: {e}", "", 0, 0


def fallback_world(reason, rid, caption="", vision_ms=0, nemotron_ms=0):
    """A valid, playable world that is unmistakably a fallback. Logged loudly."""
    world = dict(pipeline.DEFAULTS)
    world["name"] = FALLBACK_NAME
    world["barks"] = {k: list(v) for k, v in FALLBACK_BARKS.items()}
    world["caption"] = caption
    world["fallback"] = True
    world["ms"] = {"vision": int(vision_ms), "nemotron": int(nemotron_ms)}
    bump("fallbacks")
    print("!" * 72)
    print(f"[{rid}] FALLBACK WORLD SERVED -- {reason}")
    print(f"[{rid}] serving biome={world['biome']} name={world['name']!r} "
          f"vision={vision_ms} ms nemotron={nemotron_ms} ms")
    print("!" * 72)
    pipeline.log_event("fallback", {"rid": rid, "reason": reason, "caption": caption,
                                    "vision_ms": vision_ms, "nemotron_ms": nemotron_ms})
    return world


def world_for_image(raw, rid):
    """
    The whole request, budget included. Returns (world_dict, cached_bool).
    Always returns a world.
    """
    deadline = time.monotonic() + BUDGET_S
    digest = hashlib.sha256(raw).hexdigest()

    with _cache_lock:
        hit = _cache.get(digest)
        if hit is not None:
            _cache.move_to_end(digest)
    if hit is not None:
        bump("cache_hits")
        print(f"[{rid}] cache hit {digest[:12]} -> {hit['biome']} "
              f"(no model call, no bill)")
        return dict(hit), True

    data_url = encode_jpeg_bytes(raw, rid)
    if data_url is None:
        # Not a model failure: the bytes were not a decodable image. Still a
        # world, so the game has something, and still flagged as a fallback.
        return fallback_world("upload was not a decodable image", rid), False

    result = {}

    def work():
        (result["world"], result["reason"], result["caption"],
         result["vision_ms"], result["nemotron_ms"]) = build_world(data_url, deadline, rid)
        if result["world"] is not None:
            with _cache_lock:
                _cache[digest] = dict(result["world"])
                _cache.move_to_end(digest)
                while len(_cache) > CACHE_MAX:
                    _cache.popitem(last=False)

    worker = threading.Thread(target=work, name=f"world-{rid}", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()) + GRACE_S)

    if worker.is_alive():
        # The budget is the promise. The thread is daemon and its own HTTP
        # timeouts are clamped to the same deadline, so it unwinds shortly;
        # if it does finish it populates the cache and the next identical
        # frame gets the real world for free.
        return fallback_world(
            f"wall-clock budget of {BUDGET_S:g}s exceeded; worker abandoned", rid), False

    if result.get("world") is None:
        return fallback_world(result.get("reason") or "pipeline returned no world", rid,
                              caption=result.get("caption") or "",
                              vision_ms=result.get("vision_ms") or 0,
                              nemotron_ms=result.get("nemotron_ms") or 0), False

    bump("ok")
    return result["world"], False


# --------------------------------------------------------------------------
# the frames themselves
# --------------------------------------------------------------------------

FRAME_DIR = os.path.join(pipeline.LOG_DIR, "frames")
FRAME_KEEP = 12                  # a short history, not an album


def keep_frame(raw, rid):
    """Save the photo this request was built from. Best effort, never raises."""
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        at = int(time.time() * 1000)
        path = os.path.join(FRAME_DIR, f"{at}.jpg")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
        names = sorted(f for f in os.listdir(FRAME_DIR) if f.endswith(".jpg"))
        for old in names[:-FRAME_KEEP]:
            try:
                os.remove(os.path.join(FRAME_DIR, old))
            except OSError:
                pass
        return at
    except Exception as e:
        print(f"[{rid}] could not keep the frame: {e}")
        return None


def frame_list():
    out = []
    try:
        for name in sorted(f for f in os.listdir(FRAME_DIR) if f.endswith(".jpg")):
            try:
                at = int(name[:-4])
            except ValueError:
                continue
            out.append({"id": at, "at": at / 1000.0,
                        "bytes": os.path.getsize(os.path.join(FRAME_DIR, name))})
    except OSError:
        pass
    return out


def frame_bytes(fid):
    try:
        name = f"{int(fid)}.jpg"
    except (TypeError, ValueError):
        return None
    path = os.path.join(FRAME_DIR, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(FRAME_DIR):
        return None                      # nothing clever with the id
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------
# text to speech (ElevenLabs)
# --------------------------------------------------------------------------

ELEVEN_BASE = "https://api.elevenlabs.io/v1"
TTS_DIR = os.path.join(pipeline.LOG_DIR, "tts")
# Flash is the cheap fast one and it SOUNDS it: distilled for latency, it
# reads a bark flat, which is most of what "robotic" meant. Multilingual v2 is
# the natural-sounding model -- 830 ms against Flash's 210 ms here, and a full
# credit per character instead of half. Both are affordable, because a line is
# synthesised once and then served from disk forever, and because no line is
# ever spoken twice there is nothing to re-bill.
TTS_MODEL = os.environ.get("JZ_TTS_MODEL", "eleven_multilingual_v2")
# 22 kHz at 32 kbps is a telephone. The thin, buzzy quality it adds is
# indistinguishable from the model sounding synthetic, and it costs nothing to
# fix: the bitrate is not billed, only the characters are.
TTS_FORMAT = os.environ.get("JZ_TTS_FORMAT", "mp3_44100_128")
# Half a credit per character on the distilled models, a full one otherwise.
TTS_CREDIT_RATE = 0.5 if ("flash" in TTS_MODEL or "turbo" in TTS_MODEL) else 1.0
# A hard ceiling on spend, expressed in CREDITS so it survives a model change.
# The account holds 10,000; this leaves a deliberate reserve. At ~25 characters
# a bark it is about 240 distinct lines, and cached lines never count.
TTS_BUDGET_CREDITS = float(os.environ.get("JZ_TTS_BUDGET_CREDITS", "6000"))
TTS_BUDGET_CHARS = int(os.environ.get(
    "JZ_TTS_BUDGET_CHARS", str(int(TTS_BUDGET_CREDITS / TTS_CREDIT_RATE))))
TTS_MAX_CHARS = 140              # a bark is eight words; anything longer is a bug
TTS_TIMEOUT_S = float(os.environ.get("JZ_TTS_TIMEOUT_S", "12"))
TTS_VOICES_TTL_S = 600.0

# Who sounds like what. Matched against the account's own library by FIRST
# NAME, because ElevenLabs names carry a description after it ("Callum - Husky
# Trickster"). Each list is a preference order, so a library without the first
# choice still casts sensibly; the ids are public defaults, used only if the
# library cannot be read at all.
#
# Cast from this account's 21 voices: a husky trickster shambles, an energetic
# one runs, a fierce warrior is the brute, and the peaceful locals get the
# reassuring one.
TTS_ROLE_VOICES = {
    "walker": (["Callum", "Brian", "Bill", "Arnold", "Josh"], "VR6AewLTigWG4xSOukaG"),
    "runner": (["Liam", "Charlie", "Chris", "Antoni", "Sam"], "ErXwobaYiN019PkySvjV"),
    "brute":  (["Harry", "Adam", "George", "Roger"], "pNInz6obpgDQGcFmaJgB"),
    "calm":   (["Sarah", "Alice", "River", "Bella", "Rachel"], "21m00Tcm4TlvDq8ikWAM"),
}
# Stability is a misleading name: LOW is not "more expressive", it is
# "wobbly", and wobble reads as synthetic just as much as monotone does. These
# sit in the middle and lean on similarity_boost and speaker_boost instead,
# which keep the voice's own timbre rather than flattening it towards the
# model's average. use_speaker_boost was simply missing before.
TTS_SETTINGS = {
    "walker": {"stability": 0.42, "similarity_boost": 0.82, "style": 0.45, "use_speaker_boost": True},
    "runner": {"stability": 0.38, "similarity_boost": 0.80, "style": 0.55, "use_speaker_boost": True},
    "brute":  {"stability": 0.50, "similarity_boost": 0.85, "style": 0.35, "use_speaker_boost": True},
    "calm":   {"stability": 0.55, "similarity_boost": 0.82, "style": 0.30, "use_speaker_boost": True},
}

# A bare fragment with no terminator is read flat, because there is no sentence
# for the model to shape. One character of punctuation buys the falling (or
# rising) intonation that makes it sound like speech instead of a label.
TTS_TERMINATORS = {"runner": "!"}


def tts_speakable(text, role="walker"):
    """What is actually sent to the model. The bubble keeps the original."""
    text = " ".join(str(text or "").split()).strip()
    if text and text[-1] not in ".!?\u2026:,;":
        text += TTS_TERMINATORS.get(role, ".")
    return text

_tts_lock = threading.Lock()
_tts_voice_cache = {"at": 0.0, "voices": [], "error": None}


def tts_key():
    return pipeline.load_env_value("ELEVENLABS_API_KEY")


def _tts_usage_path():
    return os.path.join(TTS_DIR, "usage.json")


def tts_usage():
    try:
        with open(_tts_usage_path(), "r", encoding="utf-8") as f:
            u = json.load(f)
            if isinstance(u, dict):
                u.setdefault("chars", 0); u.setdefault("requests", 0); u.setdefault("cached", 0)
                return u
    except Exception:
        pass
    return {"chars": 0, "requests": 0, "cached": 0}


def _tts_usage_add(chars=0, requests=0, cached=0):
    try:
        os.makedirs(TTS_DIR, exist_ok=True)
        u = tts_usage()
        u["chars"] += chars; u["requests"] += requests; u["cached"] += cached
        u["at"] = time.time()
        tmp = _tts_usage_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(u, f)
        os.replace(tmp, _tts_usage_path())
        return u
    except Exception as e:
        print(f"[tts] could not record usage: {e}")
        return tts_usage()


def tts_voice_list(force=False):
    """The account's voices. Cached; never raises."""
    key = tts_key()
    if not key:
        return [], "no ELEVENLABS_API_KEY"
    with _tts_lock:
        fresh = (time.time() - _tts_voice_cache["at"]) < TTS_VOICES_TTL_S
        if fresh and not force and _tts_voice_cache["voices"]:
            return _tts_voice_cache["voices"], _tts_voice_cache["error"]
    req = urllib.request.Request(ELEVEN_BASE + "/voices", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=TTS_TIMEOUT_S) as r:
            data = json.loads(r.read().decode("utf-8"))
        voices = [{"id": v.get("voice_id"), "name": v.get("name")}
                  for v in (data.get("voices") or []) if v.get("voice_id")]
        err = None if voices else "account has no voices"
    except Exception as e:
        voices, err = [], f"{type(e).__name__}: {e}"
        print(f"[tts] voice list failed: {err}")
    with _tts_lock:
        _tts_voice_cache["at"] = time.time()
        if voices:
            _tts_voice_cache["voices"] = voices
        _tts_voice_cache["error"] = err
    return voices, err


def tts_voice_for(role):
    """(voice_id, voice_name, how_it_was_chosen)."""
    prefs, fallback_id = TTS_ROLE_VOICES.get(role) or TTS_ROLE_VOICES["walker"]
    voices, _ = tts_voice_list()
    # "Callum - Husky Trickster" is matched by "Callum".
    by_first = {}
    for v in voices:
        first = (v["name"] or "").strip().split(" ")[0].strip().lower()
        by_first.setdefault(first, v)
    for want in prefs:
        v = by_first.get(want.lower())
        if v:
            return v["id"], v["name"], "cast"
    if voices:
        # Nothing preferred is in the library: spread the roles over whatever
        # is, deterministically, so a given role always sounds the same.
        idx = sum(ord(c) for c in role) % len(voices)
        return voices[idx]["id"], voices[idx]["name"], "substituted"
    return fallback_id, prefs[0], "default id (voice list unavailable)"


def tts_say(text, role="walker"):
    """
    Returns (mp3_bytes, meta) or (None, meta_with_error).
    A cached line costs nothing and is never re-billed.
    """
    text = " ".join(str(text or "").split()).strip()
    meta = {"role": role, "text": text, "model": TTS_MODEL, "cached": False}
    if not text:
        meta["error"] = "empty text"
        return None, meta
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]
        meta["text"] = text
        meta["note"] = f"truncated to {TTS_MAX_CHARS} characters"
    voice_id, voice_name, how = tts_voice_for(role)
    meta.update(voice=voice_name, voice_id=voice_id, voice_choice=how)
    speak = tts_speakable(text, role)
    meta["spoken_text"] = speak
    # The digest covers everything that changes the audio, so retuning any of
    # it re-synthesises rather than serving a stale file.
    digest = hashlib.sha256(
        f"{voice_id}|{TTS_MODEL}|{TTS_FORMAT}|{json.dumps(TTS_SETTINGS.get(role), sort_keys=True)}|{speak}"
        .encode("utf-8")).hexdigest()
    meta["key"] = digest[:16]
    path = os.path.join(TTS_DIR, digest + ".mp3")
    try:
        with open(path, "rb") as f:
            audio = f.read()
        if audio:
            meta["cached"] = True
            meta["bytes"] = len(audio)
            _tts_usage_add(cached=1)
            return audio, meta
    except OSError:
        pass

    key = tts_key()
    if not key:
        meta["error"] = ("no ELEVENLABS_API_KEY -- put it in jen-zombie/.env "
                         "and restart this service")
        return None, meta
    used = tts_usage()
    if used["chars"] + len(speak) > TTS_BUDGET_CHARS:
        meta["error"] = (f"character budget spent ({used['chars']}/{TTS_BUDGET_CHARS}); "
                         "raise JZ_TTS_BUDGET_CHARS to continue")
        return None, meta

    body = json.dumps({
        "text": speak,
        "model_id": TTS_MODEL,
        "voice_settings": TTS_SETTINGS.get(role, TTS_SETTINGS["walker"]),
    }).encode("utf-8")
    url = f"{ELEVEN_BASE}/text-to-speech/{voice_id}?output_format={TTS_FORMAT}"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TTS_TIMEOUT_S) as r:
            audio = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        meta["error"] = f"HTTP {e.code} from ElevenLabs: {detail}"
        print(f"[tts] {meta['error']}")
        return None, meta
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        print(f"[tts] {meta['error']}")
        return None, meta
    meta["ms"] = int((time.monotonic() - t0) * 1000)
    if not audio:
        meta["error"] = "ElevenLabs returned no audio"
        return None, meta
    try:
        os.makedirs(TTS_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(audio)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[tts] could not cache {digest[:12]}: {e}")
    u = _tts_usage_add(chars=len(speak), requests=1)
    meta["bytes"] = len(audio)
    meta["spent_chars"] = u["chars"]
    meta["spent_credits"] = round(u["chars"] * TTS_CREDIT_RATE)
    print(f"[tts] {role} {voice_name!r} {len(speak)}ch {meta['ms']}ms "
          f"{len(audio)}B  (budget {u['chars']}/{TTS_BUDGET_CHARS} chars, "
          f"~{round(u['chars'] * TTS_CREDIT_RATE)} credits)")
    return audio, meta


def tts_document():
    voices, err = tts_voice_list()
    roles = {}
    for role in TTS_ROLE_VOICES:
        vid, name, how = tts_voice_for(role)
        roles[role] = {"voice": name, "id": vid, "how": how}
    u = tts_usage()
    cached_files = 0
    try:
        cached_files = len([f for f in os.listdir(TTS_DIR) if f.endswith(".mp3")])
    except OSError:
        pass
    return {"ok": bool(tts_key()), "key_present": bool(tts_key()),
            "model": TTS_MODEL, "format": TTS_FORMAT,
            "voices": voices, "voices_error": err, "roles": roles,
            "usage": u, "budget_chars": TTS_BUDGET_CHARS,
            "credits_estimate": round(u["chars"] * 0.5),
            "cached_lines": cached_files}


# --------------------------------------------------------------------------
# trace: the model traffic, in readable form
# --------------------------------------------------------------------------

TRACE_MAX = 40                  # exchanges returned at most
TRACE_TAIL_BYTES = 400_000      # how far back in the log we bother to read
TRACE_TEXT_MAX = 6000           # per field, so one huge reply cannot fill a page


def _clip(text, limit=TRACE_TEXT_MAX):
    t = "" if text is None else str(text)
    return t if len(t) <= limit else t[:limit] + f"\n… [{len(t) - limit} more characters]"


def _prompt_text(body):
    """The human-readable prompt out of either provider's request body."""
    out = []
    try:
        # NVIDIA / OpenAI shape: messages[{role, content}]
        for msg in (body.get("messages") or []):
            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        parts.append("[image: " + str(part.get("image_url", {}).get("url", ""))[:60] + "]")
                content = "\n".join(parts)
            out.append(f"[{msg.get('role', '?')}]\n{content}")
        # Gemini shape: contents[{parts:[{text|inline_data}]}], system separate
        sys_inst = body.get("systemInstruction") or body.get("system_instruction")
        if sys_inst:
            texts = [p.get("text", "") for p in (sys_inst.get("parts") or []) if isinstance(p, dict)]
            if texts:
                out.insert(0, "[system]\n" + "\n".join(texts))
        for item in (body.get("contents") or []):
            parts = []
            for part in (item.get("parts") or []):
                if not isinstance(part, dict):
                    continue
                if "text" in part:
                    parts.append(part["text"])
                elif "inline_data" in part:
                    d = part["inline_data"]
                    parts.append(f"[image: {d.get('mime_type', 'image')} {d.get('data', '')}]")
            out.append(f"[{item.get('role', 'user')}]\n" + "\n".join(parts))
    except Exception as e:
        out.append(f"[unreadable request body: {type(e).__name__}: {e}]")
    return "\n\n".join(x for x in out if x)


def _reply_text(raw):
    """What the model actually said, out of either provider's reply."""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except Exception:
        return str(raw)
    try:
        ch = parsed.get("choices")
        if ch:
            return ch[0].get("message", {}).get("content") or ""
        cand = parsed.get("candidates")
        if cand:
            parts = cand[0].get("content", {}).get("parts") or []
            return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if "error" in parsed:
            return json.dumps(parsed["error"], ensure_ascii=False)
    except Exception:
        pass
    return json.dumps(parsed, ensure_ascii=False)


def _read_log_tail():
    """Last records from pipeline's jsonl. Returns [] if there is no log yet."""
    try:
        size = os.path.getsize(pipeline.LOG_PATH)
        with open(pipeline.LOG_PATH, "rb") as f:
            if size > TRACE_TAIL_BYTES:
                f.seek(size - TRACE_TAIL_BYTES)
                f.readline()                  # discard the partial first line
            data = f.read()
    except OSError:
        return []
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue                          # a half-written line, nothing more
    return out


def trace_document(limit=12):
    """
    Pair each request with its reply, newest first. One entry per model call:
    what went to Gemini or Nemotron, and what came back, both as text.
    """
    limit = max(1, min(TRACE_MAX, int(limit or 12)))
    records = _read_log_tail()
    pending = {}          # (label, attempt) -> request record
    exchanges = []
    for rec in records:
        kind, label = rec.get("kind"), rec.get("label") or "?"
        key = (label, rec.get("attempt"))
        if kind == "request":
            pending[key] = rec
        elif kind == "reply":
            req = pending.pop(key, None)
            exchanges.append({
                "at": rec.get("ts"),
                "model": (req or {}).get("model") or rec.get("model") or "",
                # The log labels legs "vision:gemini" / "nemotron"; name them
                # the way someone reading the page would.
                "leg": ("vision · Gemini" if "gemini" in label
                        else "vision · Llama" if "vision" in label
                        else "world · Nemotron" if "nemotron" in label else label),
                "label": label,
                "attempt": rec.get("attempt"),
                "status": rec.get("status"),
                "elapsed_ms": rec.get("elapsed_ms"),
                "error": rec.get("error"),
                "sent": _clip(_prompt_text((req or {}).get("body") or {})),
                "received": _clip(_reply_text(rec.get("raw"))),
            })
    exchanges.reverse()
    return {"ok": True, "count": len(exchanges[:limit]), "total_seen": len(exchanges),
            "log": pipeline.LOG_PATH, "now": now_ms(),
            "exchanges": exchanges[:limit]}


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

def _probe(url, headers, timeout=PROBE_TIMEOUT_S):
    """GET a cheap endpoint. Returns (reachable, status|None, ms, error|None)."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.status, int((time.monotonic() - t0) * 1000), None
    except urllib.error.HTTPError as e:
        ms = int((time.monotonic() - t0) * 1000)
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200].strip()
        except Exception:
            pass
        # A 4xx here means the endpoint answered us: the key reached it and was
        # rejected. That is "not reachable with this key", and worth saying so.
        return False, e.code, ms, f"HTTP {e.code}: {body or e.reason}"
    except Exception as e:
        return False, None, int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {e}"


def key_health():
    """Both keys: present? reachable? Cached for HEALTH_TTL_S so polling is free."""
    with _health_lock:
        cached = _health_cached["value"]
        if cached is not None and (time.monotonic() - _health_cached["at"]) < HEALTH_TTL_S:
            return cached, True

    nvidia_key = pipeline.load_api_key()
    gemini_key = pipeline.load_env_value("GEMINI_API_KEY")
    out = {
        "nvidia": {"present": bool(nvidia_key), "reachable": False,
                   "status": None, "ms": 0, "error": None if nvidia_key else "key not found"},
        "gemini": {"present": bool(gemini_key), "reachable": False,
                   "status": None, "ms": 0, "error": None if gemini_key else "key not found"},
    }

    def probe_into(slot, url, headers):
        ok, status, ms, err = _probe(url, headers)
        out[slot].update(reachable=ok, status=status, ms=ms, error=err)

    threads = []
    if nvidia_key:
        threads.append(threading.Thread(target=probe_into, args=(
            "nvidia", NVIDIA_MODELS_URL,
            {"Authorization": f"Bearer {nvidia_key}", "Accept": "application/json"})))
    if gemini_key:
        threads.append(threading.Thread(target=probe_into, args=(
            "gemini", GEMINI_MODELS_URL,
            {"x-goog-api-key": gemini_key, "Accept": "application/json"})))
    for t in threads:
        t.daemon = True
        t.start()
    for t in threads:
        t.join(PROBE_TIMEOUT_S + 1.0)

    with _health_lock:
        _health_cached["value"] = out
        _health_cached["at"] = time.monotonic()
    return out, False


def health_document():
    keys, cached = key_health()
    with _stats_lock:
        stats = dict(_stats)
    with _cache_lock:
        cache_size = len(_cache)
    provider = pipeline.VISION_PROVIDER
    vision_model = pipeline.GEMINI_MODEL if provider == "gemini" else pipeline.VISION_MODEL
    both_ok = all(keys[k]["present"] and keys[k]["reachable"] for k in ("nvidia", "gemini"))
    # The active legs are what decides whether a real world can be built now;
    # "ok" is the contract's question (both keys), kept separate on purpose.
    active_ok = keys["nvidia"]["present"] and keys["nvidia"]["reachable"] and (
        provider != "gemini" or (keys["gemini"]["present"] and keys["gemini"]["reachable"]))
    return {
        "ok": both_ok,
        "active_legs_ok": active_ok,
        "keys": keys,
        "keys_probe_cached": cached,
        "config": {
            "vision_provider": provider,
            "vision_model": vision_model,
            "nemotron_model": pipeline.NEMOTRON_MODEL,
            "reasoning_effort": pipeline.NEMOTRON_REASONING_EFFORT,
            "budget_s": BUDGET_S,
        },
        "cache": {"entries": cache_size, "max": CACHE_MAX, "hits": stats.get("cache_hits", 0)},
        "stats": stats,
        "uptime_s": round(time.monotonic() - _started, 1),
        "now": now_ms(),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class WorldHandler(BaseHTTPRequestHandler):
    server_version = "jz-world/1.0"
    protocol_version = "HTTP/1.1"     # every reply below carries Content-Length

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}")

    # -- replies -----------------------------------------------------------
    def _send(self, code, payload, close=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # The phone may reach us through the Pi or, while debugging,
            # straight from a page on another origin. Costs nothing.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            print("[http] client went away before the reply was written")

    def _send_bytes(self, code, body, ctype, cache_s=0):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", f"max-age={cache_s}" if cache_s else "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            print("[http] client went away before the audio was written")

    # -- body ---------------------------------------------------------------
    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            # nginx and friends may re-frame the upload; BaseHTTPRequestHandler
            # does not decode it for us.
            chunks, total = [], 0
            while True:
                line = self.rfile.readline(65536).strip()
                if b";" in line:
                    line = line.split(b";", 1)[0]
                try:
                    n = int(line, 16)
                except ValueError:
                    raise ValueError(f"bad chunk size {line[:32]!r}")
                if n == 0:
                    while True:                      # trailers
                        t = self.rfile.readline(65536)
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                total += n
                if total > MAX_BODY:
                    raise ValueError(f"body over {MAX_BODY} bytes")
                chunks.append(self.rfile.read(n))
                self.rfile.read(2)                   # trailing CRLF
            return b"".join(chunks)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if n > MAX_BODY:
            raise ValueError(f"body over {MAX_BODY} bytes")
        return self.rfile.read(n) if n > 0 else b""

    # -- routes -------------------------------------------------------------
    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/health":
                self._send(200, health_document())
            elif path == "/frames":
                self._send(200, {"ok": True, "frames": frame_list()})
            elif path == "/frame":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                data = frame_bytes((q.get("id") or [""])[0])
                if data is None:
                    self._send(404, {"error": "no such frame"})
                else:
                    self._send_bytes(200, data, "image/jpeg", cache_s=86400)
            elif path == "/voices":
                # ?force=1 re-reads the library immediately, for the moment
                # after a key's permissions change (the list is cached 10 min).
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                if (q.get("force") or ["0"])[0] == "1":
                    tts_voice_list(force=True)
                self._send(200, tts_document())
            elif path == "/say":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                text = (q.get("text") or [""])[0]
                role = (q.get("role") or ["walker"])[0].strip().lower()
                if role not in TTS_ROLE_VOICES:
                    role = "walker"
                audio, meta = tts_say(text, role)
                if audio is None:
                    # The game shows the line as text when this fails, so the
                    # reason has to come back rather than a bare status.
                    err = meta.get("error") or ""
                    self._send(503 if ("budget" in err or "ELEVENLABS" in err) else 502, meta)
                else:
                    self._send_bytes(200, audio, "audio/mpeg", cache_s=86400)
            elif path == "/trace":
                q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                try:
                    n = int((q.get("n") or ["12"])[0])
                except Exception:
                    n = 12
                self._send(200, trace_document(n))
            elif path == "/":
                self._send(200, {"service": "jz-world", "post": "/world (image/jpeg)",
                                 "health": "/health", "trace": "/trace?n=12"})
            elif path == "/world":
                self._send(405, {"error": "POST the JPEG bytes to /world; GET is not it"})
            else:
                self._send(404, {"error": f"no route {path}", "routes": ["/world", "/health", "/trace", "/say", "/voices", "/frames", "/frame"]})
        except Exception as e:
            print(f"[http] GET {path} blew up: {type(e).__name__}: {e}")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/world":
            self._send(404, {"error": f"no route {path}", "routes": ["/world", "/health", "/trace", "/say", "/voices", "/frames", "/frame"]})
            return

        rid = next_rid()
        bump("requests")
        t0 = time.monotonic()
        try:
            raw = self._read_body()
        except ValueError as e:
            print(f"[{rid}] rejected upload: {e}")
            self._send(413 if "over" in str(e) else 400, {"error": str(e)}, close=True)
            return
        except Exception as e:
            print(f"[{rid}] could not read body: {type(e).__name__}: {e}")
            self._send(400, {"error": f"could not read body: {e}"}, close=True)
            return

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip() or "(none)"
        print("=" * 72)
        print(f"[{rid}] POST /world  {len(raw)} bytes  Content-Type: {ctype}")

        if not raw:
            print(f"[{rid}] empty body -- nothing to caption")
            self._send(400, {"error": "empty body; POST the JPEG bytes as the request body"})
            return

        keep_frame(raw, rid)
        try:
            world, cached = world_for_image(raw, rid)
        except Exception as e:
            # Last line of defence. A world, never a bare 500.
            print(f"[{rid}] unexpected {type(e).__name__}: {e}")
            world, cached = fallback_world(f"unexpected {type(e).__name__}: {e}", rid), False

        total_ms = int((time.monotonic() - t0) * 1000)
        print(f"[{rid}] -> biome={world['biome']} name={world['name']!r} "
              f"fallback={world['fallback']} cached={cached} total={total_ms} ms")
        self._send(200, world)


def local_addresses():
    """Which of our addresses faces the Pi, and which faces the internet."""
    found = []
    for probe, label in ((PI_ADDR, "wired link to the Pi"), ("8.8.8.8", "default route")):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 9))
            ip = s.getsockname()[0]
            if ip not in [f[0] for f in found]:
                found.append((ip, label))
        except Exception:
            pass
        finally:
            s.close()
    return found


def main():
    global BUDGET_S
    ap = argparse.ArgumentParser(description="photo -> world HTTP service")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--budget", type=float, default=BUDGET_S,
                    help="hard wall-clock seconds per request (default 25)")
    args = ap.parse_args()
    BUDGET_S = args.budget

    provider = pipeline.VISION_PROVIDER
    print(f"vision:         {provider} "
          f"({pipeline.GEMINI_MODEL if provider == 'gemini' else pipeline.VISION_MODEL})")
    print(f"nemotron model: {pipeline.NEMOTRON_MODEL}"
          f"  (reasoning_effort={pipeline.NEMOTRON_REASONING_EFFORT})")
    print(f"budget:         {BUDGET_S:g}s per request")
    print(f"log file:       {pipeline.LOG_PATH}")

    httpd = ThreadingHTTPServer((args.host, args.port), WorldHandler)
    httpd.daemon_threads = True
    print(f"listening:      http://{args.host}:{args.port}   (POST /world, GET /health)")
    for ip, label in local_addresses():
        print(f"                http://{ip}:{args.port}/world   <- {label}")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ninterrupted.")
    finally:
        httpd.server_close()
        with _stats_lock:
            print(f"served: {_stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

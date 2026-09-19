#!/usr/bin/env python3
"""
Establish -- empirically, not by assumption -- which endpoint and auth form the
GEMINI_API_KEY in .env actually works with, and which models take image input.

The key reportedly starts "AQ." rather than the usual "AIza", and came with
project number 61575411390, so a Vertex AI path is plausible.  This tries every
combination and reports raw status + error text.  Nothing raises; the key is
never printed.

    python3 probe_gemini.py
"""
import importlib.util, json, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_s = importlib.util.spec_from_file_location("pipeline", os.path.join(HERE, "pipeline.py"))
P = importlib.util.module_from_spec(_s); _s.loader.exec_module(P)

PROJECT_NUMBER = "61575411390"
GL = "https://generativelanguage.googleapis.com"


def load_key(name="GEMINI_API_KEY"):
    v = os.environ.get(name, "").strip()
    if v:
        return v, "environment"
    try:
        for ln in open(os.path.join(HERE, ".env"), encoding="utf-8"):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, val = ln.split("=", 1)
            if k.strip() == name:
                val = val.strip().strip('"').strip("'")
                if val:
                    return val, ".env"
    except Exception as e:
        print(f"[key] could not read .env: {e}")
    return None, None


def try_get(label, url, headers, timeout=30):
    """GET; returns (status, body_snippet, ms).  Never raises, never logs the key."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            ms = int((time.monotonic() - t0) * 1000)
            print(f"  {label:52s} {r.status}  {ms:6d} ms   OK")
            return r.status, body, ms
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        ms = int((time.monotonic() - t0) * 1000)
        msg = ""
        try:
            msg = json.loads(body).get("error", {}).get("message", "")[:110]
        except Exception:
            msg = body[:110].replace("\n", " ")
        print(f"  {label:52s} {e.code}  {ms:6d} ms   {msg}")
        return e.code, body, ms
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        print(f"  {label:52s} ---  {ms:6d} ms   {type(e).__name__}: {e}")
        return None, "", ms


def main():
    key, src = load_key()
    if not key:
        print("GEMINI_API_KEY not found in environment or .env.")
        print("Add it to .env next to this script, then re-run.")
        return 2
    print(f"key loaded from {src}: len={len(key)} prefix={key[:3]!r}")
    looks_vertex = not key.startswith("AIza")
    print(f"format: {'NOT the AIza API-key form -- Vertex/OAuth is plausible' if looks_vertex else 'standard AIza API key'}\n")

    print("A. generativelanguage, key as x-goog-api-key header")
    a = try_get("v1beta/models", f"{GL}/v1beta/models", {"x-goog-api-key": key})
    a1 = try_get("v1/models", f"{GL}/v1/models", {"x-goog-api-key": key})

    print("\nB. generativelanguage, key as ?key= query parameter")
    b = try_get("v1beta/models?key=", f"{GL}/v1beta/models?key={key}", {})
    b1 = try_get("v1/models?key=", f"{GL}/v1/models?key={key}", {})

    print("\nC. generativelanguage, key as OAuth bearer token")
    c = try_get("v1beta/models (Bearer)", f"{GL}/v1beta/models", {"Authorization": f"Bearer {key}"})

    print("\nD. Vertex AI, key as bearer (project 61575411390)")
    vert = "https://aiplatform.googleapis.com/v1"
    d = try_get("us-central1 publishers/google/models",
                f"{vert}/projects/{PROJECT_NUMBER}/locations/us-central1/publishers/google/models",
                {"Authorization": f"Bearer {key}"})
    d1 = try_get("global publishers/google/models",
                 f"{vert}/projects/{PROJECT_NUMBER}/locations/global/publishers/google/models",
                 {"Authorization": f"Bearer {key}"})

    print("\nE. Vertex AI express / aiplatform with x-goog-api-key")
    e = try_get("aiplatform v1beta1 openapi models",
                "https://aiplatform.googleapis.com/v1beta1/publishers/google/models",
                {"x-goog-api-key": key})

    winners = [(lbl, res) for lbl, res in
               [("A header v1beta", a), ("A header v1", a1), ("B query v1beta", b),
                ("B query v1", b1), ("C bearer GL", c), ("D vertex us-central1", d),
                ("D vertex global", d1), ("E aiplatform header", e)]
               if res[0] == 200]
    print("\n" + "=" * 70)
    if not winners:
        print("NO endpoint/auth combination worked.")
        print("If every response was 401/403 with 'API key not valid' or 'invalid")
        print("authentication credential', this is not a generativelanguage API key.")
        print("An 'AQ.'-prefixed credential with a project NUMBER is characteristic of")
        print("a Vertex AI / OAuth access token, which needs a google-auth flow")
        print("(gcloud auth application-default login) rather than a static header.")
        return 1

    lbl, (status, body, ms) = winners[0]
    print(f"WORKING: {lbl}  ({ms} ms)\n")
    try:
        models = json.loads(body).get("models", [])
    except Exception:
        models = []
    print(f"{len(models)} models returned. Ones that accept image input:")
    vision = []
    for m in models:
        name = m.get("name", "")
        methods = m.get("supportedGenerationMethods", []) or []
        if "generateContent" not in methods:
            continue
        blob = json.dumps(m).lower()
        if any(t in blob for t in ("vision", "image")) or "gemini" in name.lower():
            vision.append(name)
            print(f"   {name:56s} in={m.get('inputTokenLimit')} out={m.get('outputTokenLimit')}")
    flash = [n for n in vision if "flash" in n.lower()]
    print(f"\nFlash-tier candidates: {flash or '(none found)'}")
    try:
        with open(os.path.join(HERE, "logs", "gemini_probe.json"), "w") as f:
            json.dump({"working": lbl, "vision_models": vision, "flash": flash,
                       "all_models": [m.get("name") for m in models]}, f, indent=2)
        print("[saved] logs/gemini_probe.json")
    except Exception as ex:
        print(f"[save] ignored: {ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

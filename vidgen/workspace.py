"""API-key checks, key removal and the workspace reset.

Three things the dashboard asks for:

  * check_ai33 / check_gemini - one free, read-only call per key, so a key that
    is mistyped, expired or out of quota is reported when it is pasted, not
    half-way through a video.
  * remove_keys - every saved copy of both keys, gone.
  * reset_workspace - the buyer's own work, gone; what the tool ships with
    (built-in character references and styles), the code, the API keys and the
    cloned voices are left alone. Opt-in extras: Chrome logins and the
    finished-video folders of the deleted projects.

Deleting here is permanent on purpose: the reset exists so a copy can be handed
to someone else, and a `_trash` folder would hand them everything anyway.
"""
import json
import os
import shutil
import time

import requests

# Shipped with the tool - never deleted by a reset.
FACTORY_REFS = {
    "single_stickman_ref.png", "stick_bare.png", "roman_toga_ref.png",
    "roman_soldier_ref.png", "char_sheet.png", "ancient_rome_citizen_rustic.png",
    "ancient_rome_cast_sheet.png", "ancient_rome_woman_rustic.png",
    "ancient_rome_soldier_rustic.png", "clean_stickman_ref.png",
}
FACTORY_LOOKS = {"2d.json", "2d_mixed.json", "2d_modern.json", "3d.json",
                 "realistic.json", "vox.json"}

AI33 = "https://api.ai33.pro"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"


# ------------------------------------------------------------- key checks

def check_ai33(key):
    """('valid'|'invalid'|'unknown', message) - one free voice-list call."""
    try:
        r = requests.get(f"{AI33}/v3/voices", headers={"xi-api-key": key},
                         params={"provider": "elevenlabs", "page_size": 1}, timeout=20)
    except Exception as exc:
        return "unknown", f"could not reach AI33 to check it ({str(exc)[:60]})"
    if r.status_code in (401, 403):
        return "invalid", "AI33 refused this key"
    if r.status_code == 200:
        try:
            if r.json().get("success") is False:
                return "invalid", "AI33 refused this key"
        except ValueError:
            pass
        return "valid", "AI33 key verified"
    return "unknown", f"AI33 answered HTTP {r.status_code}; key saved without a check"


def check_gemini(key):
    """('valid'|'invalid'|'unknown', message) - one free model-list call."""
    try:
        r = requests.get(GEMINI, params={"key": key, "pageSize": 1}, timeout=20)
    except Exception as exc:
        return "unknown", f"could not reach Google to check it ({str(exc)[:60]})"
    if r.status_code == 200:
        return "valid", "Gemini key verified"
    body = r.text[:400]
    if r.status_code in (400, 401, 403) and ("API_KEY" in body or "API key" in body
                                            or r.status_code in (401, 403)):
        return "invalid", "Google refused this Gemini key"
    if r.status_code == 429:
        return "valid", "Gemini key accepted (its quota is busy right now)"
    return "unknown", f"Google answered HTTP {r.status_code}; key saved without a check"


# ------------------------------------------------------------- remove keys

def remove_keys(paths):
    """Delete every saved copy of the API keys. Returns how many were removed."""
    removed = 0
    for path in sorted(set(paths)):
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    for var in ("AI33_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(var, None)
    return removed


# ------------------------------------------------------------ the reset

def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _remove(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def plan(where, chrome=False, videos=False):
    """[(label, path)] that a reset would delete, built from `where`:
    projects, scripts, refs, looks, data_dir, queue_file, profiles_dir,
    pub_dirs (list), plus 'extra_files' (list)."""
    out = []
    projects_dir = where.get("projects")
    project_names = []
    if projects_dir and os.path.isdir(projects_dir):
        for n in sorted(os.listdir(projects_dir)):
            path = os.path.join(projects_dir, n)
            if os.path.isdir(path):
                out.append(("project", path))
                if not n.startswith(("_", ".")):
                    project_names.append(n)
    sd = where.get("scripts")
    if sd and os.path.isdir(sd):
        for n in sorted(os.listdir(sd)):
            out.append(("script", os.path.join(sd, n)))
    rd = where.get("refs")
    if rd and os.path.isdir(rd):
        for n in sorted(os.listdir(rd)):
            if n not in FACTORY_REFS:
                out.append(("reference", os.path.join(rd, n)))
    ld = where.get("looks")
    if ld and os.path.isdir(ld):
        for n in sorted(os.listdir(ld)):
            if n.lower().endswith(".json") and n not in FACTORY_LOOKS:
                out.append(("style", os.path.join(ld, n)))
            elif n == "_trash":
                out.append(("style", os.path.join(ld, n)))
    dd = where.get("data_dir")
    if dd:
        logs = os.path.join(dd, "logs")
        if os.path.isdir(logs):
            out.append(("log", logs))
    for f in where.get("extra_files") or []:
        if f and os.path.isfile(f):
            out.append(("setting", f))
    if chrome:
        pd = where.get("profiles_dir")
        if pd and os.path.isdir(pd):
            for n in sorted(os.listdir(pd)):
                path = os.path.join(pd, n)
                if os.path.isdir(path):
                    out.append(("chrome login", path))
    if videos:
        for base in where.get("pub_dirs") or []:
            for n in project_names:
                path = os.path.join(base, n)
                if os.path.isdir(path):
                    out.append(("finished video", path))
    return out


def summary(where, chrome=False, videos=False):
    items = plan(where, chrome, videos)
    groups = {}
    for label, path in items:
        g = groups.setdefault(label, {"count": 0, "bytes": 0})
        g["count"] += 1
        g["bytes"] += _size(path)
    return {"groups": groups, "total_bytes": sum(g["bytes"] for g in groups.values())}


def reset_workspace(where, chrome=False, videos=False):
    """Delete the buyer's work. Returns {'removed': {label: n}, 'failed': [...]}."""
    removed, failed = {}, []
    for label, path in plan(where, chrome, videos):
        try:
            _remove(path)
            removed[label] = removed.get(label, 0) + 1
        except OSError as exc:
            failed.append(f"{os.path.basename(path)}: {exc.strerror or exc}")
    qf = where.get("queue_file")
    if qf:
        try:
            with open(qf, "w", encoding="utf-8") as f:
                json.dump([], f)
        except OSError:
            pass
    for d in (where.get("projects"), where.get("scripts")):
        if d:
            os.makedirs(d, exist_ok=True)
    if chrome and where.get("profiles_dir"):
        os.makedirs(where["profiles_dir"], exist_ok=True)
    return {"removed": removed, "failed": failed,
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}

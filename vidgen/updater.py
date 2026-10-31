"""
In-app updater - FlowStudio style: the app checks GitHub Releases itself,
downloads the new Setup.exe and hands over to the installer. No .bat files,
no terminal.

The release repo (mohdkhan-lab/Mk-tool) is PRIVATE, so downloading a release
needs a GitHub token. The user pastes a fine-grained read-only token once in
the update dialog; it lives in the user data folder (never in the install
folder, so an upgrade keeps it) and is only ever sent to api.github.com.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error

REPO = "mohdkhan-lab/Mk-tool"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = "MKStoryStudio-Updater"

_state_lock = threading.Lock()
_state = {"phase": "idle", "downloaded": 0, "total": 0, "file": "", "error": ""}
_cache = {"at": 0.0, "data": None}


# ---------------------------------------------------------------- the token

def _token_path():
    try:
        import userdata
        base = userdata.base()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "update_token.txt")


def load_token():
    try:
        with open(_token_path(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def has_token():
    return bool(load_token())


def save_token(token):
    token = (token or "").strip()
    try:
        if token:
            with open(_token_path(), "w", encoding="utf-8") as f:
                f.write(token)
        elif os.path.exists(_token_path()):
            os.remove(_token_path())
    except OSError as e:
        return {"ok": False, "error": str(e)[:200]}
    _cache["at"] = 0.0
    _cache["data"] = None
    return {"ok": True, "has_token": bool(token)}


# ------------------------------------------------------------------- checks

def _current_version():
    try:
        import version as _v
        return _v.APP_VERSION
    except Exception:
        return "0.0.0"


def _parse(v):
    parts = []
    for p in str(v).strip().lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check(force=False):
    """Latest-release check, cached for 5 minutes."""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["at"] < 300:
        return _cache["data"]
    cur = _current_version()
    out = {"ok": True, "current": cur, "latest": None, "available": False,
           "has_token": has_token(), "asset": None, "notes": ""}
    req = urllib.request.Request(API_LATEST)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/vnd.github+json")
    tok = load_token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rel = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404) and not tok:
            out.update(ok=False, error="token_required",
                       message="Repo private hai - update check ke liye ek "
                               "GitHub token chahiye. Neeche token paste karo.")
        else:
            out.update(ok=False, error=f"http_{e.code}",
                       message=f"GitHub se jawab: HTTP {e.code}")
        return out
    except Exception as e:
        out.update(ok=False, error="network",
                   message=f"Update check fail ho gaya: {e}"[:200])
        return out

    latest = str(rel.get("tag_name") or "").lstrip("v")
    out["latest"] = latest
    out["notes"] = (rel.get("name") or "")[:120]
    asset = None
    for a in rel.get("assets") or []:
        name = a.get("name") or ""
        if name.lower().endswith(".exe") and "setup" in name.lower():
            asset = {"id": a.get("id"), "name": name,
                     "size": a.get("size") or 0, "url": a.get("url")}
            break
    out["asset"] = asset
    out["available"] = bool(latest and asset and _parse(latest) > _parse(cur))
    _cache["at"] = now
    _cache["data"] = out
    return out


# ---------------------------------------------------------------- download

def status():
    with _state_lock:
        s = dict(_state)
    s["percent"] = int(100 * s["downloaded"] / s["total"]) if s.get("total") else 0
    return {"ok": True, **s}


def _set_state(**kw):
    with _state_lock:
        _state.update(kw)


def start_download():
    info = check()
    if not info.get("available"):
        return {"ok": False, "error": "no_update",
                "message": "Koi naya update uplabdh nahi hai."}
    with _state_lock:
        if _state["phase"] == "downloading":
            return {"ok": True, "phase": "downloading"}
    asset = info["asset"]
    _set_state(phase="downloading", downloaded=0, total=asset["size"],
               error="", file="")
    threading.Thread(target=_download_worker, args=(asset,),
                     daemon=True).start()
    return {"ok": True, "phase": "downloading"}


def _download_worker(asset):
    dest = os.path.join(tempfile.gettempdir(), asset["name"])
    headers = {"User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    tok = load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        try:
            import requests
        except ImportError:
            requests = None
        if requests is not None:
            # requests drops the Authorization header when the redirect
            # leaves api.github.com, so the token never reaches the
            # storage host.
            with requests.get(asset["url"], headers=headers, stream=True,
                              timeout=60) as r:
                r.raise_for_status()
                _set_state(total=int(r.headers.get("Content-Length")
                                     or asset["size"] or 0))
                done = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=262144):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        _set_state(downloaded=done)
        else:
            req = urllib.request.Request(asset["url"])
            for k, v in headers.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(dest, "wb") as f:
                _set_state(total=int(resp.headers.get("Content-Length")
                                     or asset["size"] or 0))
                done = 0
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    _set_state(downloaded=done)
        _set_state(phase="ready", file=dest)
    except Exception as e:
        _set_state(phase="error", error=str(e)[:200])


# ----------------------------------------------------------------- install

def install():
    with _state_lock:
        dest = _state.get("file") or ""
        phase = _state.get("phase")
    if phase != "ready" or not dest or not os.path.isfile(dest):
        return {"ok": False, "error": "not_downloaded",
                "message": "Pehle update download karo."}
    try:
        subprocess.Popen([dest], close_fds=True)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    _set_state(phase="installing")
    # Give this HTTP reply time to reach the page, then end the server
    # process so the installer can overwrite the app files freely.
    threading.Timer(1.5, os._exit, args=(0,)).start()
    return {"ok": True, "phase": "installing"}

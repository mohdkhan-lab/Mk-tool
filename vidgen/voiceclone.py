"""Voice cloning through AI33 (the same account the narration already uses).

A 3-30 second sample in -> a `clone_...` voice that joins the narrator list.

AI33's documented v3 API:
    POST   /v3/text-to-speech/voice-clone          voice_name, audio_file (<=10MB)
    DELETE /v3/text-to-speech/voice-clone/{id}
    GET    /v3/voices?provider=clone               (already used for the list)
Their own clone page enforces 3-30 s of .mp3/.wav; a clone costs 333 credits.

The sample is cleaned here first, because clone quality is sample quality:
any format the browser hands over (mp3, wav, m4a, ogg, a webm mic recording)
becomes mono 44.1 kHz mp3, leading/trailing silence is trimmed, loudness is
evened out, and anything past 30 s is cut.
"""
import os
import re
import shutil
import subprocess
import tempfile

import requests

API = "https://api.ai33.pro"
MIN_SECONDS = 3.0
MAX_SECONDS = 30.0
MAX_UPLOAD = 25 * 1024 * 1024      # raw upload; the cleaned mp3 is far smaller
MAX_SAMPLE = 10 * 1024 * 1024      # AI33's limit
CLONE_CREDITS = 333

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class CloneError(Exception):
    """A message that can be shown to the user as it is."""


def _tool(name):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(here, "tools", name + (".exe" if os.name == "nt" else ""))
    return local if os.path.isfile(local) else (shutil.which(name) or name)


def _duration(path):
    try:
        out = subprocess.run([_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True,
                             timeout=60, creationflags=_NO_WINDOW).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def _samples_dir():
    try:
        import userdata
        base = userdata.base()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "voice_samples")
    os.makedirs(d, exist_ok=True)
    return d


def clean_name(name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    name = re.sub(r"[^\w \-().'ऀ-ॿ]", "", name)[:60].strip()
    if len(name) < 2:
        raise CloneError("Give the voice a name (at least 2 characters).")
    return name


def prepare_sample(raw, filename="sample"):
    """Raw uploaded bytes -> (clean mp3 path, seconds, notes[])."""
    if not raw:
        raise CloneError("No audio received. Choose a file or record a sample.")
    if len(raw) > MAX_UPLOAD:
        raise CloneError("That file is over 25 MB. Use a shorter sample (10-30 seconds is ideal).")
    ext = os.path.splitext(filename or "")[1].lower() or ".bin"
    tmp = tempfile.mkdtemp(prefix="sf_clone_")
    src = os.path.join(tmp, "in" + ext)
    out = os.path.join(tmp, "sample.mp3")
    with open(src, "wb") as f:
        f.write(raw)
    original = _duration(src)
    # Trim silence at both ends, even out loudness, cap at 30 s.
    af = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,"
          "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.25,areverse,"
          "loudnorm=I=-18:TP=-1.5:LRA=11")
    cmd = [_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", src,
           "-vn", "-af", af, "-t", str(MAX_SECONDS), "-ac", "1", "-ar", "44100",
           "-c:a", "libmp3lame", "-b:a", "192k", out]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       timeout=180, creationflags=_NO_WINDOW)
    if r.returncode != 0 or not os.path.isfile(out):
        raise CloneError("Could not read that audio file. Try an .mp3 or .wav file.")
    seconds = _duration(out)
    notes = []
    if original and original > MAX_SECONDS + 0.5:
        notes.append(f"Your sample was {original:.0f}s; the first 30 seconds were used.")
    if seconds < MIN_SECONDS:
        raise CloneError(f"Only {seconds:.1f}s of speech was found. A clone needs at least "
                         f"{MIN_SECONDS:.0f} seconds - 10 to 30 seconds gives the best result.")
    if seconds < 10:
        notes.append("Short sample - 10 to 30 seconds of clear speech clones noticeably better.")
    if os.path.getsize(out) > MAX_SAMPLE:
        raise CloneError("The cleaned sample is still over 10 MB.")
    return out, round(seconds, 1), notes


def _err(resp, action):
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error") or ""
    except Exception:
        msg = resp.text[:200]
    if resp.status_code in (401, 403):
        return CloneError("AI33 refused the API key. Check it under API Keys.")
    if resp.status_code == 402 or re.search(r"credit|balance|insufficient|quota", str(msg), re.I):
        return CloneError(f"AI33: not enough credits to {action} ({msg or 'balance too low'}). "
                          f"A clone costs {CLONE_CREDITS} credits.")
    return CloneError(f"AI33 could not {action}: {msg or 'HTTP ' + str(resp.status_code)}")


def create_clone(api_key, name, sample_path):
    """Send the sample to AI33. Returns the voice id to narrate with (clone_...)."""
    if not api_key:
        raise CloneError("No AI33 key yet - add it under API Keys first.")
    with open(sample_path, "rb") as f:
        resp = requests.post(f"{API}/v3/text-to-speech/voice-clone",
                             headers={"xi-api-key": api_key},
                             data={"voice_name": name},
                             files={"audio_file": (re.sub(r"\W+", "_", name) + ".mp3", f, "audio/mpeg")},
                             timeout=300)
    if resp.status_code >= 400:
        raise _err(resp, "clone this voice")
    try:
        body = resp.json()
    except ValueError:
        raise CloneError("AI33 answered with something that is not JSON.")
    if body.get("success") is False:
        raise CloneError("AI33: " + (body.get("message") or "the clone was refused"))
    data = body.get("data") or {}
    vid = str(data.get("voice_id") or data.get("id") or "").strip()
    if not vid:
        raise CloneError("AI33 accepted the sample but returned no voice id.")
    voice_id = vid if vid.startswith("clone_") else "clone_" + vid
    try:
        _remember(voice_id, name, round(_duration(sample_path), 1))
    except Exception:
        pass
    # Keep the cleaned sample so the voice can be re-cloned later if needed.
    try:
        safe = re.sub(r"[^\w-]+", "_", name)
        keep = os.path.join(_samples_dir(), f"{safe}_{vid[-8:]}.mp3")
        shutil.copyfile(sample_path, keep)
    except Exception:
        pass
    return voice_id


# AI33's `provider=clone` listing is not only this buyer's voices (it returns a
# shared library of 100+), so the voices made HERE are remembered locally:
# that list is what the dialog shows and the only thing it will delete.

def _registry_file():
    return os.path.join(os.path.dirname(_samples_dir()), "my_cloned_voices.json")


def my_clones():
    import json
    try:
        with open(_registry_file(), encoding="utf-8") as f:
            data = json.load(f)
        return [v for v in data if isinstance(v, dict) and v.get("id")]
    except (OSError, ValueError):
        return []


def _save_registry(items):
    import json
    path = _registry_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _remember(voice_id, name, seconds=0):
    import time
    items = [v for v in my_clones() if v["id"] != voice_id]
    items.insert(0, {"id": voice_id, "name": name, "seconds": seconds,
                     "created": time.strftime("%Y-%m-%d %H:%M")})
    _save_registry(items)


def list_clones(api_key=None):
    """The voices cloned from this app, newest first."""
    return [{"id": v["id"], "name": v.get("name") or v["id"], "preview": "",
             "created": v.get("created", "")} for v in my_clones()]


def delete_clone(api_key, voice_id):
    raw = str(voice_id or "")
    if not any(v["id"] == raw for v in my_clones()):
        raise CloneError("Only voices you cloned in this app can be deleted here.")
    resp = requests.delete(f"{API}/v3/text-to-speech/voice-clone/{raw[len('clone_'):]}",
                           headers={"xi-api-key": api_key}, timeout=60)
    if resp.status_code >= 400 and resp.status_code != 404:
        raise _err(resp, "delete this voice")
    _save_registry([v for v in my_clones() if v["id"] != raw])
    return True

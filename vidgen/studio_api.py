"""
Studio API extensions for DASH:
- Live Image Generation Stream & Inspection
- Available Scripts Catalog
- Reference Images Catalog
- Zero-Blank Verification on demand
- Deliverables Player & Metadata API
- Background Video Production Launcher
"""
import os
import json
import re
import glob
import time
import subprocess
import shutil
import sqlite3
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
SCRIPTS_DIR = os.environ.get("STICKFILM_SCRIPTS") or os.path.join(ROOT_DIR, "scripts")
PROJECTS_DIR = os.environ.get("STICKFILM_PROJECTS") or os.path.join(HERE, "projects")
REFS_DIR = os.environ.get("STICKFILM_REFS") or os.path.join(HERE, "refs")
_DEFAULT_PUB = os.path.join(os.path.expanduser("~"), "StickFilm")
PUB_BASE = os.environ.get("STICKFILM_PUB_BASE") or _DEFAULT_PUB
def get_available_scripts():
    """List all available scripts in scripts/ with stats."""
    if not os.path.isdir(SCRIPTS_DIR):
        return []
    scripts = []
    for f in sorted(os.listdir(SCRIPTS_DIR)):
        if f.endswith(".txt"):
            fp = os.path.join(SCRIPTS_DIR, f)
            name = os.path.splitext(f)[0]
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as sfile:
                    content = sfile.read().strip()
                words = len(content.split())
                preview = content[:200].replace("\n", " ") + "..." if len(content) > 200 else content
                scripts.append({
                    "name": name,
                    "filename": f,
                    "words": words,
                    "chars": len(content),
                    "preview": preview,
                    "path": fp
                })
            except Exception:
                pass
    return scripts


def get_available_refs():
    """List all character reference images in refs/ and root directory."""
    refs = []
    seen = set()
    
    # 1. vidgen/refs/
    if os.path.isdir(REFS_DIR):
        for f in sorted(os.listdir(REFS_DIR)):
            if f.lower().endswith((".png", ".jpg", ".webp")):
                fp = os.path.join(REFS_DIR, f)
                refs.append({
                    "name": f,
                    "filename": f,
                    "category": "Standard Refs",
                    "path": fp,
                    "url": f"/ref/{f}"
                })
                seen.add(f.lower())

    # 2. Root directory *.png
    for f in sorted(os.listdir(ROOT_DIR)):
        if f.lower().endswith(".png") and f.lower() not in seen:
            fp = os.path.join(ROOT_DIR, f)
            refs.append({
                "name": f,
                "filename": f,
                "category": "Project Refs",
                "path": fp,
                "url": f"/ref/{f}"
            })
            seen.add(f.lower())

    return refs


def get_live_images(pid):
    """
    Scans generated images for project pid, pairing them with prompts and timestamps.
    Returns real-time image cards with zero-blank verification status.
    """
    pdir = os.path.join(PROJECTS_DIR, pid)
    img_dir = os.path.join(pdir, "images")
    down_dir = os.path.join(pdir, "downloads")

    # Load prompts if available
    prompts_file = os.path.join(pdir, "prompts.txt")
    prompts = []
    if os.path.isfile(prompts_file):
        with open(prompts_file, "r", encoding="utf-8", errors="replace") as f:
            prompts = [line.strip() for line in f if line.strip()]

    # Load segments if available
    seg_file = os.path.join(pdir, "segments.json")
    segments = []
    if os.path.isfile(seg_file):
        try:
            with open(seg_file, "r", encoding="utf-8") as f:
                segments = json.load(f)
        except Exception:
            pass

    # Load downloads count
    zip_count = len([f for f in os.listdir(down_dir) if f.endswith(".zip")]) if os.path.isdir(down_dir) else 0

    # images/ only fills once a round has been unpacked AND named, which needs
    # segments.json. Until then the pictures sit in images_raw/ under their
    # round names - show those rather than an empty wall.
    raw_dir = os.path.join(pdir, "images_raw")
    staged = False
    if not os.path.isdir(img_dir) or not [
        f for f in os.listdir(img_dir) if f.lower().endswith((".png", ".jpg", ".webp"))
    ]:
        if os.path.isdir(raw_dir) and [
            f for f in os.listdir(raw_dir) if f.lower().endswith((".png", ".jpg", ".webp"))
        ]:
            img_dir = raw_dir
            staged = True

    if not os.path.isdir(img_dir):
        return {
            "project": pid,
            "total_images": 0,
            "distinct_images": 0,
            "blanks_count": 0,
            "zip_count": zip_count,
            "staged": False,
            "images": []
        }

    files = sorted(os.listdir(img_dir))
    img_files = [f for f in files if f.lower().endswith((".png", ".jpg", ".webp"))]
    
    total_imgs = len(img_files)
    blanks_count = 0
    items = []

    # Map shot index from filename
    # e.g. 00-00.00_i001.png or shot_001.png
    num_re = re.compile(r"_i(\d+)\.")
    time_re = re.compile(r"^(\d{2}-\d{2}\.\d{2})")

    for f in img_files:
        fp = os.path.join(img_dir, f)
        sz = os.path.getsize(fp)
        is_blank = (sz == 39370 or sz < 15000)
        if is_blank:
            blanks_count += 1

        m_num = num_re.search(f)
        shot_n = int(m_num.group(1)) if m_num else len(items) + 1

        m_time = time_re.match(f)
        timecode = m_time.group(1).replace("-", ":") if m_time else ""

        # Find prompt and text
        prompt_text = prompts[shot_n - 1] if 0 < shot_n <= len(prompts) else ""
        seg = next((s for s in segments if s.get("n") == shot_n), None)
        spoken_text = seg.get("text", "") if seg else ""
        dur = round(float(seg.get("end", 0)) - float(seg.get("start", 0)), 2) if seg else None

        # The file's own timestamp rides along in the URL. A redrawn shot
        # keeps the same path, so without this the browser would go on
        # showing the copy it cached the first time.
        try:
            stamp = int(os.path.getmtime(fp))
        except OSError:
            stamp = 0
        base_url = f"/image_raw/{pid}/{f}" if staged else f"/image/{pid}/{shot_n}"

        items.append({
            "n": shot_n,
            "filename": f,
            "mtime": stamp,
            "url": f"{base_url}?v={stamp}",
            "timecode": timecode,
            "duration": dur,
            "size_kb": round(sz / 1024, 1),
            "is_blank": is_blank,
            "text": spoken_text,
            "prompt": prompt_text
        })

    return {
        "project": pid,
        "staged": staged,
        "total_images": total_imgs,
        "blanks_count": blanks_count,
        "zip_count": zip_count,
        "images": items
    }


def verify_project_images(pid):
    """Deep validation of images for zero blanks."""
    res = get_live_images(pid)
    total = res["total_images"]
    blanks = res["blanks_count"]
    passed = (total > 0 and blanks == 0)
    return {
        "project": pid,
        "total_images": total,
        "blank_images": blanks,
        "passed": passed,
        "status": "PASSED" if passed else ("FAILED (Blanks detected)" if blanks > 0 else "NO IMAGES")
    }


def get_deliverables(pid):
    """Retrieve final master video and published deliverables."""
    pdir = os.path.join(PROJECTS_DIR, pid)
    pub_dir = os.path.join(PUB_BASE, pid)
    
    # Find master video
    mp4_path = ""
    if os.path.isdir(pub_dir):
        pub_mp4s = [f for f in os.listdir(pub_dir)
                    if f.endswith(".mp4") and not f.endswith(".part.mp4")]
        if pub_mp4s:
            mp4_path = os.path.join(pub_dir, pub_mp4s[0])
    if not mp4_path and os.path.isfile(os.path.join(pdir, "final.mp4")):
        mp4_path = os.path.join(pdir, "final.mp4")

    has_mp4 = bool(mp4_path and os.path.isfile(mp4_path))
    mp4_size_mb = round(os.path.getsize(mp4_path) / (1024 * 1024), 1) if has_mp4 else 0

    # Find thumbnails
    thumbs = []
    target_thumb_dir = pub_dir if os.path.isdir(pub_dir) else pdir
    if os.path.isdir(target_thumb_dir):
        for f in sorted(os.listdir(target_thumb_dir)):
            if f.lower().startswith("thumbnail") and f.lower().endswith((".jpg", ".png", ".webp")):
                thumbs.append({
                    "filename": f,
                    "url": f"/deliverable/{pid}/{f}"
                })

    # Read upload.txt metadata
    upload_txt = ""
    for loc in [pub_dir, pdir]:
        up_path = os.path.join(loc, "upload.txt")
        if os.path.isfile(up_path):
            try:
                with open(up_path, "r", encoding="utf-8") as f:
                    upload_txt = f.read()
                break
            except Exception:
                pass

    return {
        "project": pid,
        "has_master": has_mp4,
        "master_size_mb": mp4_size_mb,
        # The version tag changes with the file, so a finished render is never
        # hidden behind a copy the browser cached earlier.
        "video_url": (f"/video/{pid}?v={int(os.path.getmtime(mp4_path))}" if has_mp4 else None),
        "thumbnails": thumbs,
        "metadata": upload_txt,
        "pub_dir": pub_dir if os.path.isdir(pub_dir) else pdir
    }


# ── Chrome Browser Profiles Management ─────────────────────────────────────

PROFILES_DIR = (os.environ.get("STICKFILM_PROFILES")
                or os.path.join(ROOT_DIR, "ai_avatar_pipeline", "browser_profiles"))


def find_chrome_exe():
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


def _cookie_key_usable(profile_dir):
    """True when this machine can actually decrypt the profile's cookies.

    Chrome keeps the AES key in Local State, wrapped by DPAPI, which seals it
    to one Windows account on one PC. Copy the folder elsewhere and the wrap
    cannot be opened: the cookie rows survive, their values do not, and Chrome
    starts signed out. Returns None when there is nothing to judge.
    """
    state = os.path.join(profile_dir, "Local State")
    if not os.path.isfile(state):
        return None
    try:
        import base64
        import ctypes
        import ctypes.wintypes as wt

        with open(state, encoding="utf-8", errors="replace") as f:
            blob = json.load(f).get("os_crypt", {}).get("encrypted_key")
        if not blob:
            return None
        raw = base64.b64decode(blob)
        if raw[:5] != b"DPAPI":
            return None
        raw = raw[5:]

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        src = _BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw),
                                          ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
        if ok:
            ctypes.windll.kernel32.LocalFree(out.pbData)
            return True
        return False
    except Exception:
        return None


_GOOGLE_SIGNIN_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID",
                          "APISID", "HSID", "SSID", "__Secure-1PAPISID",
                          "__Secure-3PAPISID")


def profile_login_state(profile_dir):
    """
    "yes"    - this profile really holds Google session cookies
    "no"     - the folder exists but nobody has signed in
    "in use" - a running browser has the cookie DB locked, so we cannot tell

    Checking whether the folder has files in it is NOT enough: Chrome creates
    "Default", "Preferences" and "First Run" the instant it launches, so a
    profile that was merely opened once looks identical to a signed-in one.
    Cookie values are encrypted, but host names are plain text, which is all
    this needs.
    """
    locked = False
    for rel in ("Default/Network/Cookies", "Default/Cookies", "Network/Cookies", "Cookies"):
        db = os.path.join(profile_dir, *rel.split("/"))
        if not os.path.isfile(db):
            continue
        tmp = db + ".logincheck"
        try:
            shutil.copy2(db, tmp)
            conn = sqlite3.connect(tmp)
            try:
                # Only the cookies Google sets on sign-in. Opening Gemini
                # signed out still leaves NID, AEC and consent cookies on
                # google.com, and counting those showed every profile that
                # had ever loaded the page as "Signed in".
                marks = ",".join("?" * len(_GOOGLE_SIGNIN_COOKIES))
                count = conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%google.com' "
                    "AND name IN (" + marks + ")",
                    _GOOGLE_SIGNIN_COOKIES,
                ).fetchone()[0]
            finally:
                conn.close()
            if count:
                # Rows exist - but can this PC read them?
                if _cookie_key_usable(profile_dir) is False:
                    return "foreign"
                return "yes"
        except Exception:
            locked = True
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return "in use" if locked else "no"


def get_browser_profiles():
    try:
        os.makedirs(PROFILES_DIR, exist_ok=True)
    except OSError as exc:
        # Read-only install folder. Say so on the page instead of throwing:
        # an exception here empties the whole profiles tab with no reason given.
        return [{
            "name": "(profiles folder unavailable)",
            "path": PROFILES_DIR,
            "login": "no",
            "logged_in": False,
            "has_data": False,
            "status": "Cannot create profiles here - %s" % (exc.strerror or exc),
        }]
    # Guarantee standard acc1..acc6 exist
    for default_acc in ["acc1", "acc2", "acc3", "acc4", "acc5", "acc6"]:
        os.makedirs(os.path.join(PROFILES_DIR, default_acc), exist_ok=True)

    _LABEL = {
        "yes":     "Signed in",
        "no":      "Not signed in - open it once and log in",
        "in use":  "In use by a running browser",
        "foreign": "Session was made on another PC - open it and sign in here",
    }
    profiles = []
    for item in sorted(os.listdir(PROFILES_DIR)):
        item_path = os.path.join(PROFILES_DIR, item)
        if not os.path.isdir(item_path):
            continue
        state = profile_login_state(item_path)
        profiles.append({
            "name": item,
            "path": item_path,
            "login": state,
            "logged_in": state == "yes",
            # has_data stays for older callers, but it now means "signed in",
            # never "a folder exists"
            "has_data": state == "yes",
            "status": _LABEL[state],
        })
    return profiles


def create_browser_profile(name):
    clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name).strip()
    if not clean_name:
        return False, "Invalid profile name. Use letters, numbers, underscores."
    p_path = os.path.join(PROFILES_DIR, clean_name)
    os.makedirs(p_path, exist_ok=True)
    return True, clean_name


def open_browser_profile(name):
    chrome_bin = find_chrome_exe()
    if not chrome_bin:
        return False, "Google Chrome executable not found in standard paths!"
    p_path = os.path.join(PROFILES_DIR, name)
    os.makedirs(p_path, exist_ok=True)

    google_url = "https://accounts.google.com"
    gemini_url = "https://gemini.google.com"
    ext_url = "https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc?hl=en"

    cmd = [chrome_bin, f"--user-data-dir={p_path}", "--new-window", google_url, gemini_url, ext_url]
    try:
        subprocess.Popen(cmd)
        return True, f"Chrome opened for profile '{name}'!"
    except Exception as e:
        return False, str(e)


def open_all_browser_profiles():
    chrome_bin = find_chrome_exe()
    if not chrome_bin:
        return False, "Google Chrome executable not found!"
    profiles = get_browser_profiles()
    opened_count = 0
    for p in profiles:
        open_browser_profile(p["name"])
        opened_count += 1
        time.sleep(1.2)
    return True, f"Opened {opened_count} Chrome profile windows for one-time login!"


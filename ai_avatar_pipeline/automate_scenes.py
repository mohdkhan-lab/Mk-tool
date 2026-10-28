"""
AUTOMATE SCENES — drives real Chrome (your logged-in profile) with Playwright to
do the repetitive part of the pipeline automatically: for each scene in a
scenes.json, send the image prompt to ChatGPT, wait for the image, download it
(no manual "Save As" click — Playwright captures downloads directly), then do
the same for Google Flow to turn the image + dialogue into an animated clip.

WHY THIS EXISTS
    Doing this by hand (as Claude was doing turn-by-turn in the browser pane)
    means a manual Save-dialog click for every single image and video across
    28+ scenes. Playwright controls a real browser and intercepts downloads
    programmatically, so no dialog ever appears — this script can run the
    whole scenes.json unattended once you're logged in.

SETUP (one-time)
    pip install --user playwright
    python automate_scenes.py --login
        This opens a real Chrome window using a dedicated profile folder
        (chrome_profile/ next to this script). Log into ChatGPT (and Google
        Flow, if doing video) in that window, then come back to the terminal
        and press Enter. The login is saved in chrome_profile/ for next time.

USAGE
    python automate_scenes.py output/01_gajar_bhai_sabzi/scenes.json --images
        Generates + downloads any scene image that doesn't have an image_path
        yet, writes files to output/<id>/images/scene_NN.png, and updates
        scenes.json as it goes (safe to Ctrl+C and resume later).

    python automate_scenes.py output/01_gajar_bhai_sabzi/scenes.json --videos
        Same idea for Google Flow: uploads each scene's image + dialogue,
        downloads the animated clip to output/<id>/clips/scene_NN.mp4.
        NOTE: Flow's page structure wasn't hand-verified the way ChatGPT's
        was — the selectors below are best-effort. If a step doesn't find its
        element, the script prints exactly which selector failed so it's a
        quick fix rather than a silent hang.

    python automate_scenes.py output/01_gajar_bhai_sabzi/scenes.json --images --videos
        Does both, scene by scene (image, then that image animated), so a
        character-consistency issue shows up immediately instead of after
        generating all 28 images first.

After this, stitch as usual:
    python stitch_video.py output/01_gajar_bhai_sabzi/scenes.json
"""

import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

from playwright.sync_api import sync_playwright

# Every helper here runs from a no-console (pythonw) parent — the dashboard
# app — so a plain subprocess.run still gets Windows its own console window,
# one flash per tasklist/taskkill/powershell call. kill_stale_chrome alone
# fires up to five of these per generation round.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(SCRIPT_DIR, "chrome_profile")

# Where the signed-in Chrome profiles live. Overridable because the sign-in is
# the one thing a copy of this folder cannot bring with it: the code and the
# look files travel, the Google session does not. Point STICKFILM_PROFILES at
# an install that is already signed in and every account works immediately;
# leave it unset and this folder grows its own, which then has to be signed in
# once per account.
# The previous owner's drive is not a default on anyone else's PC.
_default_prof = os.path.join(SCRIPT_DIR, "browser_profiles")
PROFILES_DIR = os.environ.get("STICKFILM_PROFILES") or _default_prof
def _profiles_file():
    """profiles.json, wherever this copy keeps it.

    Installed, this module runs from <user folder>/code while profiles.json
    ships with the app in Program Files. Looking only beside this file found
    nothing, fell back to a single "default" profile, and the images stage
    died with "no browser profile called acc1". Try the user's folder (a
    list they edited), then every ai_avatar_pipeline folder on the path (the
    shipped one), then beside this file.
    """
    tried = []
    try:
        import userdata
        tried.append(os.path.join(userdata.base(), "ai_avatar_pipeline", "profiles.json"))
    except Exception:
        pass
    for d in sys.path:
        if os.path.basename(os.path.normpath(d or ".")) == "ai_avatar_pipeline":
            tried.append(os.path.join(d, "profiles.json"))
    tried.append(os.path.join(SCRIPT_DIR, "profiles.json"))
    for path in tried:
        if os.path.isfile(path):
            return path
    return tried[-1]


PROFILES_FILE = _profiles_file()

CHATGPT_URL = "https://chatgpt.com"
# How many times one scene is retried on the same account before it is set
# aside. Switching accounts on a generic failure is what caused the same clip
# to be generated twice on two profiles at once.
RETRY_PER_SCENE = 3

FLOW_URL ="https://labs.google/fx/tools/flow"

# How long to allow for a Flow page load. Playwright's default is 30s, and the
# calls that took it killed a whole 48-clip run before a single scene had been
# submitted: one slow load raises and nothing catches it. Everywhere else in
# this file already passes 90s explicitly, so this just makes that the rule
# rather than something each call site has to remember.
NAV_TIMEOUT = 90_000

# Text that Flow shows when a account's free generation quota is used up.
# Matched case-insensitively against the page body to decide when to rotate
# to the next profile. Add more phrasings here if you see a different one.
QUOTA_PATTERNS = [
    "out of credits", "no credits", "credits remaining", "run out",
    "limit reached", "reached your limit", "daily limit", "quota",
    "upgrade to continue", "not enough credits", "insufficient credits",
]


class FlowRejected(RuntimeError):
    """Flow refused this generation outright — retrying as-is will not help."""


def log(msg):
    print(f"[automate] {msg}", flush=True)


def load_profiles():
    """
    Reads profiles.json — the list of browser profiles to rotate through as
    each one's Flow quota runs out. Falls back to the single legacy
    chrome_profile/ directory when the file doesn't exist yet.

    profiles.json format:
    [
      {"name": "chrome1", "browser": "chromium", "channel": "chrome"},
      {"name": "chrome2", "browser": "chromium", "channel": "chrome"},
      {"name": "edge1",   "browser": "chromium", "channel": "msedge"},
      {"name": "brave1",  "browser": "chromium",
       "executable_path": "C:\\\\Program Files\\\\BraveSoftware\\\\Brave-Browser\\\\Application\\\\brave.exe"},
      {"name": "firefox1","browser": "firefox"}
    ]

    Each profile gets its own persistent user-data dir under
    browser_profiles/<name>/, so their Google logins stay separate.
    """
    if os.path.isfile(PROFILES_FILE):
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            profiles = json.load(f)
    else:
        profiles = [{"name": "default", "browser": "chromium", "channel": "chrome"}]
    for p in profiles:
        p.setdefault("browser", "chromium")
        if p.get("clone_from"):
            # A live profile we copy the session out of — the clone itself is a
            # sandbox dir, so the real browser is free to stay open.
            p["clone_from"] = os.path.expandvars(os.path.expanduser(p["clone_from"]))
            p["user_data_dir"] = os.path.join(PROFILES_DIR, f"{p['name']}_clone")
        elif p.get("user_data_dir"):
            # A real, already-signed-in browser profile — expand ~ and %VARS%.
            p["user_data_dir"] = os.path.expandvars(os.path.expanduser(p["user_data_dir"]))
            p["real_profile"] = True
        elif p["name"] == "default" and os.path.isdir(PROFILE_DIR):
            p["user_data_dir"] = PROFILE_DIR  # keep using the original login
        else:
            p["user_data_dir"] = os.path.join(PROFILES_DIR, p["name"])
    return profiles


# Session-carrying files inside a Chromium profile. Copying just these into a
# sandbox directory reproduces the logged-in state without needing the source
# browser to be closed (a full profile copy would be gigabytes and mostly cache).
CLONE_FILES = [
    "Local State",
    "{p}/Network/Cookies",
    "{p}/Network/Network Persistent State",
    "{p}/Cookies",
    "{p}/Login Data",
    "{p}/Preferences",
    "{p}/Secure Preferences",
    "{p}/Web Data",
]
CLONE_DIRS = [
    "{p}/Local Storage",
    "{p}/Session Storage",
    "{p}/IndexedDB",
]


def clone_profile(profile):
    """
    Copies the session files of a REAL browser profile into a sandbox copy.

    DOES NOT CARRY THE LOGIN on modern Chromium. Chrome/Brave/Opera 127+ ship
    App-Bound Encryption: the cookie key in "Local State" is sealed to the
    installed app, so a copied profile launches signed OUT even though the
    cookie DB copies fine (verified — the source Local State here carries an
    "app_bound_encrypted_key"). That is exactly the profile-copying attack the
    feature exists to stop, so there is no way around it.

    Kept only for non-Chromium profiles; for Chromium, point the profile at
    its real user_data_dir and close that browser while automation runs.
    """
    src = profile["clone_from"]
    src_profile_dir = profile.get("profile_directory") or "Default"
    dest = os.path.join(PROFILES_DIR, f"{profile['name']}_clone")
    dest_profile_dir = "Default"  # the clone always uses a plain Default profile

    if not os.path.isdir(src):
        raise RuntimeError(f"source profile not found: {src}")

    os.makedirs(os.path.join(dest, dest_profile_dir), exist_ok=True)

    copied = 0
    got_cookies = False
    for rel in CLONE_FILES:
        s = os.path.join(src, rel.format(p=src_profile_dir))
        if not os.path.isfile(s):
            continue
        rel_dest = rel.format(p=dest_profile_dir)
        d = os.path.join(dest, rel_dest)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        try:
            shutil.copy2(s, d)
            copied += 1
            if rel.endswith("Cookies"):
                got_cookies = True
        except Exception as e:
            log(f"  (skipped {rel}: {e})")

    # Without the cookie DB the clone is simply a logged-out browser, which
    # would fail much later with a confusing "not signed in" symptom. The file
    # is locked while the source browser runs, so say exactly that instead.
    if not got_cookies:
        raise RuntimeError(
            f"could not copy the cookie database out of '{profile['name']}' — the browser is holding it open.\n"
            f"       Close {os.path.basename(profile.get('executable_path') or 'the browser')} COMPLETELY "
            f"(all windows plus the system-tray icon), run this once to build the clone, "
            f"and then you can reopen it — the automation only uses the clone from that point on."
        )

    for rel in CLONE_DIRS:
        s = os.path.join(src, rel.format(p=src_profile_dir))
        if not os.path.isdir(s):
            continue
        d = os.path.join(dest, rel.format(p=dest_profile_dir))
        try:
            shutil.copytree(s, d, dirs_exist_ok=True,
                            ignore_dangling_symlinks=True)
            copied += 1
        except Exception as e:
            log(f"  (partially copied {rel}: {e})")

    # The source browser's Preferences remember how it last exited and which
    # tabs were open. Left as-is, the clone tries to restore that whole tab set
    # on startup (or shows a "restore pages?" prompt) — which is what left the
    # window sitting on about:blank until Playwright's launch timed out. The
    # session files themselves are never copied, so we just clear the flags
    # that ask for a restore.
    prefs_path = os.path.join(dest, dest_profile_dir, "Preferences")
    if os.path.isfile(prefs_path):
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            prefs.setdefault("profile", {})["exit_type"] = "Normal"
            prefs["profile"]["exited_cleanly"] = True
            prefs.setdefault("session", {})["restore_on_startup"] = 5  # 5 = new tab page
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
        except Exception as e:
            log(f"  (could not normalise Preferences: {e})")

    log(f"  cloned {copied} session item(s) from the live profile -> {dest}")
    return dest


def profile_has_google_login(profile):
    """
    True only if this profile actually holds Google session cookies.

    Checking "does the profile directory have files in it" is not enough — a
    directory is created the moment the browser launches, so a profile that
    was merely opened once looks identical to one that is signed in. Cookie
    VALUES are encrypted, but host names are stored in plain text, which is
    all we need here.

    Returns "yes", "no", or "in use" — the last when a running browser holds
    the cookie DB locked, which makes the check impossible rather than
    negative (reporting that as "not logged in" would be plain wrong for the
    profile currently generating videos).
    """
    locked = False
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        db = os.path.join(profile["user_data_dir"], *rel.split("/"))
        if not os.path.isfile(db):
            continue
        # Copy first: the DB is locked while a browser has the profile open.
        tmp = db + ".listcheck"
        try:
            shutil.copy2(db, tmp)
            conn = sqlite3.connect(tmp)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%google.com'"
                ).fetchone()[0]
            finally:
                conn.close()
            if count:
                return "yes"
        except Exception:
            locked = True
            continue
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return "in use" if locked else "no"


def browser_is_running(profile):
    """
    True if the browser that owns this profile's user-data dir is running.
    A real (non-sandbox) profile directory is locked by the running browser,
    so launching Playwright against it would fail — better to say so plainly
    than to surface a cryptic Playwright error.
    """
    # A cloned profile is a private copy, so the live browser never blocks it.
    if not profile.get("real_profile") or profile.get("clone_from"):
        return False
    exe = (profile.get("executable_path") or profile.get("channel") or "chrome")
    name = os.path.splitext(os.path.basename(exe))[0].lower()
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=20,
                             creationflags=_NO_WINDOW).stdout.lower()
    except Exception:
        return False
    return f"{name}.exe" in out


def kill_stale_chrome(user_data_dir):
    """
    Close any browser still holding this profile directory.

    Chrome allows one process per user-data dir. An interrupted run — a crash,
    a Ctrl-C, a killed parent — leaves its browser behind still holding the
    lock, and every later attempt on that profile dies with "Opening in
    existing browser session" and gets skipped. Only processes whose command
    line names this exact directory are touched, so the user's own Chrome is
    never in scope.
    """
    if not user_data_dir:
        return 0
    target = os.path.abspath(user_data_dir).lower()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
        )
        rows = json.loads(out.stdout or "[]")
    except Exception:
        return 0
    if isinstance(rows, dict):
        rows = [rows]

    pids = [row["ProcessId"] for row in rows
            if target and target in (row.get("CommandLine") or "").lower()]
    if not pids:
        return 0

    # Ask the browser to close itself first. Force-killing Chrome mid-write can
    # leave its profile damaged — which is how a set of signed-in profiles ended
    # up signed out — so /F is only used on what refuses to exit.
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=20,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass
    time.sleep(5)

    still = []
    for pid in pids:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=20,
                                 creationflags=_NO_WINDOW)
            if str(pid) in (out.stdout or ""):
                still.append(pid)
        except Exception:
            pass
    for pid in still:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass

    log(f"  cleared {len(pids)} leftover browser process(es) holding this profile"
        + (f" ({len(still)} needed forcing)" if still else ""))
    time.sleep(3)
    return len(pids)


# ZAPI FLOW cannot be side-loaded from the command line. Chrome 137 began
# ignoring --load-extension and by Chrome 151 the switch is gone outright —
# neither DisableLoadExtensionCommandLineSwitch nor
# --enable-unsafe-extension-debugging brings it back, and the profile comes up
# with no extension at all. That was checked against acc1, whose genuinely
# installed Google Docs Offline shows up fine under the same probe, so the
# switch is what is dead, not the detection. The extension is therefore
# installed once per profile through the Web Store by hand — see
# install_extension.py — and persists in the profile from then on.
EXT_ID = "debadkiomlbdambamdpdlgdkgnedcjil"
EXT_URL = f"https://chromewebstore.google.com/detail/zapi-flow/{EXT_ID}"


def launch_profile(playwright, profile, accept_downloads=True, minimized=False):
    """
    Opens a persistent browser context for one profile entry.

    Two modes:
      * sandbox profile — a fresh user-data dir under browser_profiles/, which
        you log into once via --login.
      * real_profile    — your actual installed browser profile (already signed
        in). Needs "user_data_dir" pointing at the browser's User Data folder
        and, for Chromium browsers with several profiles, "profile_directory"
        (e.g. "Profile 15"). The browser must be CLOSED, since it holds a lock
        on that directory while running.

    `minimized` opens the window far off-screen instead of headless — a login
    still has to be a real, visible browser (headless gets rejected outright
    by Google's sign-in page), but an unattended generation run needs no
    audience, and full headless is a second, untested way to trip the same
    detection. Off-screen keeps the same real browser Google already accepts,
    just where the buyer never sees it.
    """
    browser_type = getattr(playwright, profile.get("browser", "chromium"))
    kwargs = {
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "accept_downloads": accept_downloads,
        # A real profile restores its previous tabs on startup; with a dozen of
        # them the handshake can outrun Playwright's 30s default and leave the
        # window parked on about:blank. Give startup room to finish.
        "timeout": 300_000,
    }
    if minimized:
        # Headed (so Google's automation checks still pass) but hidden from the
        # buyer: parked far off-screen AND the OS window is hidden after launch
        # (see hide_profile_windows), so there is no taskbar button either. The
        # anti-throttle flags keep the renderer at full speed while hidden —
        # without them Chrome slows a background/hidden window and generation
        # crawls. page.screenshot still captures the page for the Live Wall.
        kwargs["args"] = kwargs.get("args", []) + [
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ]
    if profile.get("browser", "chromium") == "chromium":
        # Playwright's default launch flags (--enable-automation, the
        # automation-controlled navigator.webdriver flag, etc.) make Google's
        # sign-in page reject the browser outright with "Couldn't sign you in
        # / this browser or app may not be secure". Stripping them is required
        # for accounts.google.com to accept a login at all — this is not
        # about hiding from Flow, only about letting Google's own sign-in
        # form work in an automated browser.
        # --disable-extensions is in Playwright's own default args, and it
        # wins over --load-extension: without dropping it the profile comes up
        # with no extensions at all, including ones already installed in it.
        kwargs["ignore_default_args"] = ["--enable-automation",
                                         "--disable-extensions"]
        kwargs["args"] = kwargs.get("args", []) + [
            "--disable-blink-features=AutomationControlled",
            # Flow's editor renders video previews for every item in a
            # media library that grows with each run; the tab crashed
            # ("Page crashed") mid-generation without these.
            "--disable-dev-shm-usage",
            "--max_old_space_size=4096",
        ]
    if profile.get("channel"):
        kwargs["channel"] = profile["channel"]
    if profile.get("executable_path"):
        kwargs["executable_path"] = profile["executable_path"]

    if profile.get("clone_from"):
        # Session copied into a sandbox dir, always as a plain "Default"
        # profile — so no --profile-directory arg here. Restoring the source
        # browser's old tabs would drown the startup (it is what made the
        # window sit on about:blank until Playwright timed out), so the clone
        # is told to start with a single blank page.
        user_data_dir = clone_profile(profile)
    else:
        user_data_dir = profile["user_data_dir"]
        if profile.get("profile_directory"):
            # Appended, not assigned: a bare assignment here dropped the
            # anti-automation and extension flags set above.
            kwargs["args"] = kwargs.get("args", []) + [
                f"--profile-directory={profile['profile_directory']}"]
        if profile.get("real_profile") and not os.path.isdir(user_data_dir):
            raise RuntimeError(f"profile directory not found: {user_data_dir}")
        if not profile.get("real_profile"):
            os.makedirs(user_data_dir, exist_ok=True)

    # Extra flags a caller asked for, appended so they never displace the
    # anti-automation and stability flags set above. omni.py --serve uses this
    # to open a debugging port, which is how later commands share one browser
    # instead of each killing the last.
    if profile.get("args"):
        kwargs["args"] = kwargs.get("args", []) + list(profile["args"])

    # A leftover browser on this directory makes the launch fail outright, and
    # the run then skips a perfectly good account.
    kill_stale_chrome(user_data_dir)
    return browser_type.launch_persistent_context(user_data_dir, **kwargs)


    # One video costs 15 credits. Below roughly that, Flow stops giving the
    # full-quality render and quietly returns a degraded clip instead of
    # refusing outright — so the account has to be left while it still has
    # enough, not after the bad clips have already been made.
MIN_CREDITS_TO_CONTINUE = 20


def credits_left(page):
    """
    Credits still on this account, or None if it could not be read.

    The balance is NOT on the page. It lives behind the plan badge (PRO /
    ULTRA / FREE) next to the avatar, so the badge has to be opened first.

    An earlier version of this read `inner_text("body")` and matched anything
    shaped like "<number> credits". On the signed-out landing page that is the
    pricing table — "50 daily", "25,000 monthly" — so it would happily report a
    plan tier as the account's money, and it took the largest match, which is
    the worst possible guess. Budgeting on that number would send an account
    far more work than it can pay for, which is precisely what the balance is
    consulted to prevent. Returning None when the menu cannot be read is the
    honest answer: callers treat it as "unknown", not as "plenty".
    """
    try:
        badges = page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('button,div,span,a')) {
                const t = (el.innerText || '').trim();
                if (/^(PRO|ULTRA|FREE|PLUS)$/i.test(t) && el.offsetParent !== null) {
                    const r = el.getBoundingClientRect();
                    out.push({x: Math.round(r.x + r.width / 2),
                              y: Math.round(r.y + r.height / 2)});
                }
            }
            return out;
        }""")
        if not badges:
            return None
        page.mouse.click(badges[0]["x"], badges[0]["y"])
        page.wait_for_timeout(3000)

        # Short leaf nodes only — the open menu states the balance on its own
        # line, while the surrounding page is full of unrelated numbers.
        texts = page.evaluate("""() => {
            const out = [];
            for (const el of document.querySelectorAll('*')) {
                if (el.children.length) continue;
                const t = (el.innerText || '').trim();
                if (t && t.length < 80 && /credit/i.test(t)) out.push(t);
            }
            return out;
        }""")
    except Exception:
        return None
    finally:
        # Leave the page as it was found; a menu left hanging open swallows the
        # next click, and the next click is usually a submit.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:
            pass

    best = None
    for t in texts:
        # Flow words this as "11 Google Flow credits", so the number and the
        # word "credits" are not adjacent. Matching them adjacently — which an
        # earlier version did — silently found nothing on every account.
        m = re.search(r"([\d,]+)\s+(?:[A-Za-z]+\s+){0,3}credits?\b", t, re.I)
        if not m:
            continue
        try:
            n = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # The menu can show both a remaining balance and the plan's allowance.
        # The smaller one is what is actually left to spend.
        if best is None or n < best:
            best = n
    return best


# What Flow charges for one 10s clip, measured rather than assumed: an account
# opened at 56 credits, produced three clips, and finished on 11 — 45 for 3.
# The first version of this guessed 100, which would have priced a full account
# at a fraction of its real capacity and an empty one as still usable.
#
# Erring high is the safe direction. Overestimating leaves a few credits
# unspent; underestimating hands an account more scenes than it can pay for,
# and Flow does not refuse cleanly at the end — it returns a degraded render
# that looks finished. Override per run with --per-clip.
CREDITS_PER_CLIP = 15

# How long a single clip may sit before it is treated as wedged rather than
# slow. Measured clips landed between 60s and 250s; two that hung went past
# 800s and never arrived. Flow reports both states identically, so the only
# way to tell them apart is elapsed time. Set well above normal so a genuinely
# queued job is never thrown away, and well below the 1500s hard deadline so a
# wedged one does not cost twenty minutes of an idle window.
STRAGGLER_SEC = 420


def clip_budget(page, per_clip=CREDITS_PER_CLIP):
    """
    How many clips this account can still afford, or None if unknown.

    The plan badge that carries the balance only exists inside a project, not
    on the landing page, so this opens one when the badge is not already
    there. Reading the balance from the landing page instead returns nothing —
    which the caller treats as "unknown" and therefore as "no cap", quietly
    disabling the very limit this exists to enforce.

    None means the balance could not be read. That is not the same as zero,
    and it is emphatically not the same as plenty: callers fall back to the
    quota banner, which is late but at least real.
    """
    credits = credits_left(page)
    if credits is None:
        try:
            new_project = page.get_by_role("button", name="New project")
            if new_project.count():
                new_project.first.scroll_into_view_if_needed()
                new_project.first.click()
                page.wait_for_timeout(8000)
                credits = credits_left(page)
        except Exception:
            return None
    if credits is None:
        return None
    return max(0, credits // per_clip)


def quota_exhausted(page):
    """
    True when this account should be left alone.

    Covers both the explicit out-of-quota banners and the quieter case the
    banners miss: enough credits gone that Flow starts returning low-quality
    clips rather than saying no.
    """
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    if any(pat in body for pat in QUOTA_PATTERNS):
        return True

    left = credits_left(page)
    if left is not None and left < MIN_CREDITS_TO_CONTINUE:
        log(f"  sirf {left} credit bache hain (ek video = 15) — is account ko yahin chhod rahe hain, "
            f"warna ghatiya quality wali clip banegi")
        return True
    return False


def load_scenes(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_scenes(path, data):
    """
    Writes scenes.json back, merging in any fields another writer changed.

    A run holds `data` in memory for hours, so a second run — or a hand edit —
    that lands in between would be silently erased by a straight dump. That has
    already cost real work: a stale run overwrote freshly recovered project
    URLs. Per-scene keys this run did not touch are carried over from whatever
    is on disk now, and a clip already recorded there is never unrecorded.
    """
    try:
        disk = json.load(io.open(path, encoding="utf-8-sig"))
        on_disk = {s["scene_number"]: s for s in disk.get("scenes", [])}
    except Exception:
        on_disk = {}

    for scene in data.get("scenes", []):
        other = on_disk.get(scene.get("scene_number"))
        if not other:
            continue
        for key, value in other.items():
            if key not in scene:
                scene[key] = value
        # A clip on disk outranks this run's memory of there being none.
        if other.get("video_path") and not scene.get("video_path"):
            scene["video_path"] = other["video_path"]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def do_login(playwright, profile_name=None):
    """
    Opens one profile's browser so you can sign in to Google Flow with a
    different account. Finishing is signalled by CLOSING the browser window —
    not by pressing Enter — because this also runs as a subprocess from
    ui_server.py, which has no stdin attached (an input() prompt there dies
    with EOFError). The persistent profile saves cookies as you go.
    """
    profiles = load_profiles()
    if profile_name:
        matches = [p for p in profiles if p["name"] == profile_name]
        if not matches:
            log(f"No profile named '{profile_name}' in profiles.json. Known: {', '.join(p['name'] for p in profiles)}")
            return
        profile = matches[0]
    else:
        profile = profiles[0]

    log(f"Opening browser for profile '{profile['name']}'...")
    context = launch_profile(playwright, profile, accept_downloads=False)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(FLOW_URL, timeout=NAV_TIMEOUT)
    log(f"Sign in to Google Flow with the account you want to use for profile '{profile['name']}'.")
    log("When you're done, simply CLOSE the browser window — the login is saved automatically.")

    closed = threading.Event()
    context.on("close", lambda: closed.set())
    # Poll rather than block forever: the context fires 'close' when the user
    # shuts the window, but we also stop if every tab is gone.
    while not closed.is_set():
        try:
            if not context.pages:
                break
        except Exception:
            break
        time.sleep(1)

    try:
        context.close()
    except Exception:
        pass
    log(f"Login saved. Profile stored at: {profile['user_data_dir']}")


def generate_scene_image_chatgpt(page, scene, images_dir, character_note, aspect_ratio):
    scene_num = scene["scene_number"]
    out_path = os.path.join(images_dir, f"scene_{scene_num:02d}.png")

    prompt = (
        f"Generate the image for Scene {scene_num}: {scene['image_prompt']}"
        f" {aspect_ratio} aspect ratio."
        + (f" {character_note}" if character_note else "")
    )

    box = page.get_by_role("textbox", name="Chat with ChatGPT")
    if not box.count():
        box = page.locator("#prompt-textarea")
    box.click()
    box.fill(prompt)
    page.keyboard.press("Enter")

    log(f"  scene {scene_num}: waiting for image generation (ChatGPT)...")
    # Image generation renders a downloadable image; poll for a "Download"-
    # labelled control to appear on the newest assistant message.
    download_btn = page.get_by_role("button", name="Download").last
    download_btn.wait_for(state="visible", timeout=180_000)

    with page.expect_download() as dl_info:
        download_btn.click()
    download = dl_info.value
    download.save_as(out_path)
    log(f"  scene {scene_num}: saved -> {out_path}")
    return out_path


def generate_scene_image_flow(page, scene, images_dir, character_note, aspect_ratio):
    """
    Best-effort — Google Flow's image-generation mode DOM wasn't hand-verified
    the way ChatGPT's was. If a step below times out, open Flow manually,
    inspect the real element (right-click -> Inspect), and fix the selector.
    """
    scene_num = scene["scene_number"]
    out_path = os.path.join(images_dir, f"scene_{scene_num:02d}.png")

    prompt = (
        f"{scene['image_prompt']} {aspect_ratio} aspect ratio."
        + (f" {character_note}" if character_note else "")
    )

    page.get_by_role("button", name="New project").first.click()
    page.wait_for_timeout(1000)
    # Flow's mode switcher — pick "Text to Image" / "Frames" image mode.
    mode_btn = page.get_by_role("button", name="Image").first
    if mode_btn.count():
        mode_btn.click()
        page.wait_for_timeout(500)

    ar_btn = page.get_by_role("button", name=aspect_ratio)
    if ar_btn.count():
        ar_btn.first.click()

    prompt_box = page.get_by_placeholder("Describe your image")
    if not prompt_box.count():
        prompt_box = page.locator("textarea").last
    prompt_box.click()
    prompt_box.fill(prompt)
    page.keyboard.press("Enter")

    log(f"  scene {scene_num}: waiting for image generation (Flow)...")
    download_btn = page.get_by_role("button", name="Download").last
    download_btn.wait_for(state="visible", timeout=180_000)

    with page.expect_download() as dl_info:
        download_btn.click()
    download = dl_info.value
    download.save_as(out_path)
    log(f"  scene {scene_num}: saved -> {out_path}")
    return out_path


def run_images(scenes_path, character_note, source="flow"):
    data = load_scenes(scenes_path)
    out_dir = os.path.dirname(os.path.abspath(scenes_path))
    images_dir = os.path.join(out_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    aspect_ratio = data.get("aspect_ratio", "16:9")

    start_url = FLOW_URL if source == "flow" else CHATGPT_URL
    generate_fn = generate_scene_image_flow if source == "flow" else generate_scene_image_chatgpt

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", headless=False,
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()
        page.goto(start_url)
        page.wait_for_timeout(3000)

        for scene in sorted(data["scenes"], key=lambda s: s["scene_number"]):
            if scene.get("image_path"):
                continue
            try:
                path = generate_fn(page, scene, images_dir, character_note, aspect_ratio)
                scene["image_path"] = os.path.relpath(path, out_dir)
                save_scenes(scenes_path, data)
            except Exception as e:
                log(f"  scene {scene['scene_number']}: FAILED ({e}) — skipping, rerun script to retry")

        context.close()
    log("Image pass complete.")


def _flow_upload_image(page, path):
    """Verified working: click Add Media, pick the file, wait for the real
    upload response (not a fixed sleep — that's what made earlier attempts
    misreport "Failed" when the upload had actually succeeded), and accept
    the one-time content-responsibility dialog if it appears."""
    page.get_by_role("button", name="Add Media").first.click()
    page.wait_for_timeout(1200)
    with page.expect_response(lambda r: "flow/uploadImage" in r.url, timeout=60000) as ri:
        page.locator("input[type=file]").first.set_input_files(path)
        agree = page.get_by_role("button", name="I agree")
        if agree.count():
            agree.first.click()
    if ri.value.status != 200:
        raise RuntimeError(f"image upload failed: HTTP {ri.value.status}")
    page.wait_for_timeout(1000)


def _flow_dismiss_banners(page):
    """
    Closes Flow's promo banners ("Introducing Gemini Omni Flash", the daily
    credits notice). They overlay the projects list, and Playwright refuses to
    click an element another node covers — which is why "New project" timed
    out on every scene after the first even though the button was on the page.
    """
    for name in ("Dismiss", "close Dismiss", "Close"):
        btn = page.get_by_role("button", name=name)
        for _ in range(btn.count()):
            try:
                btn.first.click(timeout=4000)
                page.wait_for_timeout(600)
            except Exception:
                break


def _flow_find_settings_button(page, aspect_ratio="16:9"):
    """
    Flow keeps renaming the combined settings control. Try every label shape
    we've seen: plain crop icon, "Video - 8s crop_16_9 x2", aria-label only.
    """
    ar_icon = "crop_9_16" if aspect_ratio == "9:16" else "crop_16_9"
    probes = [
        page.get_by_role("button", name=ar_icon),
        page.get_by_role("button", name=re.compile(rf".*{re.escape(ar_icon)}.*", re.I)),
        page.get_by_role("button", name=re.compile(r"Video.*crop", re.I)),
        page.locator(f'button[aria-label*="{ar_icon}"]'),
        page.locator('button:has-text("crop_16_9"), button:has-text("crop_9_16")'),
    ]
    for probe in probes:
        try:
            if probe.count():
                return probe.first
        except Exception:
            continue
    return None


def _flow_click_option(page, label, alt_labels=()):
    """Click a settings-panel option, trying a few label variants."""
    candidates = (label,) + tuple(alt_labels)
    for cand in candidates:
        for getter in (
            lambda c=cand: page.get_by_text(c, exact=True),
            lambda c=cand: page.get_by_role("button", name=c),
            lambda c=cand: page.get_by_role("radio", name=c),
            lambda c=cand: page.get_by_role("menuitem", name=c),
            lambda c=cand: page.locator(f'button:has-text("{c}")'),
        ):
            try:
                opt = getter()
                if opt.count():
                    opt.first.click(timeout=4000)
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
    return False


def _flow_configure(page, aspect_ratio="16:9", duration="10s", count="x1"):
    """
    Sets the compose bar's generation options, which all live behind one
    combined button labelled like "Video - 8s  crop_16_9  x2".

    The defaults are actively wrong for this pipeline:
      * count defaults to x2, so every prompt returns TWO identical clips and
        burns double the quota;
      * duration defaults short (an "8 second" request came back as a 4s
        file), which truncates the scene's dialogue mid-sentence.

    `duration` is whatever label Flow shows in that panel ("4s", "6s", "8s",
    "10s"). A scene can set its own via `duration_sec` in scenes.json; the
    label that is missing from the panel is reported rather than silently
    leaving Flow on its own default.
    """
    combo = _flow_find_settings_button(page, aspect_ratio)
    if combo is None:
        log("  WARNING: generation-settings button not found, using Flow's defaults")
        return
    combo.click(timeout=8000)
    page.wait_for_timeout(2500)

    # Aspect ratio renders as an icon-label ("crop_16_9") rather than the
    # plain "16:9" text the other options use.
    ar_label = "crop_9_16" if aspect_ratio == "9:16" else "crop_16_9"
    dur_alts = (duration.replace("s", " s"), duration.replace("s", " seconds"))
    count_alts = ("×1", "x 1", "1") if count == "x1" else (count.replace("x", "×"),)

    for label, alts in ((ar_label, ()), (duration, dur_alts), (count, count_alts)):
        if _flow_click_option(page, label, alts):
            continue
        log(f"  WARNING: option '{label}' not found in settings panel")

    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)
    log(f"  settings: {aspect_ratio}, {duration}, {count}")


def generate_scene_video(page, scene, next_scene, clips_dir, out_dir, aspect_ratio,
                         on_submit=None, speaker_look=None):
    """
    Flow's current UI is a chat-agent (not the classic form we originally
    guessed at): you upload an image, describe what you want in a prompt box,
    and an agent plans the generation. Verified end-to-end against the real
    UI (see test_flow_simple.py):
      1. New project
      2. Add Media -> upload the scene's image (single starting frame —
         first+last-frame interpolation is Veo-only / paid; the free default
         model "Omni Flash" only animates from one image, so no last_image
         is used here despite the parameter existing).
      3. Type a prompt asking for an 8s animation with the scene's dialogue
         spoken (Flow generates the voice/audio itself — no separate TTS
         step needed).
      4. Click Create. The agent may show an approval card ("Would you like
         me to kick off this video generation, costing N credits?") —
         approve it if so.
      5. Poll the page for a "Download" control (progress shows as a % on a
         thumbnail in the meantime) and download the finished clip.
    """
    scene_num = scene["scene_number"]
    out_path = os.path.join(clips_dir, f"scene_{scene_num:02d}.mp4")
    image_path = scene.get("image_path")
    if not image_path:
        raise RuntimeError("no image_path yet — run --images first")
    start_image = os.path.join(out_dir, image_path)

    # If the file was already downloaded (e.g. crash between save_as and
    # save_scenes), skip generation so we don't burn a credit regenerating it.
    if os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
        log(f"  scene {scene_num}: clip already on disk, skipping generation")
        return out_path

    _flow_dismiss_banners(page)

    # A generation already sent for this scene must never be sent again. Flow
    # has no cancel and no resume of its own, so a clip that rendered but was
    # not downloaded used to leave scenes.json saying "pending" — and the next
    # run happily paid for the same clip a second, third and fourth time.
    # Recording the project as soon as it exists makes that impossible: the
    # rerun returns to that project and collects the clip instead.
    existing = scene.get("video_project_url")
    if existing:
        log(f"  scene {scene_num}: is scene ki generation pehle se bhej chuke hain — "
            f"usi project se clip lene ja raha hoon (koi naya credit nahi)")
        try:
            page.goto(existing, timeout=90000)
            page.wait_for_timeout(8000)
            _flow_dismiss_banners(page)
            path = _flow_collect_clip(page, scene_num, out_path, start_image)
            if path:
                return path
            log(f"  scene {scene_num}: us project mein clip abhi tayyar nahi — wahin intezaar")
            return _flow_wait_and_download(page, scene_num, out_path, source_image=start_image)
        except Exception as e:
            log(f"  scene {scene_num}: purana project khul nahi paya ({type(e).__name__}) — "
                f"naya banana padega")
            scene.pop("video_project_url", None)
            scene.pop("video_account", None)

    new_project = page.get_by_role("button", name="New project")
    new_project.first.wait_for(state="visible", timeout=60_000)
    new_project.first.scroll_into_view_if_needed()
    new_project.first.click()
    page.wait_for_timeout(9000)

    # Per-scene length: a short beat does not need a full 10s clip, and a
    # shorter clip is faster to generate and cheaper on Flow's quota.
    seconds = int(scene.get("duration_sec") or 10)
    _flow_configure(page, aspect_ratio, duration=f"{seconds}s", count="x1")

    _flow_upload_image(page, start_image)

    # CRITICAL: uploading only files the image in the project's media library.
    # The agent will happily ignore it and do text-to-video from the prompt
    # alone — a village-carrot image once produced an onion-in-space video
    # because the prompt mentioned "Sabzi Multiverse". The image only becomes
    # the actual source frame once it is attached via "Add to Prompt".
    prompt_box = page.locator("[contenteditable=true]").first
    prompt_box.click()
    prompt_box.type("@", delay=80)
    page.wait_for_timeout(2500)

    add_to_prompt = page.get_by_text("Add to Prompt")
    if not add_to_prompt.count():
        raise RuntimeError("could not find 'Add to Prompt' — image would be ignored, aborting")
    add_to_prompt.first.click()
    page.wait_for_timeout(2000)

    # A scene with no dialogue is deliberately silent — the narrator speaks over
    # it instead. Asking Flow to animate speech anyway would put mouth movement
    # under a voice that is not the character's.
    dialogue = (scene.get("dialogue") or "").strip()
    if dialogue:
        # Name the speaker. "The character speaks..." leaves Flow to pick one of
        # whoever is in frame, and it picked wrong again and again — Lauki Chachi
        # delivering Bhindi Ji's lines, Bhindi delivering Baingan's. Only the
        # named character may move their mouth.
        speaker = (scene.get("speaker") or "").strip()
        look = (speaker_look or "").strip()
        if speaker:
            who = f"{speaker} ({look})" if look else speaker
            prompt_text = (
                f"Animate this image into a {seconds}-second video. "
                f"ONLY {who} speaks. {speaker} says this line in {SPEECH_LANGUAGE}: \"{dialogue}\". "
                f"Every other character in the frame stays SILENT with their mouth closed — "
                f"they may react with expressions and small movements, but must not talk."
            )
            voice = (scene.get("voice_hint") or "").strip()
            if voice:
                prompt_text += f" {speaker}'s voice should sound like {voice}."
        else:
            prompt_text = (
                f"Animate this image into a {seconds}-second video. "
                f"The character speaks this dialogue: \"{dialogue}\""
            )
    else:
        prompt_text = (
            f"Animate this image into a {seconds}-second video. No one speaks and no mouths move — "
            "the characters stay silent. Animate ONLY what is already visible in the image — "
            "do NOT add any people, characters or figures that are not already in the frame. "
            "Bring whoever is already there to life with natural body movement, gestures and "
            "expressions, plus gentle camera motion. If the scene has no people in it, it must "
            "stay empty: animate the light and the camera only."
        )
    instruction = scene.get("video_instruction", "").strip()
    if instruction:
        prompt_text += f" Additional instruction: {instruction}"
    prompt_box.click()
    prompt_box.type(prompt_text, delay=10)
    page.wait_for_timeout(500)

    create_btn = page.get_by_role("button", name="arrow_forward Create")
    if not create_btn.count():
        create_btn = page.get_by_role("button", name="Create")
    if not create_btn.count():
        _flow_debug_shot(page, scene_num, "no-create-button")
        raise RuntimeError(f"scene {scene_num}: no Create button on screen")

    # Count the tiles before submitting, so "a new one appeared" can be told
    # apart from "one was already there".
    media_before = _flow_media_count(page)
    create_btn.last.click()

    # Save the project the moment the request goes out — before waiting, before
    # downloading. Anything that kills the run after this point (a crash, a
    # closed browser, a stop) then still leaves a trail back to the clip Flow
    # is rendering, instead of orphaning it and paying again.
    if on_submit:
        try:
            on_submit(page.url)
        except Exception:
            pass

    # Sanity-check the click, but never abort on it. Flow can take a while to
    # show anything at all, and treating silence as failure abandoned clips
    # that were already rendering — the browser closed, the scene was marked
    # failed, and the work Flow had done was thrown away. Warn, then fall into
    # the normal wait loop, which is what actually detects completion.
    if not _flow_submission_started(page, media_before):
        _flow_debug_shot(page, scene_num, "no-signal-yet")
        log(f"  scene {scene_num}: no visible progress yet — waiting anyway "
            f"(screenshot in debug_shots/)")

    source_image = start_image
    log(f"  scene {scene_num}: waiting for video generation (queueing/generation can take several minutes)...")
    deadline = time.time() + 1200  # 20 minutes — Flow warns of queueing under high demand
    approved = False
    started = time.time()
    last_beat = 0.0
    last_pct = -1
    while time.time() < deadline:
        page.wait_for_timeout(8000)

        # Without this the loop is silent for up to 20 minutes, which is
        # indistinguishable from being hung — and that ambiguity is what made
        # the run look stuck when it was only waiting on Flow.
        elapsed = time.time() - started
        if elapsed - last_beat >= 30:
            last_beat = elapsed
            state = []
            try:
                if page.locator("video").count():
                    state.append(f"{page.locator('video').count()} video element(s)")
                if page.get_by_role("button", name="Download").count():
                    state.append("download ready")
                if page.get_by_role("button", name="Approve").count():
                    state.append("approval card showing")
                body = page.inner_text("body") or ""
                pct = re.search(r"\b(\d{1,3})\s?%", body)
                if pct:
                    state.append(f"{pct.group(1)}% rendered")
                    # Something is genuinely being produced — keep the deadline
                    # ahead of it. Timing out on a clip that is visibly
                    # progressing throws away work Flow will not resume.
                    now = int(pct.group(1))
                    if now > last_pct:
                        last_pct = now
                        deadline = max(deadline, time.time() + 600)
                for word in ("queued", "in queue", "processing", "failed", "error"):
                    if word in body.lower():
                        state.append(word)
                        break
                # Flow reports a rejected generation within seconds and then
                # never changes — waiting the full twenty minutes on it burns
                # the run for nothing. Match a real failure sentence, not the
                # bare word: "failed" sits in this page's chrome even while a
                # clip is rendering happily, and treating that as a refusal
                # once made the run abandon perfectly good generations.
                refused = re.search(
                    r"(generation failed|failed to generate|couldn't generate|"
                    r"could not generate|something went wrong|unable to generate)",
                    body, re.I)
                if (refused and not pct
                        and not page.get_by_role("button", name="Download").count()):
                    raise FlowRejected(
                        f"scene {scene_num}: Flow ne generation reject kar di "
                        f"({int(elapsed)}s) — prompt ya source image badalni padegi")
            except FlowRejected:
                raise
            except Exception:
                state.append("page busy")
            log(f"  scene {scene_num}: still waiting… {int(elapsed)}s "
                f"[{', '.join(state) or 'nothing on screen yet'}]")

        # The agent asks permission before spending credits, and nothing
        # generates until that card is answered.
        if not approved and _flow_click_approve(page):
            approved = True
            log(f"  scene {scene_num}: approval card accepted")
            page.wait_for_timeout(2000)
            continue

        got = _flow_collect_clip(page, scene_num, out_path, source_image)
        if got:
            return got

    raise TimeoutError(f"scene {scene_num}: no download appeared within 20 minutes")


def _flow_wait_and_download(page, scene_num, out_path, timeout_sec=1200, source_image=None):
    """Sit on an already-submitted project until its clip can be collected."""
    deadline = time.time() + timeout_sec
    beat = 0.0
    started = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(8000)
        _flow_click_approve(page)
        got = _flow_collect_clip(page, scene_num, out_path, source_image)
        if got:
            return got
        elapsed = time.time() - started
        if elapsed - beat >= 30:
            beat = elapsed
            log(f"  scene {scene_num}: purane project pe intezaar… {int(elapsed)}s")
    raise TimeoutError(f"scene {scene_num}: clip still not collectable after {timeout_sec}s")



def _clip_matches_source(clip_path, source_image, tolerance=24.0):
    """
    Is this clip actually the one generated from this scene's image?

    An image-to-video clip begins on its source frame, so comparing the first
    frame against the image the scene was built from is a direct check. Worth
    doing because the network fallback takes a video URL off the page, and a
    page can be showing more than one clip — two scenes ended up holding each
    other's footage before this existed.
    """
    try:
        from PIL import Image
    except ImportError:
        return True     # cannot check; do not block the download
    if not source_image or not os.path.isfile(source_image):
        return True
    try:
        tmp = clip_path + ".frame.png"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", clip_path,
             "-frames:v", "1", "-vf", "scale=32:18,format=gray", tmp],
            check=True, timeout=60, creationflags=_NO_WINDOW,
        )
        with Image.open(tmp) as im:
            a = list(im.convert("L").getdata())
        with Image.open(source_image) as im:
            b = list(im.convert("L").resize((32, 18)).getdata())
        os.remove(tmp)
        diff = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
        if diff > tolerance:
            log(f"  clip source image se match nahi karti (dist {diff:.0f}) — reject")
            return False
        return True
    except Exception:
        return True


def _flow_collect_clip(page, scene_num, out_path, source_image=None):
    """
    Get the finished clip out of whatever view we are on, or return None.

    Two routes, because neither works everywhere: the Download control is the
    reliable one once it exists, but Flow only renders it after the player has
    been engaged, and on some views it never appears at all — there the file is
    taken straight off the network instead. Downloading is the step that kept
    failing, and a failed download is what made the same scene get generated
    over and over, so both routes are tried before giving up.
    """
    try:
        dl_btn = page.get_by_role("button", name="Download")
        if not dl_btn.count():
            _flow_wake_player(page)
            dl_btn = page.get_by_role("button", name="Download")

        if not dl_btn.count() and "/edit/" not in page.url:
            # The Download control lives one level in, on /project/<id>/edit/<clip>.
            # A project page only shows tiles, so polling it for a download
            # button waits forever on a clip that is sitting right there —
            # which is exactly how scene 7 timed out after twenty minutes.
            try:
                tiles = page.locator("[role='button'] img, img[src*='blob'], video")
                if tiles.count():
                    tiles.first.click(timeout=8000)
                    page.wait_for_timeout(7000)
                    _flow_wake_player(page)
                    dl_btn = page.get_by_role("button", name="Download")
            except Exception:
                pass

        if dl_btn.count():
            try:
                with page.expect_download(timeout=60000) as dl_info:
                    dl_btn.last.click()
                dl_info.value.save_as(out_path)
                if os.path.getsize(out_path) > 10_000:
                    if not _clip_matches_source(out_path, source_image):
                        os.remove(out_path)
                    else:
                        log(f"  scene {scene_num}: saved -> {out_path}")
                        return out_path
            except Exception as e:
                log(f"  scene {scene_num}: download button ne kaam nahi kiya "
                    f"({type(e).__name__}) — network se try kar raha hoon")

        urls = page.evaluate(
            """() => [...document.querySelectorAll('video')]
                     .map(v => v.currentSrc || v.src)
                     .filter(u => u && !u.startsWith('blob:'))"""
        )
        for url in dict.fromkeys(urls):
            try:
                body = page.request.get(url, timeout=120000).body()
            except Exception:
                continue
            if len(body) < 50_000:
                continue
            with open(out_path, "wb") as f:
                f.write(body)
            if not _clip_matches_source(out_path, source_image):
                os.remove(out_path)
                continue        # is page pe koi aur video hai — agla try karo
            log(f"  scene {scene_num}: network se saved -> {out_path} "
                f"({len(body) / 1048576:.1f}MB)")
            return out_path
    except Exception as e:
        log(f"  scene {scene_num}: collect attempt failed ({type(e).__name__})")
    return None


def _flow_click_approve(page):
    """
    Answer Flow's "kick off this generation?" card.

    The choices are not buttons — they render as plain rows with a tick icon,
    so looking only for a button role never matched and the card sat there
    until the twenty-minute timeout, with nothing generating the whole time.
    Several strategies are tried, and "do not ask again" is deliberately
    avoided so the account keeps asking rather than being silently changed.
    """
    try:
        if "approve" not in (page.inner_text("body") or "").lower():
            return False
    except Exception:
        return False

    # The rows carry no role and no stable class — they are plain divs whose
    # text is the icon ligature plus the label ("check\nApprove"), sitting
    # beside a "Reject" row of exactly the same shape. Matching is therefore
    # done on the text itself, in the page, where the exact string is visible:
    # anything containing "reject" or "do not ask again" is never a candidate,
    # so a mis-click cannot cancel the generation.
    clicked = page.evaluate(
        """() => {
            const rows = [...document.querySelectorAll('div,span,button')].filter(e => {
                const t = (e.innerText || '').trim();
                if (!/^(check\\s*)?Approve$/i.test(t)) return false;
                return e.offsetParent !== null;
            });
            if (!rows.length) return null;
            // Deepest match is the label; its clickable row is a parent.
            const label = rows[rows.length - 1];
            const target = label.closest('[class*="sc-"]') || label;
            for (const el of [label, target, label.parentElement]) {
                if (!el) continue;
                const t = (el.innerText || '').toLowerCase();
                if (t.includes('reject') || t.includes('do not ask again')) continue;
                el.click();
                return (el.innerText || '').trim().slice(0, 30);
            }
            return null;
        }"""
    )
    if clicked:
        log(f"  approval card: '{clicked}' dabaya")
        return True

    # Fallback for a future markup change: the accessible-name routes.
    for loc in (page.get_by_role("button", name=re.compile(r"^\s*(check\s+)?Approve\s*$", re.I)),
                page.get_by_role("menuitem", name=re.compile(r"^\s*Approve\s*$", re.I))):
        try:
            item = loc.filter(has_not_text=re.compile(r"do not ask again|reject", re.I)).first
            if item.is_visible():
                item.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def _flow_debug_shot(page, scene_num, tag):
    """
    Save what the browser is showing when something goes wrong.

    The automation runs headful but unattended, so by the time a failure is
    noticed the screen has usually moved on. A picture of the actual moment is
    the difference between diagnosing this in one attempt and guessing.
    """
    try:
        shots = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_shots")
        os.makedirs(shots, exist_ok=True)
        path = os.path.join(shots, f"scene{scene_num:02d}_{tag}_{int(time.time())}.png")
        page.screenshot(path=path, full_page=False)
        log(f"  debug screenshot -> {path}")
    except Exception as e:
        log(f"  (could not save debug screenshot: {type(e).__name__})")


def _flow_submission_started(page, media_before, timeout_sec=45):
    """
    Wait for a positive sign that Flow accepted the generation.

    Deliberately does NOT inspect the compose box: submitting re-renders the
    page, and the handle held from before the click points at a detached node
    that still carries the typed text. Reading that stale node reported "the
    prompt never sent" for scenes Flow had in fact already started rendering.

    `media_before` is the number of result tiles counted before clicking, so a
    new one appearing is unambiguous evidence rather than a guess.
    """
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            # Nothing starts until the credit-approval card is answered, so
            # answering it here counts as the submission having gone through.
            if _flow_click_approve(page):
                log("  approval card accepted")
                page.wait_for_timeout(2000)
                return True
            if _flow_media_count(page) > media_before:
                return True
            if page.get_by_role("button", name="Download").count():
                return True
            body = page.inner_text("body") or ""
            # While a clip renders, Flow labels the tile with nothing but a
            # percentage — no "generating", no player, no buttons. Missing this
            # one signal is what made a run that was already 37% done look like
            # it had never submitted.
            if re.search(r"\b\d{1,3}\s?%", body):
                return True
            if any(w in body.lower() for w in
                   ("generating", "queued", "in queue", "processing", "creating")):
                return True
        except Exception:
            pass
        page.wait_for_timeout(3000)
    return False


def _flow_media_count(page):
    """How many result tiles the project is showing right now."""
    try:
        return page.locator("video").count() + page.locator("img[src*='blob']").count()
    except Exception:
        return 0


def _flow_wake_player(page):
    """
    Nudge the newest result so its controls render.

    Tries the cheap, non-destructive things in order — scroll it into view,
    hover it, start playback via the DOM — and stops at the first that works.
    Playing through the element rather than clicking avoids hitting whatever
    else may sit under the cursor position.
    """
    try:
        videos = page.locator("video")
        if videos.count():
            last = videos.last
            last.scroll_into_view_if_needed(timeout=5000)
            last.hover(timeout=5000)
            # muted play: autoplay policies block sound-on playback started
            # without a real user gesture, and a muted play still loads the file.
            last.evaluate("v => { v.muted = true; const p = v.play(); if (p) p.catch(() => {}); }")
            page.wait_for_timeout(1500)
            return
    except Exception:
        pass

    # No <video> yet — the clip may still be a result card that reveals its
    # controls on hover.
    try:
        for name in ("play_arrow", "Play", "movie"):
            card = page.get_by_role("button", name=name)
            if card.count():
                card.last.hover(timeout=5000)
                card.last.click(timeout=5000)
                page.wait_for_timeout(1500)
                return
    except Exception:
        pass


def submit_scene_video(page, scene, clips_dir, out_dir, aspect_ratio, speaker_look=None):
    """
    Send one scene to Flow and return without waiting for the render.

    Split out from the wait so several windows can each have a job in flight at
    the same time: submit on every window first, then collect from whichever
    finishes. One account renders them in parallel, so N windows take about as
    long as one clip used to.
    """
    scene_num = scene["scene_number"]
    out_path = os.path.join(clips_dir, f"scene_{scene_num:02d}.mp4")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
        return out_path, None

    image_path = scene.get("image_path")
    if not image_path:
        raise RuntimeError("no image_path yet — run --images first")

    _flow_dismiss_banners(page)

    # A fresh project per clip costs a click and nine seconds of waiting every
    # time — six minutes across a film — and scatters the clips over dozens of
    # projects, which is what made recovering a stalled run so painful. With
    # REUSE_PROJECT a window makes one project and keeps submitting into it.
    # Off by default: the download step picks a clip out of the open project,
    # and it has only ever been exercised against a project holding one.
    in_project = "/project/" in page.url
    if not (REUSE_PROJECT and in_project):
        new_project = page.get_by_role("button", name="New project")
        new_project.first.wait_for(state="visible", timeout=60_000)
        new_project.first.scroll_into_view_if_needed()
        new_project.first.click()
        page.wait_for_timeout(9000)

    seconds = int(scene.get("duration_sec") or 10)
    _flow_configure(page, aspect_ratio, duration=f"{seconds}s", count="x1")
    prompt_text = _flow_prompt_for(scene, seconds, speaker_look)

    if NO_AGENT:
        # Flow's plain prompt bar, with no agent in the loop. The agent path
        # below is metered by a daily Agent quota that is separate from
        # credits, and when it runs out generations stop finishing with no
        # error at all — acc5 sat on 858 credits unable to produce a clip.
        # This path was checked on that same spent account and a generation
        # started (858 -> 846 credits), so it is not the agent under another
        # name.
        file_input = page.locator("input[type=file]")
        if not file_input.count():
            raise RuntimeError("no file input on the plain prompt bar")
        file_input.first.set_input_files(os.path.join(out_dir, image_path))
        page.wait_for_timeout(7000)

        # The page's first textarea belongs to reCAPTCHA and is never visible;
        # clicking it hangs until the timeout.
        box = page.locator("textarea:visible, [contenteditable=true]:visible")
        if not box.count():
            raise RuntimeError("no visible prompt input on the plain bar")
        prompt_box = box.last
        prompt_box.click()
        try:
            prompt_box.fill(prompt_text)
        except Exception:
            prompt_box.type(prompt_text, delay=8)
        page.wait_for_timeout(800)
    else:
        _flow_upload_image(page, os.path.join(out_dir, image_path))

        prompt_box = page.locator("[contenteditable=true]").first
        prompt_box.click()
        prompt_box.type("@", delay=80)
        page.wait_for_timeout(2500)
        add_to_prompt = page.get_by_text("Add to Prompt")
        if not add_to_prompt.count():
            raise RuntimeError("could not find 'Add to Prompt' — image would be ignored")
        add_to_prompt.first.click()
        page.wait_for_timeout(2000)

        prompt_box.click()
        prompt_box.type(prompt_text, delay=10)
        page.wait_for_timeout(500)

    create_btn = page.get_by_role("button", name="arrow_forward Create")
    if not create_btn.count():
        create_btn = page.get_by_role("button", name="Create")
    if not create_btn.count():
        _flow_debug_shot(page, scene_num, "no-create-button")
        raise RuntimeError(f"scene {scene_num}: no Create button on screen")
    create_btn.last.click()
    page.wait_for_timeout(4000)
    _flow_click_approve(page)
    return None, page.url


# The language the characters speak, taken from the story rather than assumed.
# It was hardcoded to Hindi, which is right for the shorts this pipeline was
# built for and wrong for anything else: an English film's lines were being
# handed to Flow with an instruction to perform them in Hindi. Set once by
# run_videos before any window starts, so the worker threads only read it.
SPEECH_LANGUAGE = "Hindi"

# Keep submitting into the project a window already opened, instead of making
# a new one per clip. Set by --one-project before any window starts.
REUSE_PROJECT = False

# Submit through Flow's plain prompt bar instead of its chat agent. The agent
# path is metered by a daily Agent quota separate from credits; the plain bar
# is not. Verified on acc5 with its Agent quota already spent: a generation
# still started and credits moved 858 -> 846.
NO_AGENT = False


def _flow_prompt_for(scene, seconds, speaker_look=None):
    """The text sent to Flow for one scene."""
    dialogue = (scene.get("dialogue") or "").strip()
    if not dialogue:
        return (
            f"Animate this image into a {seconds}-second video. No one speaks and no mouths move — "
            "the characters stay silent. Animate ONLY what is already visible in the image — "
            "do NOT add any people, characters or figures that are not already in the frame. "
            "Bring whoever is already there to life with natural body movement, gestures and "
            "expressions, plus gentle camera motion. If the scene has no people in it, it must "
            "stay empty: animate the light and the camera only."
        )
    speaker = (scene.get("speaker") or "").strip()
    if not speaker:
        return (f"Animate this image into a {seconds}-second video. "
                f"The character speaks this dialogue: \"{dialogue}\"")
    who = f"{speaker} ({speaker_look})" if speaker_look else speaker
    text = (
        f"Animate this image into a {seconds}-second video. "
        f"ONLY {who} speaks. {speaker} says this line in Hindi: \"{dialogue}\". "
        f"Every other character in the frame stays SILENT with their mouth closed — "
        f"they may react with expressions and small movements, but must not talk."
    )
    voice = (scene.get("voice_hint") or "").strip()
    if voice:
        text += f" {speaker}'s voice should sound like {voice}."
    return text


def run_videos_parallel(context, first_page, profile, todo, ordered, data, scenes_path,
                        clips_dir, out_dir, aspect_ratio, windows, failed_scenes,
                        download_only=False, budget=None):
    """
    Keep `windows` scenes rendering at once inside one account.

    Playwright's sync API is not thread-safe, so the windows are driven from a
    single loop rather than threads: submit on each free window, then sweep them
    all for finished clips. Flow renders the jobs concurrently either way, which
    is where the time is actually saved — one clip took 7-10 minutes whether or
    not anything else was in flight.
    """
    chars = {c["name"]: (c.get("short_desc") or "").replace("NOT a human — ", "")[:110]
             for c in data.get("characters", [])}
    queue = [(i, s) for i, s in todo
             if s["scene_number"] not in failed_scenes and not s.get("video_path")]
    if download_only:
        # Only scenes whose generation already happened here — a project opens
        # nowhere else, so these must be collected on this account or lost.
        queue = [(i, s) for i, s in queue if s.get("video_project_url")
                 and (s.get("video_account") or profile["name"]) == profile["name"]]
        log(f"  download-only mode: {len(queue)} scene(s) uthane hain")

    # Only take on what this account can pay for. Scenes left in the queue are
    # not lost — pending() rebuilds it for the next profile, which reads its own
    # balance and takes its own share. Collecting an already-submitted clip
    # costs nothing, so those are never counted against the budget.
    if budget is not None and not download_only:
        fresh = [(i, s) for i, s in queue if not s.get("video_project_url")]
        if len(fresh) > budget:
            keep = {id(s) for _i, s in queue if s.get("video_project_url")}
            keep |= {id(s) for _i, s in fresh[:budget]}
            dropped = len(fresh) - budget
            queue = [(i, s) for i, s in queue if id(s) in keep]
            log(f"  '{profile['name']}' ke credits {budget} clip ke hain — "
                f"baaki {dropped} scene agli profile pe jayenge")

    # Extra windows are a throughput optimisation, not a requirement: the run
    # works with one. Letting a failure here propagate meant a slow page load
    # while opening window four destroyed a 48-clip run that had not yet
    # submitted anything. Take however many windows actually open.
    pages = [first_page]
    while len(pages) < windows:
        pg = None
        try:
            pg = context.new_page()
            pg.goto(FLOW_URL, timeout=NAV_TIMEOUT)
            pg.wait_for_timeout(4000)
            pages.append(pg)
        except Exception as e:
            log(f"  window {len(pages) + 1} nahi khuli ({type(e).__name__}) — "
                f"{len(pages)} window se hi chala lenge")
            if pg is not None:
                try:
                    pg.close()
                except Exception:
                    pass
            break
    log(f"  {len(pages)} window(s) khuli — {len(queue)} scene(s) katar mein")

    inflight = {}       # page index -> (scene, out_path, started_at)
    retries = {}        # scene_number -> how many times it was resubmitted
    out_of_quota = False
    # A generation genuinely can sit in Flow's queue for twenty minutes, so a
    # long deadline is right when something was just submitted. Collecting is
    # the opposite case: the render either finished earlier or never started,
    # and nothing about waiting changes which. An account that ran out of
    # credits mid-run leaves behind project ids for jobs Flow accepted and
    # never rendered, and the full deadline turns six of those into two and a
    # half hours of an idle browser.
    deadline_per_scene = 180 if download_only else 1500

    while queue or inflight:
        # Fill every idle window.
        for idx, pg in enumerate(pages):
            if idx in inflight or not queue or out_of_quota:
                continue
            i, scene = queue.pop(0)
            num = scene["scene_number"]
            try:
                if not pg.is_closed():
                    pg.close()
                pages[idx] = pg = context.new_page()
                pg.goto(FLOW_URL, timeout=NAV_TIMEOUT)
                pg.wait_for_timeout(4000)

                # Checked before every submit, not once at the start: credits
                # drain as the run goes, and the account has to be left while it
                # can still render a full-quality clip.
                #
                # Skipped entirely on a download-only pass. Nothing is submitted
                # there — the clips were paid for on an earlier run and are only
                # being fetched — so an empty account is not a reason to stop.
                # Checking here anyway abandoned six finished clips on the one
                # account that could still hand them over.
                if not download_only and quota_exhausted(pg):
                    log(f"  profile '{profile['name']}': aage ke scenes agli profile pe jayenge")
                    queue.insert(0, (i, scene))
                    out_of_quota = True
                    break
                left = credits_left(pg)
                if left is not None:
                    log(f"  window {idx + 1} | scene {num}: {left} credit bache hain")

                existing = scene.get("video_project_url")
                owner = scene.get("video_account")
                if existing and owner and owner != profile["name"]:
                    log(f"  window {idx + 1} | scene {num}: iska project '{owner}' ka hai — "
                        f"wahin se uthega, yahan chhod raha hoon")
                    continue
                out_path = os.path.join(clips_dir, f"scene_{num:02d}.mp4")
                if existing:
                    log(f"  window {idx + 1} | scene {num}: pehle bheji hui generation se clip lunga")
                    pg.goto(existing, timeout=90000)
                    pg.wait_for_timeout(7000)
                else:
                    look = chars.get(scene.get("speaker"), "")
                    done, url = submit_scene_video(pg, scene, clips_dir, out_dir,
                                                   aspect_ratio, speaker_look=look)
                    if done:
                        _record_clip(scene, done, out_dir, data, scenes_path)
                        continue
                    scene["video_project_url"] = url
                    scene["video_account"] = profile["name"]
                    save_scenes(scenes_path, data)
                    log(f"  window {idx + 1} | scene {num}: bheja gaya "
                        f"({url.rstrip('/').split('/')[-1][:8]}) — dobara generate nahi hoga")
                inflight[idx] = (scene, out_path, time.time())
            except Exception as e:
                log(f"  window {idx + 1} | scene {num}: submit fail ({type(e).__name__}: {str(e)[:70]})")
                failed_scenes.add(num)

        if not inflight:
            break

        time.sleep(12)

        # Sweep for anything that has finished.
        for idx in list(inflight):
            scene, out_path, started = inflight[idx]
            num = scene["scene_number"]
            pg = pages[idx]
            try:
                _flow_click_approve(pg)
                got = _flow_collect_clip(pg, num, out_path,
                                         os.path.join(out_dir, scene.get("image_path") or ""))
                if got:
                    _record_clip(scene, got, out_dir, data, scenes_path)
                    del inflight[idx]
                    continue
                waited = time.time() - started
                # A clip that is going to arrive arrives in a couple of minutes;
                # the ones measured here landed between 60s and 250s. Past a few
                # multiples of that it is not slow, it is wedged, and Flow gives
                # no signal to tell the two apart. Waiting out the full deadline
                # on a wedged job costs twenty minutes; resubmitting costs one
                # clip's credits and usually lands. Once only — if the second
                # attempt also wedges, something about the scene is wrong and
                # burning credits in a loop will not fix it.
                if (not download_only and waited > STRAGGLER_SEC
                        and retries.get(num, 0) < 1):
                    retries[num] = retries.get(num, 0) + 1
                    log(f"  window {idx + 1} | scene {num}: {int(waited)}s se atki hai — "
                        f"chhod kar dobara bhej raha hoon")
                    # Blanked, not deleted. save_scenes merges anything the
                    # in-memory scene is MISSING back in from disk — that is
                    # what stops one run erasing another's work. A popped key
                    # is indistinguishable from a key that was never set, so
                    # popping here handed the stale project straight back and
                    # the "resubmit" quietly re-collected the same wedged job.
                    # A present-but-empty value survives the merge and reads as
                    # falsy everywhere downstream.
                    scene["video_project_url"] = None
                    scene["video_account"] = None
                    save_scenes(scenes_path, data)
                    del inflight[idx]
                    queue.append((0, scene))
                elif waited > deadline_per_scene:
                    log(f"  window {idx + 1} | scene {num}: {int(waited)}s ho gaye — "
                        f"project yaad hai, baad mein utha lenge")
                    del inflight[idx]
                elif int(waited) % 60 < 13:
                    log(f"  window {idx + 1} | scene {num}: {int(waited)}s")
            except Exception as e:
                log(f"  window {idx + 1} | scene {num}: check fail ({type(e).__name__})")
                del inflight[idx]

    return out_of_quota


def _record_clip(scene, path, out_dir, data, scenes_path):
    scene["video_path"] = os.path.relpath(path, out_dir)
    scene.pop("video_instruction", None)
    scene.pop("video_project_url", None)
    scene.pop("video_account", None)
    save_scenes(scenes_path, data)
    log(f"  scene {scene['scene_number']}: saved -> {path}")


def run_videos(scenes_path, windows=1, no_new_on=(), only_on=(),
               per_clip=CREDITS_PER_CLIP, scene_range=None):
    """
    Generates a clip per scene in Flow, rotating through every browser profile
    in profiles.json as each account's free quota runs out — so the whole
    video can be produced across several free accounts instead of stopping at
    the first limit.
    """
    data = load_scenes(scenes_path)
    out_dir = os.path.dirname(os.path.abspath(scenes_path))
    clips_dir = os.path.join(out_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    aspect_ratio = data.get("aspect_ratio", "16:9")

    global SPEECH_LANGUAGE
    SPEECH_LANGUAGE = (data.get("language") or "Hindi").strip().capitalize()
    log(f"dialogue language: {SPEECH_LANGUAGE}")
    if REUSE_PROJECT:
        log("one-project mode: har window apne hi project mein kaam karegi")

    profiles = load_profiles()
    if only_on:
        profiles = [p for p in profiles if p["name"] in only_on]
    log(f"{len(profiles)} browser profile(s) available: {', '.join(p['name'] for p in profiles)}")

    ordered = sorted(data["scenes"], key=lambda s: s["scene_number"])

    # Two runs started side by side to go faster will both walk this list from
    # the top and generate the same scenes twice, paying for each of them —
    # nothing here coordinates between processes. A range makes the split
    # explicit, so one run can take the first half and another the second.
    if scene_range:
        lo, hi = scene_range
        ordered = [s for s in ordered if lo <= s["scene_number"] <= hi]
        log(f"scene range {lo}-{hi}: {len(ordered)} scene(s) is run ke hisse mein")

    # Scenes that kept failing for reasons unrelated to quota. Tracked so a
    # broken scene does not block the queue, and so the next profile does not
    # start over on it either.
    failed_scenes = set()

    def pending():
        return [(i, s) for i, s in enumerate(ordered)
                if not s.get("video_path") and s.get("image_path")
                and s["scene_number"] not in failed_scenes]

    with sync_playwright() as p:
        for profile in profiles:
            todo = pending()
            if not todo:
                break

            log(f"--- profile '{profile['name']}' ({profile.get('browser')}"
                f"{'/' + profile['channel'] if profile.get('channel') else ''}): {len(todo)} scene(s) left ---")
            if browser_is_running(profile):
                log(f"  SKIPPED: this is a real browser profile and that browser is currently OPEN.")
                log(f"  Close it completely (all windows + tray icon) and re-run to use this profile.")
                continue

            # An account that is out of credits can still hand over clips it
            # already made, but must not be given new work — Flow does not
            # refuse, it just returns a degraded render.
            if profile["name"] in no_new_on:
                has_saved = any(s.get("video_project_url")
                                and (s.get("video_account") or profile["name"]) == profile["name"]
                                for _i, s in todo)
                if not has_saved:
                    log(f"  '{profile['name']}' credit khatam bataya gaya hai aur yahan koi purani "
                        f"generation baaki nahi — skip")
                    continue
                log(f"  '{profile['name']}': sirf purani bani clips download hongi, naya kuch nahi banega")
            try:
                context = launch_profile(p, profile)
            except Exception as e:
                log(f"  could not launch profile '{profile['name']}' ({e}) — skipping to next")
                continue

            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(FLOW_URL, timeout=NAV_TIMEOUT)
                page.wait_for_timeout(3000)

                # Both checks below decide whether this account may be given NEW
                # work. Neither applies to a download-only pass: the clips it
                # collects were paid for on an earlier run, and fetching them
                # costs nothing. Running these first stranded six finished
                # clips on an account that was out of credits — the account
                # could not render again, so it was skipped, and no other
                # account will touch a project it does not own.
                collect_only = profile["name"] in no_new_on
                budget = None
                if not collect_only:
                    if quota_exhausted(page):
                        log(f"  profile '{profile['name']}' already shows no quota — moving to next profile")
                        continue

                    # Price the account before giving it work. Reading the
                    # balance here costs one page read and decides how much of
                    # the queue this profile is handed, instead of discovering
                    # the limit by submitting into it and having the refused
                    # scenes re-queued.
                    budget = clip_budget(page, per_clip)
                    if budget is not None:
                        log(f"  '{profile['name']}': ~{budget} clip ki gunjaish")
                        if budget == 0:
                            log(f"  '{profile['name']}': naya kuch nahi ban sakta — agli profile")
                            continue

                if windows > 1:
                    run_videos_parallel(context, page, profile, todo, ordered, data,
                                        scenes_path, clips_dir, out_dir, aspect_ratio,
                                        windows, failed_scenes,
                                        download_only=collect_only,
                                        budget=budget)
                    continue

                consecutive_failures = 0
                queue = list(todo)
                while queue:
                    i, scene = queue[0]
                    if scene["scene_number"] in failed_scenes or scene.get("video_path"):
                        queue.pop(0)
                        continue
                    next_scene = ordered[i + 1] if i + 1 < len(ordered) else None
                    try:
                        # A finished clip leaves the tab inside that project's
                        # editor, which has no "New project" button, and Flow
                        # is a SPA so re-navigating the same tab does not
                        # reliably reset it. A fresh tab per scene does.
                        if not page.is_closed():
                            page.close()
                        page = context.new_page()
                        page.goto(FLOW_URL, timeout=NAV_TIMEOUT)
                        page.wait_for_timeout(5000)

                        def remember_project(url, _scene=scene):
                            """Persist the project as soon as Flow accepts the job."""
                            _scene["video_project_url"] = url
                            _scene["video_account"] = profile["name"]
                            save_scenes(scenes_path, data)
                            log(f"  scene {_scene['scene_number']}: project noted "
                                f"({url.rstrip('/').split('/')[-1][:8]}) — dobara generate nahi hoga")

                        # A short visual description of the speaker, so Flow can
                        # tell which figure in the frame is meant to be talking.
                        look = ""
                        for c in data.get("characters", []):
                            if c["name"] == scene.get("speaker"):
                                look = (c.get("short_desc") or "").replace("NOT a human — ", "")[:110]
                                break

                        path = generate_scene_video(page, scene, next_scene, clips_dir, out_dir,
                                                    aspect_ratio, on_submit=remember_project,
                                                    speaker_look=look)
                        scene["video_path"] = os.path.relpath(path, out_dir)
                        scene.pop("video_instruction", None)  # clear after use
                        # The clip is on disk now, so the project pointer has
                        # done its job and would only mislead a later run.
                        scene.pop("video_project_url", None)
                        scene.pop("video_account", None)
                        save_scenes(scenes_path, data)
                        consecutive_failures = 0
                        queue.pop(0)
                    except FlowRejected as e:
                        # A refusal is about this prompt or this image, so the
                        # same request will be refused on every account. Retrying
                        # it only spends other profiles' quota to be told no
                        # again — drop it and let the run finish the rest.
                        log(f"  {e}")
                        failed_scenes.add(scene["scene_number"])
                        scene.pop("video_project_url", None)
                        scene.pop("video_account", None)
                        save_scenes(scenes_path, data)
                        consecutive_failures = 0
                        queue.pop(0)
                        continue
                    except Exception as e:
                        consecutive_failures += 1
                        log(f"  scene {scene['scene_number']}: FAILED ({e})")

                        # A crashed tab poisons every later scene on this
                        # profile, so give the page a fresh start rather than
                        # burning through the remaining scenes on a dead tab.
                        if "crash" in str(e).lower() or page.is_closed():
                            log("  page crashed — reopening a fresh tab and continuing")
                            try:
                                page = context.new_page()
                                page.goto(FLOW_URL, timeout=NAV_TIMEOUT)
                                page.wait_for_timeout(5000)
                                # Deliberately NOT resetting consecutive_failures:
                                # a tab that keeps crashing would otherwise retry
                                # the same scene forever.
                            except Exception as reopen_error:
                                log(f"  could not reopen ({reopen_error}) — switching profile")
                                break
                            continue

                        if quota_exhausted(page):
                            log(f"  profile '{profile['name']}' is out of quota — switching to the next profile")
                            break

                        # Anything short of a real quota wall is retried on the
                        # SAME account. Switching on a generic failure meant a
                        # second profile started generating a scene the first
                        # one already had in flight — the same clip rendered
                        # twice, on two accounts, burning both quotas.
                        if consecutive_failures >= RETRY_PER_SCENE:
                            log(f"  scene {scene['scene_number']}: {consecutive_failures} attempts failed on "
                                f"'{profile['name']}' without a quota message — chhod kar aage badh raha hoon")
                            failed_scenes.add(scene["scene_number"])
                            consecutive_failures = 0
                            queue.pop(0)
                            continue
                        log(f"  scene {scene['scene_number']}: retrying on the same profile "
                            f"({consecutive_failures}/{RETRY_PER_SCENE})")
                        page.wait_for_timeout(5000)
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    left = pending()
    if left:
        log(f"Video pass finished, but {len(left)} scene(s) still have no clip "
            f"(scenes: {', '.join(str(s['scene_number']) for _i, s in left)}).")
        log("Add more profiles to profiles.json (and log each one in) then re-run to continue.")
    else:
        log("Video pass complete — every scene has a clip.")


def main():
    parser = argparse.ArgumentParser(description="Automate ChatGPT image + Google Flow video generation for scenes.json")
    parser.add_argument("scenes_file", nargs="?", help="Path to a scenes.json")
    parser.add_argument("--login", action="store_true", help="Open a browser profile to log into Google Flow (close the window when done)")
    parser.add_argument("--profile", help="Which profile from profiles.json to log in (default: the first one)")
    parser.add_argument("--list-profiles", action="store_true", help="Show the configured browser profiles and exit")
    parser.add_argument("--images", action="store_true", help="Generate + download scene images")
    parser.add_argument("--videos", action="store_true", help="Generate + download scene videos (Google Flow)")
    parser.add_argument("--no-new-on", default="", metavar="acc1,acc2",
                        help="Accounts that are out of credits. Clips already generated there are "
                             "still downloaded — a project only opens in the account that made it — "
                             "but nothing new is submitted, so no low-quality renders are produced.")
    parser.add_argument("--only-on", default="", metavar="acc2",
                        help="Sirf in profiles pe chalo (comma se alag)")
    parser.add_argument("--per-clip", type=int, default=CREDITS_PER_CLIP, metavar="N",
                        help=f"Ek clip ka credit kharch, budget nikalne ke liye "
                             f"(default {CREDITS_PER_CLIP})")
    parser.add_argument("--windows", type=int, default=1, metavar="N",
                        help="How many scenes to generate at once in the SAME account, each in its "
                             "own window. Flow renders them in parallel, so N windows finish N "
                             "clips in about the time one used to take.")
    parser.add_argument("--no-agent", action="store_true",
                        help="Flow ke chat agent ke bajaye normal prompt bar se "
                             "bhejo — Agent quota nahi katati, sirf credits lagte hain.")
    parser.add_argument("--one-project", action="store_true",
                        help="Har window ek hi project mein kaam kare, har clip "
                             "ke liye naya na banaye — 9 second per clip bachta "
                             "hai aur clips bikharti nahi.")
    parser.add_argument("--scenes", default="", metavar="LO-HI",
                        help="Sirf is range ke scene banao, jaise 20-40. Do run "
                             "parallel chalane ke liye zaroori — warna dono ek hi "
                             "scene par credits kharch karenge.")
    parser.add_argument("--character-note", default="", help="Extra text appended to every image prompt for consistency")
    parser.add_argument("--image-source", choices=["flow", "chatgpt"], default="flow", help="Where to generate scene images (default: flow)")
    args = parser.parse_args()

    if args.list_profiles:
        for prof in load_profiles():
            log(f"{prof['name']:<12} {prof.get('browser')}"
                f"{'/' + prof['channel'] if prof.get('channel') else '':<10} "
                f"({ {'yes': 'logged in', 'no': 'not logged in yet', 'in use': 'in use by a running browser'}[profile_has_google_login(prof)] })")
        return

    if args.login:
        with sync_playwright() as p:
            do_login(p, args.profile)
        return

    if not args.scenes_file:
        parser.print_help()
        sys.exit(1)

    if not (args.images or args.videos):
        print("Pass --images and/or --videos.")
        sys.exit(1)

    if args.images:
        run_images(args.scenes_file, args.character_note, source=args.image_source)
    # global, or these only ever set locals and the flags do nothing.
    global REUSE_PROJECT, NO_AGENT
    if args.one_project:
        REUSE_PROJECT = True
    if args.no_agent:
        NO_AGENT = True

    scene_range = None
    if args.scenes:
        try:
            lo, hi = (int(x) for x in args.scenes.split("-", 1))
        except ValueError:
            sys.exit(f"--scenes ka format LO-HI hona chahiye, mila: {args.scenes!r}")
        scene_range = (lo, hi)

    if args.videos:
        run_videos(args.scenes_file, windows=max(1, args.windows),
                   no_new_on={n.strip() for n in args.no_new_on.split(",") if n.strip()},
                   only_on={n.strip() for n in args.only_on.split(",") if n.strip()},
                   per_clip=args.per_clip, scene_range=scene_range)


if __name__ == "__main__":
    main()

"""
DASH — the whole pipeline behind one page.

The stages are good at their jobs and bad at being watched. They print into a
terminal that scrolls away, they take their settings as flags nobody remembers,
and the two that take real time spend it inside a browser where the only signal
is a grid slowly filling in. This puts a script box, the reference image, the
clip/still mix and every stage's progress in one place.

It runs the same `pipeline.py` commands you would type; nothing is reimplemented
here, so a film made from this page and a film made from the shell are the same
film. One job at a time, on purpose — two stages driving browser profiles at
once is how a batch ends up in the wrong account.

    python dash.py --port 5154

The only stage that spends money is `clips`, and this page will not start it
without a dry run first: the button stays a dry run until the pairing count
comes back and you confirm it.
"""
import argparse
import base64
import json
import math
import os
import glob
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

# This process runs as pythonw (no console) under the packaged app, so a bare
# subprocess.run/Popen still gets Windows its own console window per child —
# a flash on every taskkill/powershell/ffmpeg call unless suppressed.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

import env                       # redirects the project root; must come first
from env import AI_PIPE, CLIP_SECONDS, CREDITS_PER_CLIP
from common import probe
import mix
import batch                 # the queue; this page only reads and unlocks it
import studio_api            # local helpers: projects, refs, profiles
import updater               # in-app updates from GitHub Releases
import tts                   # Voice panel: user-key TTS providers (v1.2.0)


SUPERVISOR_IPC_PORT = 5153


def restart_server():
    """Ask the supervisor (StickFilmStudio.exe) to restart both servers.

    When no supervisor is running (dev / standalone) there is nothing to signal,
    so the caller just reloads the page — which re-fetches all state and clears a
    stuck UI."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(("127.0.0.1", SUPERVISOR_IPC_PORT))
        s.sendall(b"RESTART\n")
        s.close()
        return {"ok": True, "method": "supervisor"}
    except Exception:
        return {"ok": True, "method": "reload"}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
try:
    import userdata as _ud
    _ud.setup()
except Exception:
    pass

PROJECTS = os.environ.get("STICKFILM_PROJECTS") or os.path.join(HERE, "projects")
REFS = os.environ.get("STICKFILM_REFS") or os.path.join(HERE, "refs")
# The user's looks: the shipped ones are seeded there, and categories the
# buyer makes from a prompt are saved there. Program Files cannot be written.
LOOKS = os.environ.get("STICKFILM_LOOKS") or os.path.join(HERE, "looks")
PROFILES_FILE = os.path.join(AI_PIPE, "profiles.json")


def _add_new_looks():
    """Copy built-in looks that are missing from the user's looks folder.

    userdata seeds the folder only while it is empty, so a style added in a
    later version would never reach an existing install. Only missing files
    are copied: a look the user already has, or made, is never touched.
    """
    try:
        src = os.path.join(ROOT_DIR, "vidgen", "looks")
        if not os.path.isdir(src) or os.path.normcase(os.path.abspath(src)) == \
                os.path.normcase(os.path.abspath(LOOKS)):
            return
        os.makedirs(LOOKS, exist_ok=True)
        import shutil as _sh
        for name in os.listdir(src):
            s, d = os.path.join(src, name), os.path.join(LOOKS, name)
            # A category the buyer deleted sits in looks/_trash: never bring it back.
            trashed = os.path.isdir(os.path.join(LOOKS, "_trash")) and any(
                t.startswith(name[:-5] + "_") for t in os.listdir(os.path.join(LOOKS, "_trash")))
            if name.endswith(".json") and os.path.isfile(s) and not os.path.exists(d) and not trashed:
                _sh.copy2(s, d)
    except Exception as exc:
        print("[looks] could not add new looks: %s" % exc, flush=True)


_add_new_looks()

for p_dir in [
    r"C:\Users\sting\AppData\Local\ffmpeg\bin",
    os.path.join(ROOT_DIR, "tools"),
    os.path.dirname(sys.executable),
    r"D:\AI Autometa\bin",
]:
    if os.path.isdir(p_dir) and p_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p_dir + os.pathsep + os.environ.get("PATH", "")

DEFAULTS = {
    "voice": "elevenlabs_XNJDyPyJ8r4J7de0WJwB",   # AI33 Pro "sudhir" — a valid default for any account
    "profile": "acc1",
    "engine": "omniflash",
    "aspect": "landscape",
    "seconds": CLIP_SECONDS,
    "batch": 50,
    "jobs": 8,
    "look_mode": "2d",
    "clip_share": 0.0,     # Default to pure stills mode (no clips)
    "clip_secs": 10.0,
    "still_secs": 2.3,
    "placement": "front",
}


# ── the one running job ──────────────────────────────────────────────────────

class Job:
    """A single stage, running as the subprocess it would be in a shell."""

    def __init__(self, name="main"):
        self.name = name
        self.profile = None
        self.log_file = None
        self.proc = None
        self.stage = None
        self.started = 0.0
        self.lines = deque(maxlen=400)
        self.code = None
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, stage, argv, cwd=HERE):
        with self.lock:
            if self.running:
                return False, f"{self.stage} is still running"
            self.stage, self.code, self.started = stage, None, time.time()
            self.user_stopped = False
            self.profile = argv[argv.index("--profile") + 1] if "--profile" in argv[:-1] else None
            # Which film this run makes.
            self.project = (argv[argv.index("--name") + 1] if "--name" in argv[:-1]
                            else (Handler.project if self.name == "main" else None))
            self.lines.clear()
            self._open_log(stage)
            # Quote it the way a shell would: a project name with a space in
            # it reads as three arguments otherwise, which sends anyone
            # debugging a failed stage after the wrong thing.
            import shlex
            self.lines.append("$ " + " ".join(shlex.quote(a) for a in argv[1:]))
            self._log(self.lines[-1])
            proc_env = os.environ.copy()
            proc_env["PYTHONUNBUFFERED"] = "1"
            self.proc = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=proc_env,
                creationflags=_NO_WINDOW)
            threading.Thread(target=self._pump, daemon=True).start()
            return True, "started"

    def _pump(self):
        proc = self.proc
        for line in proc.stdout:
            self.lines.append(line.rstrip("\n"))
            self._log(self.lines[-1])
        proc.wait()
        self.code = proc.returncode
        self.lines.append(f"[exit {self.code}]")
        self._log(self.lines[-1])
        self._close_log()

    # The screen keeps the last 400 lines; the whole run goes to a file, so a
    # traceback past the window can still be copied and read later.
    LOG_FILE = None

    def _open_log(self, stage):
        self._close_log()
        try:
            d = os.path.join(_data_dir(), "logs")
            os.makedirs(d, exist_ok=True)
            tag = "" if self.name == "main" else self.name + "_"
            path = os.path.join(d, tag + "last_run.log")
            if os.path.exists(path):
                os.replace(path, os.path.join(d, tag + "previous_run.log"))
            self._logf = open(path, "w", encoding="utf-8", errors="replace")
            self._logf.write("# %s  stage=%s  version=%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), stage, version_info().get("version", "")))
            self.log_file = path
            if self.name == "main":
                Job.LOG_FILE = path
        except Exception:
            self._logf = None

    def _log(self, line):
        f = getattr(self, "_logf", None)
        if f:
            try:
                f.write(line + "\n")
                f.flush()
            except Exception:
                pass

    def _close_log(self):
        f = getattr(self, "_logf", None)
        self._logf = None
        if f:
            try:
                f.close()
            except Exception:
                pass

    def stop(self):
        self.user_stopped = True
        profile = self.profile
        stopped = False
        if self.proc:
            pid = self.proc.pid
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=_NO_WINDOW)
                else:
                    self.proc.terminate()
                stopped = True
            except Exception:
                try:
                    self.proc.kill()
                    stopped = True
                except Exception:
                    pass
        # Only this lane's browser: the others are still drawing their films.
        others = any(l.running for l in LANES if l is not self)
        if others and profile:
            kill_automation_browsers(profile)
        elif not others:
            kill_automation_browsers()
        self.code = -9
        self.lines.append("[stopped forcefully by user]")
        return stopped

    def snapshot(self):
        return {
            "stage": self.stage,
            "running": self.running,
            "code": self.code,
            "seconds": round(time.time() - self.started, 1) if self.started else 0,
            "lines": list(self.lines),
        }


def kill_automation_browsers(profile=None):
    """Kill the Chrome instances stickfilm automation opened - all of them, or
    only the one on `profile` when other lanes are still using theirs."""
    if sys.platform == "win32":
        try:
            if profile:
                safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(profile))
                match = "$_.CommandLine -match 'browser_profiles[\\\\/]+%s(?![A-Za-z0-9_])'" % safe
            else:
                match = "$_.CommandLine -like '*browser_profiles*'"
            cmd = "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | Where-Object { " + match + " } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass


JOB = Job()
# Parallel queue: lane 1 is JOB itself, so the console, stop button and every
# manual action keep working as before; lanes 2 and 3 only ever run queue
# items. How many run at once is the buyer's setting (1 by default).
LANES = [JOB, Job("lane2"), Job("lane3")]
LANE_ITEM = [None, None, None]
MAX_PARALLEL = len(LANES)


def _parallel_file():
    return os.path.join(_data_dir(), "parallel.json")


def get_parallel():
    try:
        with open(_parallel_file(), encoding="utf-8") as f:
            n = int(json.load(f).get("n", 1))
    except Exception:
        n = 1
    return max(1, min(n, MAX_PARALLEL, int(plan_limits().get("parallel") or 1)))


def set_parallel(n):
    n = max(1, min(int(n), MAX_PARALLEL, int(plan_limits().get("parallel") or 1)))
    with open(_parallel_file(), "w", encoding="utf-8") as f:
        json.dump({"n": n}, f)
    return n


def any_lane_running():
    return any(l.running for l in LANES)


# The app's working limits, fixed locally (the same values it has always
# run with): how many videos may wait in the queue, how many run at once, etc.
PRO_LIMITS = {"label": "Pro", "queue": 2, "parallel": 1, "categories": 1,
              "drafts_per_category": 3, "voice_browser": "limited", "clone_voice": True,
              "reference": "unlimited", "vox": True, "clips": False}


def plan_info(refresh=False):
    """{plan, limits, used} - the fixed local limits."""
    return {"plan": "pro", "limits": dict(PRO_LIMITS), "used": {}, "until": None}


def plan_limits():
    return plan_info()["limits"]


def queue_room():
    """How many more videos the plan lets into the queue right now."""
    pending = sum(1 for x in load_prod_queue() if x.get("status") in ("queued", "running"))
    return max(0, int(plan_limits().get("queue") or 2) - pending)

def _data_dir():
    """Where this copy may write its own files. Installed, the app folder is
    Program Files and cannot be written, so the queue file lives with the
    user's data; a developer checkout resolves to the same vidgen folder."""
    try:
        import userdata
        d = os.path.join(userdata.base(), "vidgen")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return HERE


PROD_QUEUE_FILE = os.path.join(_data_dir(), "production_queue.json")
QUEUE_LOCK = threading.Lock()
QUEUE_RUNNER_ENABLED = True
CURRENT_QUEUE_ITEM_ID = None

def load_prod_queue():
    with QUEUE_LOCK:
        if not os.path.isfile(PROD_QUEUE_FILE):
            return []
        try:
            with open(PROD_QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

def save_prod_queue(queue_data):
    with QUEUE_LOCK:
        try:
            with open(PROD_QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(queue_data, f, indent=2)
        except Exception as e:
            print("Error saving queue:", e)

def _prepare_queue_item(q, next_item):
    """argv for a queued item, or None (and the item marked failed)."""
    proj = sanitize_project_name(next_item.get("project") or "")
    if not proj:
        next_item["status"] = "failed"
        next_item["error"] = "Empty or invalid project name"
        save_prod_queue(q)
        return None

    script_path = next_item.get("script_path", "")
    script_text = next_item.get("script_text", "")

    if script_text and not script_path:
        os.makedirs(studio_api.SCRIPTS_DIR, exist_ok=True)
        script_path = os.path.join(studio_api.SCRIPTS_DIR, f"{proj}.txt")
        with open(script_path, "w", encoding="utf-8") as sf:
            sf.write(script_text)
        next_item["script_path"] = script_path

    # Check if project folder already has script.txt
    if not script_path or not os.path.isfile(script_path):
        p_script = os.path.join(PROJECTS, proj, "script.txt")
        if os.path.isfile(p_script):
            script_path = p_script
            next_item["script_path"] = script_path

    if not script_path or not os.path.isfile(script_path):
        next_item["status"] = "failed"
        next_item["error"] = f"Script not found: {script_path or 'No script'}"
        save_prod_queue(q)
        return None

    # Ensure project directory and script.txt exist
    p_dir = os.path.join(PROJECTS, proj)
    os.makedirs(p_dir, exist_ok=True)
    # Clean previous run final.mp4 so progress starts fresh
    old_final = os.path.join(p_dir, "final.mp4")
    if os.path.isfile(old_final):
        try:
            os.remove(old_final)
        except Exception:
            pass

    if os.path.abspath(script_path) != os.path.abspath(os.path.join(p_dir, "script.txt")):
        with open(script_path, "r", encoding="utf-8") as sf:
            with open(os.path.join(p_dir, "script.txt"), "w", encoding="utf-8") as df:
                df.write(sf.read())


    profile = next_item.get("profile", "acc1")
    ref = next_item.get("ref", "")
    voice = next_item.get("voice", "meri eng")
    aspect_val = next_item.get("aspect", "16:9")

    clips_val = next_item.get("clips", 0)
    try:
        clips_cnt = int(clips_val) if plan_limits().get("clips") else 0
    except Exception:
        clips_cnt = 0
    placement_val = next_item.get("placement", "front") or "front"

    argv = [sys.executable, env.script("make_video.py"), script_path,
            "--profile", profile, "--name", proj,
            "--clips", str(clips_cnt),
            "--placement", placement_val,
            "--aspect", aspect_val]
    if ref:
        argv.extend(["--ref", ref])
    if voice:
        argv.extend(["--voice", voice])
    look_q = next_item.get("look") or ""
    if look_q:
        argv.extend(["--look", look_q])
    if next_item.get("target_seconds"):
        try:
            with open(script_path, encoding="utf-8", errors="ignore") as sf:
                text = sf.read()
        except OSError:
            text = next_item.get("script_text") or ""
        speed, target = pace_for(text, voice, next_item.get("target_seconds"))
        if target:
            argv.extend(["--speed", str(speed), "--target", str(target)])


    return proj, argv


def _finish_lane_item(item, lane):
    if lane.code == 0 and not getattr(lane, "user_stopped", False):
        item["status"] = "completed"
        item["error"] = None
    elif getattr(lane, "user_stopped", False) or lane.code == -9:
        item["status"] = "failed"
        item["error"] = "Stopped by user"
    else:
        item["status"] = "failed"
        item["error"] = f"Failed with exit code {lane.code}"
    item["finished_at"] = time.time()


def queue_worker_loop():
    global CURRENT_QUEUE_ITEM_ID, QUEUE_RUNNER_ENABLED
    while True:
        try:
            time.sleep(3)
            q = load_prod_queue()
            changed = False

            # Films that finished (or were stopped) in any lane.
            for i, lane in enumerate(LANES):
                item_id = LANE_ITEM[i]
                if not item_id or lane.running:
                    continue
                item = next((x for x in q if x["id"] == item_id), None)
                if item and item.get("status") == "running":
                    _finish_lane_item(item, lane)
                    if item["error"] == "Stopped by user" and i == 0:
                        QUEUE_RUNNER_ENABLED = False
                    changed = True
                LANE_ITEM[i] = None

            # A "running" item no lane is running is left over from a restart.
            active = {x for x in LANE_ITEM if x}
            for x in q:
                if x.get("status") == "running" and x.get("id") not in active:
                    x["status"] = "queued"
                    x["started_at"] = None
                    x["error"] = None
                    changed = True
            if changed:
                save_prod_queue(q)
            CURRENT_QUEUE_ITEM_ID = next((x for x in LANE_ITEM if x), None)

            if not QUEUE_RUNNER_ENABLED:
                continue

            n = get_parallel()
            for i in range(n):
                lane = LANES[i]
                if lane.running or LANE_ITEM[i]:
                    continue
                # A Chrome profile opens in one window only: two films on one
                # account would fight over it, so an item whose account is busy
                # waits for that lane.
                busy = {l.profile for l in LANES if l.running and l.profile}
                next_item = next((x for x in q if x.get("status") == "queued"
                                  and (x.get("profile") or "acc1") not in busy), None)
                if not next_item:
                    break
                prepared = _prepare_queue_item(q, next_item)
                if not prepared:
                    continue
                proj, argv = prepared
                ok, note = lane.start("production", argv, cwd=ROOT_DIR)
                if ok:
                    LANE_ITEM[i] = next_item["id"]
                    next_item["status"] = "running"
                    next_item["started_at"] = time.time()
                    next_item["error"] = None
                    next_item["lane"] = i + 1
                    if i == 0:
                        Handler.project = proj
                else:
                    next_item["status"] = "failed"
                    next_item["error"] = f"Could not start: {note}"
                save_prod_queue(q)
            CURRENT_QUEUE_ITEM_ID = next((x for x in LANE_ITEM if x), None)
        except Exception as e:
            print("Queue worker error:", e)


def sanitize_project_name(name):
    if not name:
        return ""
    # Remove Windows forbidden characters: \ / : * ? " < > |
    cleaned = re.sub(r'[\\/:*?"<>|]', '', name).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned


def auto_project_name(script_text):
    """A readable project name off the pasted script, so a launch with no
    name typed still leaves something identifiable in the project list
    instead of a bare timestamp."""
    first_line = next((ln.strip() for ln in script_text.splitlines() if ln.strip()), "")
    base = sanitize_project_name(first_line).strip()
    base = re.sub(r"^(?:#+|\d+[.)]|title\s*:)\s*", "", base, flags=re.I).strip(' -:"\'')
    if len(base) > 48:
        cut = base[:48]
        if " " in cut:
            cut = cut[:cut.rindex(" ")]
        base = cut.rstrip(" ,;:-")
    if not base:
        base = "Script"
    name = base
    n = 2
    while os.path.isdir(os.path.join(PROJECTS, name)):
        name = f"{base} ({n})"
        n += 1
    return name


STEP_PATTERNS = [
    (1, "Step 1: Voiceover Generation", re.compile(r"STEP 1: GENERATING VOICEOVER", re.I)),
    (2, "Step 2: Timestamps Alignment", re.compile(r"STEP 2: ALIGNING TIMESTAMPS", re.I)),
    (3, "Step 3: Action Prompt Generation", re.compile(r"STEP 3: CREATING ACTION-FIRST PROMPTS", re.I)),
    (4, "Step 4: Gemini Canvas Generation", re.compile(r"STEP 4: GENERATING IMAGES|\[bulk\]|generating \d+ images", re.I)),
    (5, "Step 5: Negation Red Cross Stamps", re.compile(r"STEP 5: APPLYING AUTOMATIC NEGATION", re.I)),
    (6, "Step 6: Zero-Blank Image Verification", re.compile(r"STEP 6: VERIFYING ALL IMAGES|STEP 5: Verifying Image Integrity", re.I)),
    (7, "Step 7: 1080p Master Video Render", re.compile(r"STEP 7: RENDERING 1080p|STEP 6: Rendering Final 1080p|\[render\]|\d+/\d+\s+shots", re.I)),
    (8, "Production Complete", re.compile(r"PRODUCTION COMPLETE", re.I)),
]

SKIP_PATTERNS = [
    (2, re.compile(r"\[SKIP STEP 1\]|Voiceover .* already exists", re.I)),
    (3, re.compile(r"\[SKIP STEP 2\]|Timestamps .* already exist", re.I)),
    (4, re.compile(r"\[SKIP STEP 3\]|Prompts .* already exist", re.I)),
    (5, re.compile(r"\[SKIP STEP 4\]|All .* images already rendered", re.I)),
]

ROUND_RE = re.compile(r"round (\d+)\s*/\s*(\d+)", re.I)
GRID_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+(?:rendered|tiles)", re.I)
SHOT_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+shots", re.I)


def calculate_full_progress(lines, is_running=True, stage=None, pid=None):
    lines_list = list(lines) if lines else []
    
    # Check disk state if project folder exists
    total_rows = 0
    img_count = 0
    zip_count = 0
    has_final = False
    
    check_pid = pid or Handler.project
    if check_pid:
        pdir = folder(check_pid)
        if os.path.isdir(pdir):
            seg_file = os.path.join(pdir, "segments.json")
            if os.path.isfile(seg_file):
                try:
                    with open(seg_file, "r", encoding="utf-8") as sf:
                        total_rows = len(json.load(sf))
                except Exception:
                    pass
            img_dir = os.path.join(pdir, "images")
            if os.path.isdir(img_dir):
                img_count = len([f for f in os.listdir(img_dir) if f.lower().endswith(('.png', '.jpg', '.webp')) and os.path.getsize(os.path.join(img_dir, f)) > 20000])
            down_dir = os.path.join(pdir, "downloads")
            if os.path.isdir(down_dir):
                zip_count = len([f for f in os.listdir(down_dir) if f.endswith('.zip')])
            has_final = os.path.isfile(os.path.join(pdir, "final.mp4"))

    if not lines_list and not is_running:
        if has_final:
            return {"step": 7, "title": "🎉 Production Complete!", "detail": "Deliverables published to D:\\Video Projects", "pct": 100, "running": False}
        return {"step": 0, "title": "Ready / Idle", "detail": "Waiting to start pipeline", "pct": 0, "running": False}

    step_num = 4 if stage == "images" else 1
    step_title = "Step 4: Gemini Canvas Generation" if stage == "images" else "Step 1: Voiceover Generation"
    detail = ""

    # Detect highest step reached from stdout
    for line in lines_list:
        for num, title, pat in STEP_PATTERNS:
            if pat.search(line):
                if num > step_num:
                    step_num = num
                    step_title = title
        for num, pat in SKIP_PATTERNS:
            if pat.search(line):
                if num > step_num:
                    step_num = num
                    if step_num == 2:
                        step_title = "Step 2: Timestamps Alignment"
                    elif step_num == 3:
                        step_title = "Step 3: Action Prompt Generation"
                    elif step_num == 4:
                        step_title = "Step 4: Gemini Canvas Generation"
                    elif step_num == 5:
                        step_title = "Step 5: Negation Red Cross Stamps"

    # Sub progress parsing
    rounds = None
    grid_done, grid_tot = None, None
    shots_done, shots_tot = None, None

    for line in reversed(lines_list[-120:]):
        if rounds is None:
            m = ROUND_RE.search(line)
            if m:
                rounds = (int(m.group(1)), int(m.group(2)))
        if grid_done is None:
            m = GRID_RE.search(line)
            if m:
                grid_done, grid_tot = int(m.group(1)), int(m.group(2))
        if shots_done is None:
            m = SHOT_RE.search(line)
            if m:
                shots_done, shots_tot = int(m.group(1)), int(m.group(2))
                if step_num < 7:
                    step_num = 7
                    step_title = "Step 7: 1080p Master Video Render"

    if step_num == 8 or (has_final and not is_running) or any("PRODUCTION COMPLETE" in l for l in lines_list[-15:]):
        return {"step": 7, "title": "🎉 Production Complete!", "detail": "Deliverables published to D:\\Video Projects", "pct": 100, "running": False}


    pct = 5
    if step_num == 1:
        pct = 12
        detail = "Generating Minimax voiceover narration..."
    elif step_num == 2:
        pct = 18
        detail = "Aligning Whisper timestamps and word cuts..."
    elif step_num == 3:
        pct = 24
        detail = "Building iconic action-first visual prompts..."
    elif step_num == 4:
        # Step 4 spans 25% to 75%
        frac = 0.0
        round_info = ""
        if rounds and rounds[1] > 0:
            i, n = rounds
            sub_val = (grid_done / grid_tot) if (grid_done and grid_tot and grid_tot > 0) else 0.0
            sub_val = min(1.0, max(0.0, sub_val))
            round_frac = ((i - 1) + sub_val) / n
            frac = max(frac, min(1.0, round_frac))
            tiles_str = f" ({grid_done}/{grid_tot} tiles)" if (grid_done is not None and grid_tot) else ""
            round_info = f"Round {i}/{n}{tiles_str}"
        
        if total_rows > 0:
            tot_zips = max(1, math.ceil(total_rows / 35))
            if zip_count > 0:
                frac = max(frac, min(0.98, zip_count / tot_zips))
            if img_count > 0:
                frac = max(frac, min(1.0, img_count / total_rows))

        pct = 25 + round(50 * frac)
        saved_str = f" · {img_count}/{total_rows} images" if (total_rows > 0 and img_count > 0) else (f" · {zip_count} batches" if zip_count > 0 else "")
        detail = f"Gemini Canvas: {round_info or 'Rendering tiles live...'}{saved_str} ({pct}%)"
    elif step_num == 5:
        pct = 76
        detail = "Applying automatic negation red crosses (❌)..."
    elif step_num == 6:
        pct = 79
        detail = "Performing mandatory zero-blank image verification..."
    elif step_num == 7:
        # Step 7 spans 80% to 98%
        shot_frac = 0.0
        if shots_done and shots_tot and shots_tot > 0:
            shot_frac = min(1.0, shots_done / shots_tot)
        elif total_rows > 0 and shots_done:
            shot_frac = min(1.0, shots_done / total_rows)
        pct = 80 + round(18 * shot_frac)
        shot_str = f"{shots_done}/{shots_tot}" if (shots_done and shots_tot) else f"{pct}%"
        detail = f"Rendering 1080p Master Video: {shot_str} shots ({pct}%)"

    if not is_running and pct < 100:
        detail += " (Halted/Exit)"

    return {
        "step": min(7, step_num),
        "title": step_title,
        "detail": detail,
        "pct": min(100, max(0, pct)),
        "running": is_running,
        "round": list(rounds) if rounds else None
    }


def progress_from(lines, pid=None):
    return calculate_full_progress(lines, JOB.running, pid=pid)


def with_progress(job, pid=None):
    if not job:
        job = {}
    job["progress"] = calculate_full_progress(job.get("lines") or [], job.get("running", False), stage=job.get("stage"), pid=pid)
    return job


def calculate_disk_progress(pid):
    """Calculates true stage completion from project files on disk."""
    if not pid:
        return {"step": 0, "title": "Ready / Idle", "detail": "Waiting to start pipeline", "pct": 0, "running": False}
    pdir = folder(pid)
    if not os.path.isdir(pdir):
        return {"step": 0, "title": "Ready / Idle", "detail": "Waiting to start pipeline", "pct": 0, "running": False}
        
    has_voice = os.path.isfile(os.path.join(pdir, "voiceover.mp3"))
    has_rows = os.path.isfile(os.path.join(pdir, "segments.json"))
    has_prompts = os.path.isfile(os.path.join(pdir, "prompts.txt"))
    img_dir = os.path.join(pdir, "images")
    img_count = len([f for f in os.listdir(img_dir) if f.lower().endswith(('.png', '.jpg'))]) if os.path.isdir(img_dir) else 0
    down_dir = os.path.join(pdir, "downloads")
    zip_count = len([f for f in os.listdir(down_dir) if f.endswith('.zip')]) if os.path.isdir(down_dir) else 0
    has_final = os.path.isfile(os.path.join(pdir, "final.mp4"))
    
    total_rows = 0
    if has_rows:
        try:
            with open(os.path.join(pdir, "segments.json"), "r", encoding="utf-8") as f:
                total_rows = len(json.load(f))
        except Exception:
            pass

    if has_final:
        return {"step": 7, "title": "🎉 Production Complete!", "detail": f"1080p Master Video Ready ({img_count} shots)", "pct": 100, "running": False}
        
    if img_count > 0 and total_rows > 0:
        frac = min(1.0, img_count / total_rows)
        pct = 25 + round(50 * frac)
        return {"step": 4, "title": "Step 4: Gemini Canvas Generation", "detail": f"Generated {img_count}/{total_rows} images ({zip_count} batches downloaded)", "pct": pct, "running": False}

    if has_prompts:
        return {"step": 3, "title": "Step 3: Action Prompts Ready", "detail": f"Voiceover & {total_rows} Prompts generated — Ready for Canvas", "pct": 25, "running": False}

    if has_rows:
        return {"step": 2, "title": "Step 2: Timestamps Aligned", "detail": f"Voiceover cut into {total_rows} rows", "pct": 18, "running": False}

    if has_voice:
        try:
            dur = probe(os.path.join(pdir, "voiceover.mp3"))
            dur_str = f"{int(dur//60)}:{int(dur%60):02d} min" if dur else ""
        except Exception:
            dur_str = ""
        return {"step": 1, "title": "Step 1: Voiceover Ready", "detail": f"Voiceover generated ({dur_str})", "pct": 12, "running": False}

    return {"step": 0, "title": "Ready / Idle", "detail": "Waiting to start pipeline", "pct": 0, "running": False}


# ── reading the project ──────────────────────────────────────────────────────

def folder(pid):
    return os.path.join(PROJECTS, pid)


def sub(pid, *names):
    """Path inside a project; accepts nested components for shot files."""
    return os.path.join(folder(pid), *names)


def read_json(path, fallback=None):
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return fallback


def count(path):
    return len(os.listdir(path)) if os.path.isdir(path) else 0


def projects():
    if not os.path.isdir(PROJECTS):
        return []
    # Filter out throwaway scratch folders starting with _ or .
    projs = [n for n in os.listdir(PROJECTS)
             if os.path.isdir(os.path.join(PROJECTS, n))
             and not n.startswith(("_", "."))]
    return sorted(projs)


def profiles():
    data = read_json(PROFILES_FILE, [])
    return [p["name"] for p in data if isinstance(p, dict) and "name" in p]


def settings(pid):
    s = {**DEFAULTS, **(read_json(sub(pid, "dash.json"), {}) or {})}
    s_file = read_json(sub(pid, "settings.json"), {}) or {}
    l_file = read_json(sub(pid, "look.json"), {}) or {}
    if s_file.get("aspect"):
        s["aspect"] = s_file["aspect"]
    elif l_file.get("aspect"):
        s["aspect"] = l_file["aspect"]
    return s


def save_settings(pid, patch):
    now = settings(pid)
    now.update({k: v for k, v in patch.items() if k in DEFAULTS})
    if "manual" in patch:                       # a list, not a scalar setting
        now["manual"] = [int(i) for i in (patch["manual"] or [])]
    os.makedirs(folder(pid), exist_ok=True)
    with open(sub(pid, "dash.json"), "w", encoding="utf-8") as f:
        json.dump(now, f, indent=1)
    if "aspect" in patch:
        s_file = sub(pid, "settings.json")
        cur_s = read_json(s_file, {}) or {}
        cur_s["aspect"] = patch["aspect"]
        with open(s_file, "w", encoding="utf-8") as f:
            json.dump(cur_s, f, indent=1)
        l_file = sub(pid, "look.json")
        cur_l = read_json(l_file, {}) or {}
        cur_l["aspect"] = patch["aspect"]
        with open(l_file, "w", encoding="utf-8") as f:
            json.dump(cur_l, f, indent=1)
    return now


BATCH_SCRIPTS = [
    "01_FIRST_ANTIDOTE", "02_FIRST_TOBACCO", "03_FIRST_ALCOHOL", "04_FIRST_SILVER",
    "05_FIRST_DRUG", "06_FIRST_GOLD", "07_FIRST_ANTIBIOTIC", "08_FIRST_ANESTHETIC",
    "09_FIRST_TORTURE_DEVICE", "10_FIRST_SCAM", "11_WHAT_IF_HUMANS_NEVER_INVENTED_CLOTHES"
]


def get_batch_status():
    items = []
    total_batch_pct = 0
    for pid in BATCH_SCRIPTS:
        pdir = os.path.join(PROJECTS, pid)
        state = read_json(os.path.join(pdir, "state.json"), {}) or {}
        
        voice_id = state.get("voice_id", "")
        voice_name = state.get("voice_name", "")
        voice_ok = (voice_name == "meri eng" or voice_id == "clone_2643599")
        has_vo = os.path.isfile(os.path.join(pdir, "voiceover.mp3"))
        voice_pct = 100 if (has_vo and voice_ok) else (50 if has_vo else 0)
        
        img_dir = os.path.join(pdir, "images")
        img_count = len(os.listdir(img_dir)) if os.path.isdir(img_dir) else 0
        down_dir = os.path.join(pdir, "downloads")
        zip_count = len(os.listdir(down_dir)) if os.path.isdir(down_dir) else 0
        
        if img_count >= 500:
            img_pct = 100
        elif zip_count > 0:
            img_pct = min(95, round(zip_count / 12.0 * 100))
        else:
            img_pct = 0
            
        final_mp4 = os.path.isfile(os.path.join(pdir, "final.mp4"))
        pub_path = state.get("published", "")
        pub_exists = os.path.isdir(pub_path) and any(x.endswith(".mp4") for x in os.listdir(pub_path)) if pub_path else False
        render_pct = 100 if (final_mp4 and voice_ok) else (50 if final_mp4 else 0)
        
        film_pct = round(voice_pct * 0.30 + img_pct * 0.40 + render_pct * 0.30)
        total_batch_pct += film_pct
        
        if film_pct == 100:
            status = "100% Ready (Published)"
            badge = "done"
        elif render_pct > 0:
            status = "Rendering 1080p Video"
            badge = "rendering"
        elif img_pct == 100:
            status = "Images Ready, Queued for Render"
            badge = "images_done"
        elif img_pct > 0:
            status = f"Generating Images ({zip_count}/12 Parts)"
            badge = "generating"
        elif voice_pct > 0:
            status = "Voice Ready, Waiting for Images"
            badge = "voice_done"
        else:
            status = "Queued in Batch"
            badge = "queued"
            
        items.append({
            "pid": pid,
            "title": pid.replace("_", " ").title(),
            "voice_ok": voice_ok,
            "voice_pct": voice_pct,
            "voice_name": voice_name or ("meri eng" if voice_ok else "pending"),
            "img_count": img_count,
            "img_pct": img_pct,
            "render_pct": render_pct,
            "film_pct": film_pct,
            "status": status,
            "badge": badge,
            "published": pub_path if pub_exists else None
        })
        
    avg_batch_pct = round(total_batch_pct / len(BATCH_SCRIPTS))
    return {
        "overall_pct": avg_batch_pct,
        "items": items,
        "timestamp": time.strftime("%H:%M:%S")
    }


_VOICES = {"at": 0.0, "names": []}


def voices_available():
    """All 100+ voices available on AI33 Pro, cached for ten minutes.
    Categorized into Hindi Narrators, Cloned Voices, ElevenLabs, and Minimax.
    """
    if _VOICES["names"] and time.time() - _VOICES["at"] < 600:
        return _VOICES["names"]

    if AI_PIPE not in sys.path:
        sys.path.insert(0, AI_PIPE)
    bulk_dir = os.path.join(ROOT_DIR, "bulkgen")
    if bulk_dir not in sys.path:
        sys.path.insert(0, bulk_dir)

    import requests
    api_base = "https://api.ai33.pro"
    out = []

    # 1. Hindi Narrators — specifically tuned for Hindi storytelling
    try:
        from make_voiceover import AI33_NARRATORS
        for name, (vid, desc) in AI33_NARRATORS.items():
            if "hindi" in desc.lower():
                out.append({"name": f"{name.title()} — {desc}", "id": vid,
                            "group": "🇮🇳 Hindi Narrators", "lang": "hindi"})
            else:
                out.append({"name": f"{name.title()} — {desc}", "id": vid,
                            "group": "🇬🇧 English Narrators", "lang": "english"})
    except Exception:
        pass

    # 2. Dynamic Voices from AI33 Pro (Clones, ElevenLabs, Minimax)
    key = ""
    try:
        from common import ai33_key
        key = ai33_key()
    except Exception:
        pass

    if key:
        headers = {"xi-api-key": key}
        # A) User's own cloned voices
        try:
            r = requests.get(f"{api_base}/v3/voices?provider=clone", headers=headers, timeout=8)
            if r.status_code == 200:
                for v in r.json().get("data", []):
                    v_n = v.get("name")
                    v_id = v.get("voice_id")
                    if v_n and v_id:
                        out.append({"name": f"🎙️ {v_n}", "id": v_id,
                                    "group": "🎙️ Your Cloned Voices", "lang": "clone",
                                    "preview": v.get("preview_url") or ""})
        except Exception:
            pass

        # B) ElevenLabs Library (50+ professional voices)
        try:
            r = requests.get(f"{api_base}/v3/voices?provider=elevenlabs", headers=headers, timeout=8)
            if r.status_code == 200:
                for v in r.json().get("data", []):
                    v_name = v.get("name") or "Unnamed"
                    v_id = v.get("voice_id")
                    gender = (v.get("gender") or "").capitalize()
                    accent = (v.get("accent") or "").capitalize()
                    sub = f"{gender}, {accent}" if gender and accent else (gender or accent)
                    label = f"{v_name} ({sub})" if sub else v_name
                    if v_id:
                        out.append({"name": label, "id": v_id,
                                    "group": "🌍 ElevenLabs Voices", "lang": "elevenlabs",
                                    "preview": v.get("preview_url") or ""})
        except Exception:
            pass

        # C) Minimax Library (30 expressive character voices)
        try:
            r = requests.get(f"{api_base}/v3/voices?provider=minimax", headers=headers, timeout=8)
            if r.status_code == 200:
                for v in r.json().get("data", []):
                    v_name = v.get("name") or "Unnamed"
                    v_id = v.get("voice_id")
                    gender = (v.get("gender") or "").capitalize()
                    use = ", ".join(v.get("use_cases") or [])
                    sub = f"{gender} • {use}" if gender and use else (gender or use)
                    label = f"{v_name} ({sub})" if sub else v_name
                    if v_id:
                        out.append({"name": label, "id": v_id,
                                    "group": "🎭 Minimax Voices", "lang": "minimax",
                                    "preview": v.get("preview_url") or ""})
        except Exception:
            pass

        # A2) Voices cloned in this app - always listed, even when AI33's own
        #     clone listing does not show them on its first page.
        try:
            import voiceclone
            have = {v["id"] for v in out}
            mine = [{"name": f"🎙️ {v['name']}", "id": v["id"], "group": "🎙️ Your Cloned Voices",
                     "lang": "clone", "preview": ""}
                    for v in voiceclone.my_clones() if v["id"] not in have]
            out = mine + out
        except Exception:
            pass

        # D) The buyer's starred voices, first in the list.
        try:
            favs = [{"name": "⭐ " + r["name"], "id": r["id"], "group": "⭐ Favourites",
                     "lang": "favorite", "preview": r["preview"]}
                    for r in _favorite_ids().values()
                    if not voice_locked(r["provider"], r["id"])]
            out = favs + out
        except Exception:
            pass

    if out:
        _VOICES.update(at=time.time(), names=out)
    return out


# ------------------------------------------------------- voice browser
# The dropdown holds the first page of each AI33 library; ElevenLabs alone has
# 18,000+. The browser pages through all of them, plays each sample, and keeps
# the buyer's favourites on their AI33 account, so a star set on ai33.pro
# shows here and one set here shows there. The key never reaches the page.
AI33_API = "https://api.ai33.pro"
VOICE_PAGES_LIMITED = 2   # 60 voices per library on a limited plan
VOICE_PROVIDERS = ("clone", "elevenlabs", "minimax")
PREVIEW_HOSTS = ("ai33.pro", "hailuoai.video", "elevenlabs.io", "googleapis.com",
                 "minimax.io", "minimaxi.com", "cloudfront.net")


def _ai33_key():
    bulk_dir = os.path.join(ROOT_DIR, "bulkgen")
    for d in (AI_PIPE, bulk_dir):
        if d not in sys.path:
            sys.path.insert(0, d)
    try:
        from common import ai33_key
        return ai33_key() or ""
    except Exception:
        return ""


def _ai33(method, path, **kw):
    import requests
    key = _ai33_key()
    if not key:
        raise RuntimeError("add your AI33 key first (API Keys, top right)")
    r = requests.request(method, AI33_API + path, headers={"xi-api-key": key},
                         timeout=kw.pop("timeout", 15), **kw)
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code >= 400 or body.get("success") is False:
        raise RuntimeError(body.get("message") or "AI33 answered %d" % r.status_code)
    return body


def _voice_row(v, provider):
    sub = []
    for k in ("gender", "accent", "language"):
        val = str(v.get(k) or "").strip()
        if val and val.lower() not in [s.lower() for s in sub]:
            sub.append(val.capitalize() if k == "gender" else val)
    uses = [u for u in (v.get("use_cases") or []) if u not in ("Clone", "Bridge", "Remove noise")]
    if uses:
        sub.append(", ".join(uses[:2]).replace("_", " "))
    preview = v.get("preview_url") or ""
    if not preview:
        for lang in v.get("languages") or []:
            if lang.get("preview_url"):
                preview = lang["preview_url"]
                break
    return {"id": v.get("voice_id"), "name": v.get("name") or v.get("voice_id"),
            "sub": " · ".join(sub), "preview": preview, "provider": provider}


_FAVS = {"at": 0.0, "ids": {}}


def _favorite_ids():
    """The buyer's starred voices, all three libraries asked at once and kept
    for two minutes: every page of the browser marks its stars from this."""
    if time.time() - _FAVS["at"] < 120:
        return dict(_FAVS["ids"])

    def one(prov):
        try:
            return prov, _ai33("GET", "/v3/favorites", params={"provider": prov}).get("favorites") or []
        except Exception:
            return prov, []
    from concurrent.futures import ThreadPoolExecutor
    ids = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for prov, favs in pool.map(one, VOICE_PROVIDERS):
            for f in favs:
                data = f.get("voice_data") or {}
                vid = f.get("voice_id") or data.get("voice_id")
                if vid:
                    ids[vid] = _voice_row({**data, "voice_id": vid}, prov)
    _FAVS.update(at=time.time(), ids=ids)
    return dict(ids)


_SAMPLES = {}


def voice_sample_url(voice_id):
    """The sample of any voice by its id: the built-in narrators carry none in
    the dropdown, and AI33 finds a voice when searched by its own id."""
    if voice_id in _SAMPLES:
        return _SAMPLES[voice_id]
    for v in _VOICES.get("names") or []:
        if v.get("id") == voice_id and v.get("preview"):
            _SAMPLES[voice_id] = v["preview"]
            return v["preview"]
    prov, _, raw = str(voice_id).partition("_")
    url = ""
    if prov in VOICE_PROVIDERS and raw:
        body = _ai33("GET", "/v3/voices", params={"provider": prov, "search": raw, "page_size": 5})
        for v in body.get("data") or []:
            if v.get("voice_id") == voice_id:
                url = _voice_row(v, prov)["preview"]
                break
    _SAMPLES[voice_id] = url
    return url


_OPEN_VOICES = {}


def open_voice_ids(provider):
    """The voices a limited plan may use: the first pages of each library as
    AI33 lists them, unsearched. Kept for an hour."""
    got = _OPEN_VOICES.get(provider)
    if got and time.time() - got[0] < 3600:
        return got[1]
    ids = set()
    for pg in range(1, VOICE_PAGES_LIMITED + 1):
        try:
            body = _ai33("GET", "/v3/voices", params={"provider": provider, "page": pg, "page_size": 30})
        except Exception:
            break
        ids.update(v.get("voice_id") for v in body.get("data") or [] if v.get("voice_id"))
        if not (body.get("pagination") or {}).get("has_more"):
            break
    if ids:
        _OPEN_VOICES[provider] = (time.time(), ids)
    return ids


def voice_locked(provider, voice_id):
    """True when the plan shows this voice but does not let it be used."""
    if plan_limits().get("voice_browser") == "full" or provider == "clone":
        return False
    if provider not in VOICE_PROVIDERS:
        return False
    return voice_id not in open_voice_ids(provider)


def voices_browse(provider, search="", page=1, language="", gender=""):
    fav = _favorite_ids()
    limited = plan_limits().get("voice_browser") != "full"
    if provider == "favorites":
        rows = list(fav.values())
        if search:
            s = search.lower()
            rows = [r for r in rows if s in (r["name"] + " " + r["sub"]).lower()]
        for r in rows:
            r["fav"] = True
            r["locked"] = voice_locked(r["provider"], r["id"])
        return {"voices": rows, "has_more": False, "total": len(rows), "limited": limited}
    if provider not in VOICE_PROVIDERS:
        raise RuntimeError("unknown voice library")
    # Every plan sees the whole library, searches it and plays the samples.
    # A limited plan uses the first voices of each library; the rest show a
    # lock and a Pro+ label, so the buyer knows what the upgrade opens.
    locked = limited and provider != "clone"
    params = {"provider": provider, "page": max(int(page or 1), 1), "page_size": 30}
    if search:
        params["search"] = search
    if language and provider != "clone":
        params["language"] = language
    if gender and provider != "clone":
        params["gender"] = gender
    body = _ai33("GET", "/v3/voices", params=params)
    rows = [_voice_row(v, provider) for v in body.get("data") or [] if v.get("voice_id")]
    open_ids = open_voice_ids(provider) if locked else set()
    for r in rows:
        r["fav"] = r["id"] in fav
        r["locked"] = bool(locked and r["id"] not in open_ids)
    pg = body.get("pagination") or {}
    return {"voices": rows, "has_more": bool(pg.get("has_more")),
            "total": pg.get("total", len(rows)), "limited": locked,
            "open": len(open_ids) if locked else None}


def voice_favorite(provider, voice_id, voice, on):
    if provider not in VOICE_PROVIDERS or not voice_id:
        raise RuntimeError("which voice?")
    if on:
        data = {"voice_id": voice_id, "name": voice.get("name") or voice_id,
                "preview_url": voice.get("preview") or ""}
        _ai33("POST", "/v3/favorites", json={"provider": provider, "voice_id": voice_id,
                                              "voice_data": data})
    else:
        import urllib.parse as _up
        _ai33("DELETE", "/v3/favorites/%s/%s" % (provider, _up.quote(voice_id, safe="")))
    _VOICES["at"] = 0.0  # the dropdown's favourites group is stale now
    _FAVS["at"] = 0.0
    return True


# How long a script will be once spoken. Each voice has its own pace, so it is
# learned from this PC's own finished films in that voice; with none yet, a
# narrator's usual 140 words a minute. Measured on the owner's films: one voice
# ran 120-146 words a minute, so the estimate is a guide and the length the
# buyer picks is what the voice stage then fits exactly.
DEFAULT_WPM = 140.0
PACE_MIN, PACE_MAX = 0.8, 1.2        # the TTS speed the voice is asked for
LENGTH_MIN, LENGTH_MAX = 0.75, 1.3   # chosen length vs the natural one (1.2 x 1.15 reach)


def voice_pace(voice):
    """(words per minute at speed 1.0, how many films it was learned from)."""
    rates = []
    try:
        names = os.listdir(PROJECTS)
    except OSError:
        names = []
    for name in names:
        st = read_json(os.path.join(PROJECTS, name, "state.json"), {}) or {}
        if voice not in (st.get("voice_id"), st.get("voice_name")):
            continue
        wpm = st.get("natural_wpm")
        if not wpm:
            # Films from before speed control: spoken at 1.0, never fitted.
            if st.get("target") or float(st.get("speed") or 1.0) != 1.0:
                continue
            d = float(st.get("duration") or 0)
            words = st.get("words")
            if not words:
                try:
                    with open(os.path.join(PROJECTS, name, "script.txt"), encoding="utf-8",
                              errors="ignore") as f:
                        words = len(f.read().split())
                except OSError:
                    continue
            wpm = words / d * 60 if d > 0 else 0
        if 60 <= float(wpm) <= 260:
            rates.append((os.path.getmtime(os.path.join(PROJECTS, name, "state.json")), float(wpm)))
    rates = [r for _, r in sorted(rates)[-10:]]
    if not rates:
        return DEFAULT_WPM, 0
    return sum(rates) / len(rates), len(rates)


def estimate_length(text, voice):
    words = len((text or "").split())
    wpm, films = voice_pace(voice)
    seconds = words / wpm * 60 if wpm else 0
    return {"ok": True, "words": words, "seconds": round(seconds, 1), "wpm": round(wpm),
            "learned_from": films,
            "min_seconds": round(seconds * LENGTH_MIN, 1),
            "max_seconds": round(seconds * LENGTH_MAX, 1)}


def pace_for(text, voice, target):
    """(speed, target) to hand the voice stage for a chosen length, or (1.0, None)."""
    try:
        target = float(target or 0)
    except (TypeError, ValueError):
        target = 0
    if target <= 0:
        return 1.0, None
    est = estimate_length(text, voice)["seconds"]
    if est <= 0:
        return 1.0, None
    target = max(est * LENGTH_MIN, min(est * LENGTH_MAX, target))
    speed = max(PACE_MIN, min(PACE_MAX, est / target))
    return round(speed, 3), round(target, 1)


def looks_available():
    """The style presets on disk, each saying whether it wants a reference."""
    out = []
    for name in sorted(os.listdir(LOOKS)) if os.path.isdir(LOOKS) else []:
        if not name.endswith(".json"):
            continue
        data = read_json(os.path.join(LOOKS, name), {}) or {}
        out.append({"id": name[:-5], "label": data.get("label", name[:-5]),
                    "needs_ref": bool(data.get("needs_ref", True)),
                    "custom": bool(data.get("custom")),
                    "motion": data.get("motion", "kenburns")})
    return out



def apply_custom_style(pid, text):
    """Make the buyer's own prompt the look for this film.

    The reference image goes with it deliberately: a reference pins a
    character, and a custom style is usually a whole different way of drawing
    in which that character does not exist.
    """
    text = " ".join((text or "").split())
    look = read_json(sub(pid, "look.json"), {}) or {}
    look["style"] = text
    look["character"] = ""
    look["character_ref"] = ""
    look["style_ref"] = ""
    look["custom"] = True
    with open(sub(pid, "look.json"), "w", encoding="utf-8") as f:
        json.dump(look, f, indent=2)
    save_settings(pid, {"look_mode": "custom"})
    return look


def clear_custom_style(pid, mode="2d"):
    """Back to the presets."""
    look = read_json(sub(pid, "look.json"), {}) or {}
    look.pop("custom", None)
    with open(sub(pid, "look.json"), "w", encoding="utf-8") as f:
        json.dump(look, f, indent=2)
    return apply_look(pid, mode)


def custom_style_of(pid):
    """(on, text) for this film."""
    look = read_json(sub(pid, "look.json"), {}) or {}
    return bool(look.get("custom")), (look.get("style") or "" if look.get("custom") else "")


def apply_look(pid, mode):
    """Swap the project onto a style preset, keeping what is still true."""
    look_now = read_json(sub(pid, "look.json"), {}) or {}
    if look_now.get("custom"):
        # Someone wrote their own style. Nothing should overwrite it by a side
        # effect - picking a reference, say. Turning it off is an explicit act.
        return look_now
    preset = read_json(os.path.join(LOOKS, f"{mode}.json"))
    if not preset:
        raise ValueError(f"no look preset called {mode!r}")
    look = read_json(sub(pid, "look.json"), {}) or {}
    keep_ref = look.get("character_ref", "")
    look.update({k: v for k, v in preset.items()
                 if k in ("style", "character", "negative", "shots")})
    if preset.get("custom"):
        # A made category is a style, not a cast: its "character" was the
        # subject of the prompt it came from and would appear in every film.
        look["character"] = ""
    # How the stills move belongs to the category: a Vox category puts the
    # board on, and switching back to a plain one takes it off.
    if preset.get("motion") == "board":
        look["motion"] = "board"
    else:
        look.pop("motion", None)
    look["character_ref"] = keep_ref
    look["style_ref"] = ""
    with open(sub(pid, "look.json"), "w", encoding="utf-8") as f:
        json.dump(look, f, indent=2)
    save_settings(pid, {"look_mode": mode})
    return look


def is_3d_reference(ref_path):
    if not ref_path:
        return False
    name = os.path.basename(ref_path).lower()
    return any(k in name for k in ["3d", "chatgpt", "pixar", "cgi", "nanni", "neanderthal", "first series"])


def point_look_at(pid, ref_path):
    """Set character_ref for project, auto-selecting 3D preset if it's a 3D ref."""
    look = read_json(sub(pid, "look.json"), {}) or {}
    if look.get("custom"):
        return look          # a custom style has no character to pin

    look["character_ref"] = os.path.abspath(ref_path)
    if is_3d_reference(ref_path):
        return apply_look(pid, "3d")
    else:
        if not look.get("style"):
            return apply_look(pid, "2d")
        else:
            with open(sub(pid, "look.json"), "w", encoding="utf-8") as f:
                json.dump(look, f, indent=2)
            return look


def save_ref(name, b64_data):
    """Save an uploaded reference image into REFS folder."""
    import base64
    os.makedirs(REFS, exist_ok=True)
    clean_name = os.path.basename(name)
    target = os.path.join(REFS, clean_name)
    if "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]
    raw = base64.b64decode(b64_data)
    with open(target, "wb") as f:
        f.write(raw)
    return target


def refs_available():
    if not os.path.isdir(REFS):
        return []
    return sorted(n for n in os.listdir(REFS)
                  if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))


def move_to_trash(src, trash_dir):
    """Delete = move into a `_trash` folder next to the item, never an rm.

    Recoverable on purpose: a mis-click on 'delete project' must not destroy work.
    The `_trash` folders are hidden from the app (projects skip names starting
    with '_'; refs list only top-level image files, not the subfolder)."""
    os.makedirs(trash_dir, exist_ok=True)
    base = os.path.basename(src.rstrip("\\/"))
    dest = os.path.join(trash_dir, base)
    if os.path.exists(dest):
        dest = os.path.join(trash_dir, f"{base}.{int(time.time())}")
    os.rename(src, dest)
    return dest


def latest_project():
    projs = projects()
    if not projs:
        return None
    valid = [p for p in projs if os.path.isdir(folder(p))]
    if not valid:
        return None
    return max(valid, key=lambda p: os.path.getmtime(folder(p)))



def _gemini_list_paths():
    """Where the list of Gemini keys is kept (beside the single-key file)."""
    user = os.path.join(os.path.dirname(_key_file("gemini", write=True)), ".gemini_keys")
    repo = os.path.join(ROOT_DIR, "bulkgen", ".gemini_keys")
    return list(dict.fromkeys([user, repo]))


def _gemini_list():
    """The saved Gemini keys, in the order the prompt stage uses them."""
    bulk_dir = os.path.join(ROOT_DIR, "bulkgen")
    if bulk_dir not in sys.path:
        sys.path.insert(0, bulk_dir)
    import common as _common
    return _common.gemini_keys()


def _write_gemini_list(keys):
    """Save the list; the first key also stays in the single-key file every
    other feature reads, so nothing that used one key changes."""
    keys = [k for k in dict.fromkeys(k.strip() for k in keys) if k]
    singles = list(dict.fromkeys([_key_file("gemini", write=True),
                                  os.path.join(ROOT_DIR, "bulkgen", ".gemini_key")]))
    for path in _gemini_list_paths():
        try:
            if keys:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(keys) + "\n")
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    for path in singles:
        try:
            if keys:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(keys[0])
            elif os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    if keys:
        os.environ["GEMINI_API_KEY"] = keys[0]
    else:
        os.environ.pop("GEMINI_API_KEY", None)
    _VOICES["at"] = 0.0
    return keys


def _all_key_paths():
    """Every place either API key can be saved or read from."""
    out = list(_gemini_list_paths())
    for which in ("ai33", "gemini"):
        rel = {"gemini": ("bulkgen", ".gemini_key"),
               "ai33": ("ai_avatar_pipeline", ".ai33_key")}[which]
        out += [_key_file(which, write=True), _key_file(which), os.path.join(ROOT_DIR, *rel)]
        try:
            bulk_dir = os.path.join(ROOT_DIR, "bulkgen")
            if bulk_dir not in sys.path:
                sys.path.insert(0, bulk_dir)
            import common as _common
            out += _common.key_files(*rel)
        except Exception:
            pass
    return out


def _workspace_where():
    """What the workspace reset may touch, resolved the way this copy runs."""
    home = os.path.expanduser("~")
    pubs = [os.environ.get("STICKFILM_PUB_BASE") or "", os.path.join(home, "StickFilm"),
            r"D:\Video Projects", os.path.join(home, "Video Projects")]
    extra = [os.path.join(HERE, "live_canvas.jpg"), os.path.join(ROOT_DIR, "live_canvas.jpg")]
    try:
        import captions as _cap
        extra.append(_cap._settings_file())
    except Exception:
        pass
    return {"projects": PROJECTS, "scripts": studio_api.SCRIPTS_DIR, "refs": REFS,
            "looks": LOOKS, "data_dir": _data_dir(), "queue_file": PROD_QUEUE_FILE,
            "profiles_dir": studio_api.PROFILES_DIR,
            "pub_dirs": [d for d in dict.fromkeys(pubs) if d and os.path.isdir(d)],
            "extra_files": extra}


def _key_file(which, write=False):
    """Where a buyer's API key is kept.

    Beside the code while that is writable, so a developer checkout is
    unchanged; under the user's own folder once the app is installed, because
    Program Files is not writable and saving a key would simply fail.

    `write=True` always answers with the user's own folder. The fallback to a
    key left beside the code is for reading only: returned for a save, it sent
    the write into Program Files and the buyer got "Permission denied" - a key
    put there by a first launch that ran elevated from the installer.
    """
    rel = {"gemini": ("bulkgen", ".gemini_key"),
           "ai33": ("ai_avatar_pipeline", ".ai33_key")}[which]
    try:
        import userdata
        base = userdata.base()
    except Exception:
        base = ROOT_DIR
    path = os.path.join(base, *rel)
    # An older copy may have left a key beside the code; keep reading it.
    old = os.path.join(ROOT_DIR, *rel)
    if not write and not os.path.isfile(path) and os.path.isfile(old):
        return old
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    return path


def collect(pid):
    if not pid or not os.path.isdir(folder(pid)):
        pid = latest_project() or pid
        Handler.project = pid
    if not pid or not os.path.isdir(folder(pid)):
        return {"id": pid, "project": pid, "missing": True, "projects": projects(),
                "profiles": profiles(), "refs": refs_available(),
                "looks": looks_available(), "voices": voices_available(),
                "job": with_progress(JOB.snapshot(), pid=(Handler.project or pid)),
                "progress_info": calculate_full_progress(JOB.lines, JOB.running, stage=JOB.stage, pid=(Handler.project or pid))}

    state = read_json(sub(pid, "state.json"), {}) or {}
    look = read_json(sub(pid, "look.json"), {}) or {}
    rows = state.get("segments") or 0
    if not rows:
        rows = len(read_json(sub(pid, "segments.json"), []) or [])

    prompts = 0
    if os.path.isfile(sub(pid, "prompts.txt")):
        with open(sub(pid, "prompts.txt"), encoding="utf-8") as f:
            prompts = sum(1 for line in f if line.strip())

    script = ""
    if os.path.isfile(sub(pid, "script.txt")):
        with open(sub(pid, "script.txt"), encoding="utf-8") as f:
            script = f.read()

    scenes = 0
    if os.path.isfile(sub(pid, "scenes.txt")):
        with open(sub(pid, "scenes.txt"), encoding="utf-8-sig") as f:
            scenes = sum(1 for line in f if line.strip())

    images, clips = count(sub(pid, "images")), count(sub(pid, "clips"))
    duration = state.get("duration") or 0
    s = settings(pid)
    # The share is what the user actually set; the count only exists once the
    # narration's length is known. Never let a page refresh re-plan anything
    # that was already committed — `summary` only computes, never writes.
    # If the clip plan cannot be worked out, the rest of the state must still
    # come back: an exception here used to fail the whole request, and the
    # page sat with an empty project list and voices stuck "loading".
    studio_offline = False
    try:
        est_clips = (round(mix.most_clips(duration, s["clip_secs"]) *
                           s["clip_share"] / 100) if duration else 0)
        m = mix.summary(duration, est_clips, s["clip_secs"], s["still_secs"]) if duration else None
    except Exception:
        est_clips, m, studio_offline = 0, None, True
    saved = mix.load(pid) if os.path.isfile(sub(pid, "mix.json")) else None
    try:
        kinds = (mix.kinds_for(pid, rows) if rows else
                 [k for k, _ in mix.plan_shots(duration, est_clips, s["clip_secs"], s["still_secs"])])
    except Exception:
        kinds, studio_offline = [], True
    planned = sum(1 for k in kinds if k == "clip") if rows else 0

    stages = [
        {"id": "voice", "label": "voice",
         "done": os.path.isfile(sub(pid, "voiceover.mp3")),
         "detail": f"{state.get('duration', 0)/60:.1f} min"
                   if state.get("duration") else "no narration yet"},
        # The mix is its own step because `rows` cuts to it. Left unanswered,
        # `mix.load` plans every clip that would fit and the cut is made to a
        # number nobody picked — the choice has to happen before rows, not
        # after, because after is too late to change how many pictures exist.
        {"id": "mix", "label": "mix",
         "done": bool(saved and saved.get("targets")),
         "detail": (f"{saved.get('clips')} clips · {saved.get('credits')} credits"
                    if saved and saved.get("targets") else "kitni clips? abhi tay nahi")},
        {"id": "rows", "label": "rows", "done": bool(rows),
         "detail": f"{rows} rows · avg {state.get('seconds_per_clip', '?')}s"
                   if rows else ""},
        {"id": "scenes", "label": "scenes", "done": bool(rows) and scenes == rows,
         "now": scenes, "total": rows,
         "detail": (f"{scenes}/{rows} scene lines" if rows else
                    "rows ke baad")},
        {"id": "prompts", "label": "prompts", "done": prompts > 0,
         "detail": f"{prompts} prompts" if prompts else ""},
        {"id": "images", "label": "images", "done": bool(rows) and images == rows,
         "now": images, "total": rows, "detail": f"{images}/{rows or '?'}"},
        {"id": "clips", "label": "clips",
         "done": bool(planned) and clips >= planned,
         "now": clips, "total": planned,
         "detail": f"{clips}/{planned or '?'} planned"},
        {"id": "render", "label": "render",
         "done": os.path.isfile(sub(pid, "final.mp4")),
         "detail": f"{state.get('final_seconds', 0)/60:.1f} min"
                   if state.get("final_seconds") else ""},
        {"id": "publish", "label": "publish", "done": bool(state.get("published")),
         "detail": state.get("published") or ""},
    ]

    return {
        "project": pid,
        "projects": projects(),
        "folder": folder(pid),
        "script": script,
        "words": len(script.split()),
        "rows": rows,
        "duration": state.get("duration"),
        "avg": state.get("seconds_per_clip"),
        "images": images,
        "scenes": scenes,
        "clips": clips,
        "paired": bool(rows) and images == rows,
        "prompts": prompts,
        "stages": stages,
        "mix": m,
        "saved_mix": {"shots": (saved or {}).get("shots"),
                      "clips": (saved or {}).get("clips")},
        "planned_clips": planned,
        "kinds": kinds,
        "studio_offline": studio_offline,
        "placement": s.get("placement", "front"),
        "manual": s.get("manual", []),
        # Once the rows exist they are the truth: the plan asks for N clips,
        # the cut lands on sentence ends and gives you however many it gives.
        # The number that decides a purchase has to be the second one.
        "credits": (planned * CREDITS_PER_CLIP) if rows else
                   (m["credits"] if m else 0),
        "settings": settings(pid),
        "profiles": profiles(),
        "voices": voices_available(),
        "refs": refs_available(),
        "looks": looks_available(),
        "look_mode": "3d" if (is_3d_reference(look.get("character_ref")) or ("3d" in look.get("style", "").lower() and "2d" not in look.get("style", "").lower())) else settings(pid).get("look_mode", "2d"),
        "look": {"character": look.get("character", ""),
                 "character_ref": look.get("character_ref", ""),
                 "style": look.get("style", "")},
        # So the page knows on load which style is in charge, and can grey out
        # the reference picker before the buyer touches anything.
        "custom_style_on": bool(look.get("custom")),
        "custom_style": look.get("style", "") if look.get("custom") else "",
        "job": with_progress(JOB.snapshot(), pid=(Handler.project or pid)),
        "progress_info": (calculate_full_progress(JOB.lines, True, JOB.stage, pid=(Handler.project or pid)) if JOB.running
                          else calculate_disk_progress(pid)),
        "running_project": (Handler.project if JOB.running else None),
    }


# ── doing things ─────────────────────────────────────────────────────────────

class _Py(list):
    """[python, -u, pipeline.py], the script looked up each time it is used."""

    def __add__(self, more):
        env.child_env_path()
        return [sys.executable, "-u", env.script("vidgen/pipeline.py")] + list(more)


PY = _Py()

# The `clips` stage argparse only accepts the word form (it forwards straight
# to omni_videos.py, whose own --aspect choices are "landscape"/"portrait"),
# while the UI and every other stage speak the ratio ("16:9" / "9:16").
CLIP_ASPECT_WORD = {"16:9": "landscape", "9:16": "portrait",
                     "landscape": "landscape", "portrait": "portrait"}


def command(pid, stage, body):
    """The argv for a stage — the same line you would type in the shell."""
    s = settings(pid)
    if stage == "voice":
        return PY + ["voice", pid, "--voice", s["voice"]]
    if stage == "rows":
        return PY + ["rows", pid]
    if stage == "prompts":
        # Always through `scenes.txt`. Without `--scenes` this stage falls back
        # to template mode, which pastes the narration line in as the picture —
        # abstract lines then come back as generic filler, which is exactly the
        # failure that had to be diagnosed off a finished film once already.
        scenes = sub(pid, "scenes.txt")
        if not os.path.isfile(scenes):
            raise ValueError("scenes.txt nahi hai — har row ke liye ek picture "
                             "chahiye. Template mode se prompts nahi banate.")
        return PY + ["prompts", pid, "--scenes", scenes]
    if stage == "images":
        aspect_val = s.get("aspect", "16:9")
        return PY + ["images", pid, "--profile", s["profile"],
                     "--aspect", aspect_val,
                     "--batch", str(int(s["batch"]))]
    if stage == "clips":
        clip_aspect = CLIP_ASPECT_WORD.get(s["aspect"], "landscape")
        argv = PY + ["clips", pid, "--aspect", clip_aspect,
                     "--model", s["engine"]]
        if body.get("dry_run", True):
            argv.append("--dry-run")
        return argv
    if stage == "collect":
        return PY + ["clips", pid, "--collect"]
    if stage == "render":
        return PY + ["render", pid, "--aspect", s["aspect"], "--jobs", str(int(s["jobs"]))]
    if stage == "status":
        return PY + ["status", pid]
    raise ValueError(f"unknown stage {stage!r}")


def write_script(pid, text):
    os.makedirs(folder(pid), exist_ok=True)
    with open(sub(pid, "script.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return len(text.split())


def save_ref(name, data_url):
    """A reference image dropped on the page or uploaded, kept in vidgen's own refs and mirrored to root refs."""
    head, _, b64 = data_url.partition(",")
    if "base64" not in head:
        raise ValueError("expected a base64 data URL")
    os.makedirs(REFS, exist_ok=True)
    root_refs = os.path.join(os.path.dirname(HERE), "refs")
    safe = re.sub(r'[\\/:*?"<>|\s]+', '_', os.path.basename(name)).strip('_') or "reference.png"
    if not any(safe.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
        safe += ".png"
    path = os.path.join(REFS, safe)
    data = base64.b64decode(b64)
    with open(path, "wb") as f:
        f.write(data)
    # A second copy beside the app, for older stages that look there. Only a
    # courtesy: installed, that folder is Program Files and may not be written,
    # and creating it there used to fail the whole upload (WinError 5).
    try:
        if os.path.normcase(os.path.abspath(root_refs)) != os.path.normcase(os.path.abspath(REFS)):
            os.makedirs(root_refs, exist_ok=True)
            with open(os.path.join(root_refs, safe), "wb") as f2:
                f2.write(data)
    except Exception:
        pass
    return path


def is_3d_reference(ref_path):
    if not ref_path:
        return False
    base = os.path.basename(ref_path).lower()
    return any(k in base for k in ["3d", "chatgpt", "pixar", "cgi", "nanni", "neanderthal", "first series"])


def point_look_at(pid, ref_path):
    """`character_ref` on the project's own look, auto-detecting 3D vs 2D style presets."""
    path = sub(pid, "look.json")
    is_3d = is_3d_reference(ref_path)
    preset_file = os.path.join(LOOKS, "3d.json" if is_3d else "2d.json")
    preset = read_json(preset_file, {}) or {}

    look = read_json(path, None) or {}
    if not look or is_3d or not look.get("style") or ("stickman" in look.get("style", "").lower() and is_3d):
        look.update({k: v for k, v in preset.items()
                     if k in ("style", "character", "negative", "shots")})
    look["character_ref"] = ref_path
    look["style_ref"] = ""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(look, f, indent=2)
    save_settings(pid, {"look_mode": "3d" if is_3d else "2d"})

def _film_file(pid):
    """The finished film for a project: its final.mp4, or the copy published
    to Deliverables when the project's own is gone. None while neither exists.
    A render in progress writes final.part.mp4, so it is never picked up."""
    mp4 = sub(pid, "final.mp4")
    if os.path.isfile(mp4) and os.path.getsize(mp4) > 0:
        return mp4
    pub = os.path.join(studio_api.PUB_BASE, pid, f"{pid}.mp4")
    if os.path.isfile(pub) and os.path.getsize(pub) > 0:
        return pub
    return None


def list_ready_videos():
    """All finished videos across projects with metadata."""
    videos = []
    for pid in projects():
        mp4 = _film_file(pid)
        if mp4:
            st = read_json(sub(pid, "state.json"), {}) or {}
            size_mb = round(os.path.getsize(mp4) / (1024 * 1024), 1)
            dur = st.get("final_seconds") or st.get("duration") or 0
            videos.append({
                "id": pid,
                "title": st.get("title") or pid,
                "path": mp4,
                # Changes whenever the film does, so the browser never plays a
                # copy it cached before the render finished.
                "url": f"/video/{pid}?v={int(os.path.getmtime(mp4))}",
                "size_mb": size_mb,
                "duration": dur,
                "duration_str": f"{dur/60:.1f} min" if dur else "?",
                "published": st.get("published"),
            })
    return videos


def find_image_for_row(pid, n):
    """Find the image corresponding to shot index n."""
    img_dir = sub(pid, "images")
    if os.path.isdir(img_dir):
        tag = f"_i{n:03d}."
        tag_extra = f"_i{n:03d}_"
        for f in sorted(os.listdir(img_dir)):
            if tag in f or tag_extra in f:
                return os.path.join(img_dir, f)
        # Position fallback
        imgs = [f for f in sorted(os.listdir(img_dir))
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        if 1 <= n <= len(imgs):
            return os.path.join(img_dir, imgs[n - 1])
    # Check extra images
    extra_dir = sub(pid, "images_extra")
    if os.path.isdir(extra_dir):
        tag = f"_i{n:03d}_"
        for f in sorted(os.listdir(extra_dir)):
            if tag in f:
                return os.path.join(extra_dir, f)
    return None


def get_editor_data(pid):
    """Full timeline and shot data for the interactive web editor."""
    pids = projects()
    if not pid or pid not in pids:
        pid = pids[0] if pids else "01_FIRST_WEAPON"
    
    seg_file = sub(pid, "segments.json")
    rows = read_json(seg_file, []) if os.path.isfile(seg_file) else []
    
    scenes_map = {}
    scenes_file = sub(pid, "scenes.txt")
    if os.path.isfile(scenes_file):
        with open(scenes_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                num_part, _, text_part = line.partition("|")
                try:
                    scenes_map[int(num_part.strip())] = text_part.strip()
                except ValueError:
                    pass

    prompts_file = sub(pid, "prompts.txt")
    prompts_list = []
    if os.path.isfile(prompts_file):
        with open(prompts_file, "r", encoding="utf-8", errors="replace") as f:
            prompts_list = [line.strip() for line in f if line.strip()]

    edit_data = read_json(sub(pid, "edit.json"), []) or []
    moves_map = {item.get("n"): item.get("move", "static") for item in edit_data if isinstance(item, dict)}

    st = read_json(sub(pid, "state.json"), {}) or {}
    audio_path = sub(pid, "voiceover.mp3")
    has_audio = os.path.isfile(audio_path)
    audio_dur = probe(audio_path) if has_audio else (st.get("duration") or 0.0)

    shots = []
    for i, r in enumerate(rows, 1):
        n = r.get("n", i)
        start = round(r.get("start", 0.0), 3)
        end = round(r.get("end", 0.0), 3)
        dur = round(end - start, 3)
        text = r.get("text", "")
        scene = scenes_map.get(n, "")
        if not scene and (n - 1) < len(prompts_list):
            scene = prompts_list[n - 1]
        
        img_path = find_image_for_row(pid, n)
        has_img = img_path is not None and os.path.isfile(img_path)
        
        shots.append({
            "n": n,
            "start": start,
            "end": end,
            "dur": dur,
            "text": text,
            "scene": scene,
            "motion": moves_map.get(n, "static"),
            "image_url": f"/image/{pid}/{n}",
            "has_image": has_img,
        })

    return {
        "id": pid,
        "title": st.get("title") or pid.replace("_", " ").title(),
        "projects": projects(),
        "audio_url": f"/audio/{pid}" if has_audio else None,
        "audio_duration": round(audio_dur, 2),
        "has_audio": has_audio,
        "total_shots": len(shots),
        "has_final": os.path.isfile(sub(pid, "final.mp4")),
        "final_url": f"/video/{pid}" if os.path.isfile(sub(pid, "final.mp4")) else None,
        "shots": shots,
    }


def save_editor_data(pid, data):
    """Save user edits to segments, scenes, and prompts."""
    shots = data.get("shots", [])
    if not shots:
        return {"ok": False, "error": "no shots provided"}

    # 1. Update segments.json
    seg_file = sub(pid, "segments.json")
    rows = []
    for s in shots:
        rows.append({
            "n": int(s.get("n", 1)),
            "start": float(s.get("start", 0.0)),
            "end": float(s.get("end", 0.0)),
            "text": s.get("text", "").strip(),
        })
    with open(seg_file, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)

    # 2. Update scenes.txt
    scenes_file = sub(pid, "scenes.txt")
    with open(scenes_file, "w", encoding="utf-8", newline="\n") as f:
        for s in shots:
            n = s.get("n", 1)
            sc = s.get("scene", "").strip() or s.get("text", "").strip()
            f.write(f"{n}| {sc}\n")

    # 3. Update edit.json
    edit_file = sub(pid, "edit.json")
    edit_data = []
    for s in shots:
        n = int(s.get("n", 1))
        edit_data.append({
            "n": n,
            "start": float(s.get("start", 0.0)),
            "end": float(s.get("end", 0.0)),
            "dur": round(float(s.get("end", 0.0)) - float(s.get("start", 0.0)), 3),
            "text": s.get("text", "").strip(),
            "move": s.get("motion", "static"),
        })
    with open(edit_file, "w", encoding="utf-8", newline="\n") as f:
        json.dump(edit_data, f, indent=1, ensure_ascii=False)

    return {"ok": True, "saved_shots": len(shots)}


def save_uploaded_shot_image(pid, n, data_url):
    """Save an image uploaded directly for shot n."""
    head, _, b64 = data_url.partition(",")
    if "base64" not in head:
        raise ValueError("expected a base64 data URL")
    img_dir = sub(pid, "images")
    os.makedirs(img_dir, exist_ok=True)
    raw = base64.b64decode(b64)
    
    # Check if an existing image for row n exists
    target = find_image_for_row(pid, n)
    if not target:
        target = os.path.join(img_dir, f"00-00.00_i{n:03d}.png")
    with open(target, "wb") as f:
        f.write(raw)
    return target


def regenerate_single_shot_image(pid, n, reason, profile=None):
    """Refines the prompt using user feedback/reason and regenerates the image for shot n."""
    import zipfile
    import bulkgem
    import promptgen

    profile = profile or batch.free_profile() or "acc4"
    proj_look_file = sub(pid, "look.json")
    look = read_json(proj_look_file, {}) or promptgen.load_look()

    # 1. Get original scene description
    scenes_file = sub(pid, "scenes.txt")
    scenes = []
    if os.path.isfile(scenes_file):
        with open(scenes_file, "r", encoding="utf-8", errors="replace") as f:
            scenes = [line.strip() for line in f if line.strip()]

    orig_scene = ""
    for line in scenes:
        if line.startswith(f"{n}|"):
            _, _, orig_scene = line.partition("|")
            orig_scene = orig_scene.strip()
            break
    if not orig_scene and 0 < n <= len(scenes):
        _, _, orig_scene = scenes[n - 1].partition("|")
        orig_scene = orig_scene.strip()

    if not orig_scene:
        seg_file = sub(pid, "segments.json")
        rows = read_json(seg_file, []) or []
        row = next((r for r in rows if r.get("n") == n), None)
        if row:
            orig_scene = row.get("text", "")

    # 2. Refine the scene with user feedback
    improved_scene = f"{orig_scene}. Modification based on user feedback: {reason}" if orig_scene else reason

    # Update scenes.txt
    new_scenes_lines = []
    found = False
    for line in scenes:
        if line.startswith(f"{n}|"):
            new_scenes_lines.append(f"{n}| {improved_scene}")
            found = True
        else:
            new_scenes_lines.append(line)
    if not found:
        new_scenes_lines.append(f"{n}| {improved_scene}")
    with open(scenes_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(new_scenes_lines) + "\n")

    # 3. Generate refined prompt using look constraints
    cast = (look.get("character") or "").strip()
    parts = [f"{look.get('style', promptgen.DEFAULT_LOOK['style'])}."]
    if cast:
        parts.append(f"{cast}.")
    parts.append(improved_scene.rstrip(".") + ".")
    if look.get("negative"):
        parts.append(look["negative"] + ".")
    refined_prompt = promptgen.one_line(" ".join(parts))

    # Update prompts.txt line n
    prompts_file = sub(pid, "prompts.txt")
    if os.path.isfile(prompts_file):
        with open(prompts_file, "r", encoding="utf-8", errors="replace") as f:
            prompts = [p.strip() for p in f]
        if 0 < n <= len(prompts):
            prompts[n - 1] = refined_prompt
        else:
            prompts.append(refined_prompt)
        with open(prompts_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(prompts) + "\n")

    # 4. Generate image using bulkgem single prompt run
    shot_dir = sub(pid, "bulk")
    page, frame = bulkgem.open_app(profile, bulkgem.BULK_APP_URL, shot_dir)
    bulkgem.show_image_tab(frame)
    bulkgem.set_aspect(frame, look.get("aspect", "16:9"))
    bulkgem.enable_character(frame, look.get("character_ref"), look.get("character"))
    bulkgem.enable_style(frame, look.get("style_ref"), look.get("style"))
    bulkgem.fill_prompts(frame, [refined_prompt])

    bulkgem.generate(page, frame, 1, 15, shot_dir)

    tmp_zip = os.path.join(tempfile.gettempdir(), f"regen_{pid}_{n}_{int(time.time())}.zip")
    bulkgem.download_zip(page, frame, tmp_zip)

    # 5. Place into images/ folder
    img_dir = sub(pid, "images")
    os.makedirs(img_dir, exist_ok=True)
    target_img = find_image_for_row(pid, n)
    if not target_img:
        target_img = os.path.join(img_dir, f"00-00.00_i{n:03d}.png")

    with zipfile.ZipFile(tmp_zip, "r") as zf:
        members = [m for m in zf.namelist() if not m.endswith("/")]
        if members:
            with open(target_img, "wb") as out_f:
                out_f.write(zf.read(members[0]))
    try:
        os.remove(tmp_zip)
    except Exception:
        pass

    # 6. Re-render individual shot if shots folder exists
    shot_mp4 = sub(pid, "shots", f"shot_{n:03d}.mp4")
    if os.path.isfile(shot_mp4):
        try:
            seg_file = sub(pid, "segments.json")
            rows = read_json(seg_file, []) or []
            row = next((r for r in rows if r.get("n") == n), None)
            dur = row.get("dur", 2.3) if row else 2.3
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", target_img,
                "-t", str(dur),
                "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                shot_mp4
            ]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run(cmd, timeout=30, capture_output=True, creationflags=flags)
        except Exception:
            pass

    return {
        "ok": True,
        "n": n,
        "new_prompt": refined_prompt,
        "new_scene": improved_scene,
        "image_url": f"/image/{pid}/{n}?t={int(time.time())}",
        "target_path": target_img
    }


def _page_path():
    """The dashboard page, beside this file."""
    p = os.path.join(HERE, "dash.html")
    return p if os.path.isfile(p) else None


class Handler(BaseHTTPRequestHandler):
    project = None

    # -- plumbing ------------------------------------------------------------
    def _send(self, body, ctype="application/json", code=200):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), "application/json", code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _stream_file(self, filepath, ctype="video/mp4"):
        if not os.path.isfile(filepath):
            return self._json({"error": "file not found"}, 404)
        file_size = os.path.getsize(filepath)
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(filepath, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(remaining, 65536))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        remaining -= len(chunk)
                return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break

    def log_message(self, *a):
        pass

    # -- routes --------------------------------------------------------------
    def _category(self, path, body):
        """New category from a prompt: draft, samples, status, save, list."""
        import categories
        try:
            if path == "/api/category/list":
                return self._json({"ok": True, "categories": categories.listing()})
            if path == "/api/category/draft":
                return self._json({"ok": True, "look": categories.draft(
                    body.get("prompt", ""), body.get("profile") or "acc1",
                    body.get("reference_images") or [])})
            if path == "/api/category/samples":
                ok, note = categories.start_samples(
                    body.get("slug"), JOB, sys.executable, env.script("vidgen/pipeline.py"),
                    profile=body.get("profile") or "acc1",
                    aspect=body.get("aspect") or "16:9")
                return self._json({"ok": ok, "note": note})
            if path == "/api/category/status":
                return self._json(categories.status(body.get("slug"), JOB))
            if path == "/api/category/delete":
                busy = [x.get("look") for x in load_prod_queue()
                        if x.get("status") in ("queued", "running")]
                categories.delete(body.get("slug"), in_use=busy)
                return self._json({"ok": True, "looks": looks_available()})
            if path == "/api/category/save":
                look = categories.save(body.get("slug"), body.get("label"))
                return self._json({"ok": True, "look": look, "looks": looks_available()})
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        return self._json({"ok": False, "error": "unknown category action"}, 404)

    def do_GET(self):
        return self._do_GET()

    def do_POST(self):
        return self._do_POST()

    def _do_GET(self):
        raw_path = self.path.split("?")[0]
        path = urllib.parse.unquote(raw_path)

        # The catch-all below answers every unmatched GET with the dashboard
        # page, so without this route a browser asking for /favicon.ico got
        # 200 text/html and fell back to its generic globe in the taskbar.
        if path == "/app_icon.png":
            for cand in (os.path.join(ROOT_DIR, "app_icon.png"), os.path.join(HERE, "assets", "app_icon.png")):
                if os.path.isfile(cand):
                    with open(cand, "rb") as f:
                        blob = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(blob)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(blob)
                    return
            self.send_error(404)
            return

        if path in ("/favicon.ico", "/stickfilm_icon.ico", "/app.ico"):
            for cand in (os.path.join(ROOT_DIR, "app.ico"), os.path.join(HERE, "assets", "frameloom.ico")):
                if os.path.isfile(cand):
                    with open(cand, "rb") as f:
                        blob = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/x-icon")
                    self.send_header("Content-Length", str(len(blob)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(blob)
                    return
            for cand in (os.path.join(HERE, "assets", "frameloom.ico"),
                         os.path.join(ROOT_DIR, "stickfilm_icon.ico"),
                         os.path.join(HERE, "stickfilm_icon.ico")):
                if os.path.isfile(cand):
                    with open(cand, "rb") as f:
                        blob = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/x-icon")
                    self.send_header("Content-Length", str(len(blob)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(blob)
                    return
            self.send_error(404)
            return

        # The launcher asks this to tell a dashboard from this install apart
        # from one an older install left running.
        if path == "/api/version":
            return self._json(version_info())

        if path == "/api/update/check":
            return self._json(updater.check(force="force" in self.path))

        if path == "/api/update/status":
            return self._json(updater.status())

        if path == "/api/tts/status":
            return self._json(tts.providers_status())

        if path == "/api/tts/voices":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            prov = (q.get("provider") or [""])[0]
            try:
                return self._json(tts.list_voices(prov))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:300]})

        if path == "/api/tts/quota":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            prov = (q.get("provider") or [""])[0]
            return self._json(tts.quota(prov))

        if path == "/api/tts/audio":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            fpath = tts.audio_path((q.get("name") or [""])[0])
            if not fpath:
                return self._json({"error": "file not found"}, 404)
            ctype = "audio/wav" if fpath.endswith(".wav") else "audio/mpeg"
            return self._stream_file(fpath, ctype)

        if path == "/api/plan":
            return self._json({"ok": True, **plan_info(refresh="refresh" in self.path)})

        if path == "/api/voice/estimate":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            text = (q.get("text") or [""])[0]
            try:
                words = int((q.get("words") or ["0"])[0])
            except ValueError:
                words = 0
            if words and not text:
                text = "w " * words      # the page sends only the count
            return self._json(estimate_length(text, (q.get("voice") or [""])[0]))

        if path == "/api/voices/browse":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            g = lambda k, d="": (q.get(k) or [d])[0]
            try:
                return self._json({"ok": True, **voices_browse(
                    g("provider", "elevenlabs"), g("search").strip(), int(g("page", "1") or 1),
                    g("language"), g("gender"))})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})

        if path in ("/api/voices/preview", "/api/voices/sample"):
            # Samples play through here: some CDNs refuse a request without a
            # browser User-Agent, and only the voice hosts are fetched.
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            u = (q.get("u") or [""])[0]
            if path == "/api/voices/sample":
                try:
                    u = voice_sample_url((q.get("id") or [""])[0])
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)[:200]}, 502)
                if not u:
                    return self._json({"ok": False, "error": "this voice has no sample"}, 404)
            host = urllib.parse.urlparse(u).hostname or ""
            if not u.startswith("https://") or not any(
                    host == h or host.endswith("." + h) for h in PREVIEW_HOSTS):
                return self._json({"ok": False, "error": "not a voice sample"}, 400)
            try:
                import requests
                r = requests.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0 StickFilm"})
                r.raise_for_status()
                # Some hosts label the mp3 text/plain; a player may refuse that.
                ctype = r.headers.get("Content-Type") or ""
                return self._send(r.content, ctype if ctype.startswith("audio/") else "audio/mpeg")
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]}, 502)

        if path == "/api/log/full":
            # Everything the last run printed, not just the 400 lines on screen.
            text = ""
            try:
                if Job.LOG_FILE and os.path.exists(Job.LOG_FILE):
                    with open(Job.LOG_FILE, encoding="utf-8", errors="replace") as f:
                        text = f.read()
            except Exception:
                pass
            if not text:
                text = "\n".join(JOB.lines)
            return self._send(text.encode("utf-8"), "text/plain; charset=utf-8")

        if path == "/api/state":
            qs = urllib.parse.parse_qs(self.path.partition("?")[2]) if "?" in self.path else {}
            req_pid = qs.get("project", [None])[0]
            if not JOB.running:
                pid = req_pid or Handler.project or latest_project()
                if pid and os.path.isdir(folder(pid)):
                    Handler.project = pid
            else:
                pid = Handler.project or req_pid or latest_project()
            self._json(collect(pid))
        elif path == "/api/batch_status":
            self._json(get_batch_status())
        elif path == "/api/videos":
            self._json(list_ready_videos())
        elif path == "/api/scripts":
            self._json(studio_api.get_available_scripts())
        elif path == "/api/refs":
            self._json(studio_api.get_available_refs())
        elif path == "/api/profiles":
            self._json(studio_api.get_browser_profiles())
        elif path.startswith("/image_raw/"):
            rest = path[11:]
            r_pid, _, r_name = rest.partition("/")
            r_pid = urllib.parse.unquote(r_pid)
            r_name = urllib.parse.unquote(r_name)
            # never let a name climb out of the project's own folder
            if not r_name or "/" in r_name or "\\" in r_name or ".." in r_name:
                return self._json({"error": "bad name"}, 400)
            fp = os.path.join(folder(r_pid), "images_raw", r_name)
            if os.path.isfile(fp):
                ctype = "image/png" if fp.lower().endswith(".png") else "image/jpeg"
                with open(fp, "rb") as fh:
                    return self._send(fh.read(), ctype)
            return self._json({"error": "not found"}, 404)
        elif path.startswith("/category_preview/"):
            r_pid, _, r_name = path[len("/category_preview/"):].partition("/")
            if (not r_pid.startswith("_preview_custom_") or not r_name
                    or ".." in r_pid or "\\" in r_pid
                    or "/" in r_name or "\\" in r_name or ".." in r_name):
                return self._json({"error": "bad name"}, 400)
            fp = os.path.join(folder(r_pid), "previews", r_name)
            if os.path.isfile(fp):
                with open(fp, "rb") as fh:
                    return self._send(fh.read(), "image/png")
            return self._json({"error": "not found"}, 404)
        elif path.startswith("/api/live_images/"):
            pid = path[17:]
            self._json(studio_api.get_live_images(pid))
        elif path == "/api/canvas_stream":
            svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
                <rect width="100%" height="100%" fill="#0d1117"/>
                <circle cx="320" cy="130" r="34" fill="#161b22" stroke="#64748b" stroke-width="2"/>
                <text x="320" y="139" font-family="sans-serif" font-size="22" fill="#64748b" text-anchor="middle">&#128721;</text>
                <text x="320" y="200" font-family="sans-serif" font-size="16" font-weight="bold" fill="#ffffff" text-anchor="middle">Browser Offline / Stopped</text>
                <text x="320" y="226" font-family="sans-serif" font-size="12" fill="#94a3b8" text-anchor="middle">Click "Switch Profile &amp; Resume" or "Generate Images" to start live generation</text>
            </svg>'''

            live_target = os.path.join(HERE, "live_canvas.jpg")
            pid_target = sub(Handler.project or "", "live_canvas.jpg")
            target = live_target if os.path.isfile(live_target) else (pid_target if os.path.isfile(pid_target) else None)
            if not target and Handler.project:
                img_dir = sub(Handler.project, "images")
                b_dir = sub(Handler.project, "bulk")
                for c_dir in (img_dir, b_dir):
                    if os.path.isdir(c_dir):
                        shots = [os.path.join(c_dir, f) for f in os.listdir(c_dir) if f.lower().endswith(('.png', '.jpg', '.webp'))]
                        if shots:
                            target = max(shots, key=os.path.getmtime)
                            break
            if target and os.path.isfile(target):
                ctype = "image/png" if target.lower().endswith(".png") else "image/jpeg"
                try:
                    with open(target, "rb") as f:
                        return self._send(f.read(), ctype)
                except Exception:
                    pass

            return self._send(svg, "image/svg+xml")
        elif path.startswith("/audio/"):
            pid = path[7:]
            vo_path = sub(pid, "voiceover.mp3")
            if os.path.isfile(vo_path):
                return self._stream_file(vo_path, "audio/mpeg")
            self._json({"error": "voiceover not found"}, 404)
        elif path.startswith("/api/voice_status/"):
            pid = path[18:]
            vo_path = sub(pid, "voiceover.mp3")
            has_vo = os.path.isfile(vo_path)
            dur = probe(vo_path) if has_vo else 0.0
            self._json({
                "project": pid,
                "has_voice": has_vo,
                "duration": round(dur, 2) if dur else 0,
                "url": f"/audio/{urllib.parse.quote(pid)}" if has_vo else None
            })
        elif path == "/api/voice_clone/list":
            import voiceclone
            try:
                self._json({"ok": True, "voices": voiceclone.list_clones(_ai33_key()),
                            "credits": voiceclone.CLONE_CREDITS,
                            "allowed": bool(plan_limits().get("clone_voice", True))})
            except voiceclone.CloneError as exc:
                self._json({"ok": False, "error": str(exc), "voices": []})
            except Exception as exc:
                self._json({"ok": False, "error": f"Could not reach AI33: {str(exc)[:120]}", "voices": []})
        elif path == "/api/captions":
            import captions as capstage
            self._json({"ok": True, "settings": capstage.load_settings(),
                        **capstage.options()})
        elif path == "/api/keys_status":
            ai33_fp = _key_file("ai33")
            gem_fp = _key_file("gemini")
            ai33_val = open(ai33_fp, "r", encoding="utf-8").read().strip() if os.path.isfile(ai33_fp) else ""
            gem_val = open(gem_fp, "r", encoding="utf-8").read().strip() if os.path.isfile(gem_fp) else ""
            
            has_ai33 = bool(ai33_val and not ai33_val.startswith("#") and "PASTE" not in ai33_val)
            has_gem = bool(gem_val and not gem_val.startswith("#") and "PASTE" not in gem_val)
            
            def mask(k):
                if not k or len(k) < 6:
                    return ""
                return k[:3] + "..." + k[-4:]
            
            try:
                glist = [k for k in _gemini_list() if k and not k.startswith("#") and "PASTE" not in k]
            except Exception:
                glist = [gem_val] if has_gem else []
            self._json({
                "has_ai33": has_ai33,
                "ai33_masked": mask(ai33_val) if has_ai33 else "",
                "has_gemini": has_gem or bool(glist),
                "gemini_masked": mask(gem_val) if has_gem else (mask(glist[0]) if glist else ""),
                "gemini_keys": [{"i": i, "masked": mask(k)} for i, k in enumerate(glist)],
            })
        elif path == "/api/prod_queue":
            q = load_prod_queue()
            self._json({
                "queue": q,
                "queue_running": QUEUE_RUNNER_ENABLED,
                "active_id": CURRENT_QUEUE_ITEM_ID,
                "parallel": get_parallel(),
                "lanes": [{"lane": i + 1, "item": LANE_ITEM[i], "running": l.running,
                           "profile": l.profile if l.running else None, "stage": l.stage,
                           "seconds": round(time.time() - l.started) if l.running else 0,
                           "tail": list(l.lines)[-12:] if (l.running or LANE_ITEM[i]) else []}
                          for i, l in enumerate(LANES)],
                "job_running": JOB.running,
                "job_stage": JOB.stage,
                "current_project": Handler.project
            })
        elif path.startswith("/api/verify_images/"):
            pid = path[19:]
            self._json(studio_api.verify_project_images(pid))
        elif path.startswith("/api/project_script/"):
            pname = urllib.parse.unquote(path[20:]).strip()
            p_dir = os.path.join(PROJECTS, pname)
            s_path = os.path.join(p_dir, "script.txt")
            content = ""
            has_script = os.path.isfile(s_path)
            if has_script:
                try:
                    with open(s_path, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    pass
            img_dir = os.path.join(p_dir, "images")
            img_count = len([f for f in os.listdir(img_dir) if f.lower().endswith(('.png','.jpg','.webp'))]) if os.path.isdir(img_dir) else 0
            has_voice = os.path.isfile(os.path.join(p_dir, "voiceover.mp3"))
            self._json({
                "project": pname,
                "has_script": has_script,
                "script_text": content,
                "char_count": len(content),
                "word_count": len(content.split()),
                "has_voice": has_voice,
                "image_count": img_count
            })
        elif path.startswith("/api/deliverables/"):
            pid = path[18:]
            self._json(studio_api.get_deliverables(pid))
        elif path.startswith("/deliverable/"):
            parts = path[13:].split("/")
            if len(parts) >= 2:
                pid, fname = parts[0], parts[1]
                pub_fp = os.path.join(studio_api.PUB_BASE, pid, fname)
                local_fp = sub(pid, fname)
                target = pub_fp if os.path.isfile(pub_fp) else local_fp
                if os.path.isfile(target):
                    ctype = "image/jpeg" if fname.endswith((".jpg", ".jpeg")) else "image/png"
                    with open(target, "rb") as f:
                        return self._send(f.read(), ctype)
            self._json({"error": "deliverable not found"}, 404)
        elif path == "/api/editor":
            self._json(get_editor_data(Handler.project))
        elif path.startswith("/api/editor/"):
            pid = os.path.basename(path[12:])
            self._json(get_editor_data(pid))
        elif path == "/api/queue":
            self._json({"jobs": batch.queue_rows(),
                        "running": os.path.isfile(batch.STATUS),
                        "voices": voices_available(),
                        "profiles": profiles(),
                        "looks": looks_available(),
                        "next_profile": batch.free_profile()})
        elif path.startswith("/video/"):
            pid = os.path.basename(path[7:])
            mp4 = _film_file(pid) or sub(pid, "final.mp4")
            self._stream_file(mp4, "video/mp4")
        elif path.startswith("/audio/"):
            pid = os.path.basename(path[7:])
            audio = sub(pid, "voiceover.mp3")
            self._stream_file(audio, "audio/mpeg")
        elif path.startswith("/image/"):
            parts = path[7:].split("/")
            if len(parts) >= 2:
                pid, n_str = parts[0], parts[1]
                try:
                    n = int(n_str)
                    img = find_image_for_row(pid, n)
                    if img and os.path.isfile(img):
                        with open(img, "rb") as f:
                            return self._send(f.read(), "image/png")
                except ValueError:
                    pass
            # Fallback 1x1 transparent PNG or placeholder
            placeholder = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                           b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02"
                           b"\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82")
            self._send(placeholder, "image/png")
        elif path.startswith("/ref/"):
            name = os.path.basename(path[5:])
            full = os.path.join(REFS, name)
            if not os.path.isfile(full):
                root_ref = os.path.join(os.path.dirname(HERE), "refs", name)
                if os.path.isfile(root_ref):
                    full = root_ref
                else:
                    root_f = os.path.join(os.path.dirname(HERE), name)
                    if os.path.isfile(root_f):
                        full = root_f
            if os.path.isfile(full):
                mime = "image/png"
                nl = full.lower()
                if nl.endswith(".jpg") or nl.endswith(".jpeg"):
                    mime = "image/jpeg"
                elif nl.endswith(".webp"):
                    mime = "image/webp"
                with open(full, "rb") as f:
                    self._send(f.read(), mime)
            else:
                self._json({"error": "no such reference"}, 404)
        else:
            page = _page_path()
            if not page:
                return self._send("<h3>dash.html is missing from the vidgen folder - "
                                  "reinstall the app.</h3>", "text/html; charset=utf-8", 500)
            with open(page, encoding="utf-8") as f:
                self._send(f.read(), "text/html; charset=utf-8")

    def _do_POST(self):
        global QUEUE_RUNNER_ENABLED
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if path == "/api/restart_server":
            return self._json(restart_server())

        pid = body.get("project") or Handler.project

        try:
            if path.startswith("/api/category/"):
                return self._category(path, body)

            if path == "/api/project":
                raw_name = (body.get("name") or "").strip()
                name = sanitize_project_name(raw_name)
                if not name:
                    return self._json({"error": "Please provide a valid project name"}, 400)
                p_dir = os.path.join(PROJECTS, name)
                os.makedirs(p_dir, exist_ok=True)
                stext = body.get("script_text", "")
                if stext:
                    with open(os.path.join(p_dir, "script.txt"), "w", encoding="utf-8") as sf:
                        sf.write(stext)
                    os.makedirs(studio_api.SCRIPTS_DIR, exist_ok=True)
                    s_file = os.path.join(studio_api.SCRIPTS_DIR, f"{name}.txt")
                    with open(s_file, "w", encoding="utf-8") as sf:
                        sf.write(stext)
                Handler.project = name
                res = collect(name)
                res["project"] = name
                res["ok"] = True
                return self._json(res)

            if path == "/api/script":
                words = write_script(pid, body.get("text", ""))
                return self._json({"ok": True, "words": words})

            if path in ("/api/voice_clone", "/api/voice_clone/delete"):
                import voiceclone
                try:
                    key = _ai33_key()
                    if path.endswith("/delete"):
                        voiceclone.delete_clone(key, body.get("voice_id"))
                        _VOICES["at"] = 0.0           # the narrator list refreshes on the next poll
                        return self._json({"ok": True})
                    if not plan_limits().get("clone_voice", True):
                        return self._json({"ok": False, "error": "Voice cloning is switched off."})
                    if not body.get("consent"):
                        return self._json({"ok": False, "error": "Please confirm you have the right to clone this voice."})
                    name = voiceclone.clean_name(body.get("name"))
                    try:
                        raw = base64.b64decode(str(body.get("audio") or "").split(",", 1)[-1], validate=False)
                    except Exception:
                        raw = b""
                    sample, secs, notes = voiceclone.prepare_sample(raw, body.get("filename") or "sample")
                    voice_id = voiceclone.create_clone(key, name, sample)
                    _VOICES["at"] = 0.0               # the new clone joins the narrator list
                    return self._json({"ok": True, "voice_id": voice_id, "name": name,
                                       "seconds": secs, "notes": notes})
                except voiceclone.CloneError as exc:
                    return self._json({"ok": False, "error": str(exc)})
                except Exception as exc:
                    return self._json({"ok": False, "error": f"Voice cloning failed: {str(exc)[:160]}"})

            if path == "/api/captions":
                import captions as capstage
                return self._json({"ok": True,
                                   "settings": capstage.save_settings(body.get("settings", {}))})

            if path == "/api/settings":
                save_settings(pid, body.get("settings", {}))
                return self._json(collect(pid))

            if path == "/api/mix":
                # The preference is real even with no narration yet — a share,
                # not a count, so it survives being set before voice runs.
                # Duration turns it into a concrete `mix.json`; without one,
                # this only remembers the preference for later.
                engine = settings(pid).get("engine", "omniflash")
                # A clip cannot be generated longer than the engine returns,
                # so a slider left above that ceiling would promise seconds
                # the film can never have.
                cs = min(float(body.get("clip_secs", 10)),
                         mix.engine_seconds(engine))
                save_settings(pid, {
                    "clip_share": float(body.get("clip_share", 40)),
                    "clip_secs": cs,
                    "still_secs": float(body.get("still_secs", 2.3)),
                    "placement": body.get("placement", "front"),
                    "manual": body.get("manual", [])})
                state = read_json(sub(pid, "state.json"), {}) or {}
                duration = state.get("duration")
                if not duration:
                    return self._json({"resolved": False,
                                       "note": "prefs saved — duration abhi "
                                               "nahi hai, voice ke baad lock "
                                               "hoga"})
                s = settings(pid)
                clips_n = round(mix.most_clips(duration, s["clip_secs"]) *
                                s["clip_share"] / 100)
                data = mix.save(pid, duration, clips_n, s["clip_secs"],
                                s["still_secs"], s.get("placement", "front"),
                                s.get("manual"))
                data["resolved"] = True
                return self._json(data)


            if path == "/api/custom_style":
                if not pid or not os.path.isdir(folder(pid)):
                    return self._json({"error": "open a project first"}, 400)
                on = bool(body.get("enabled"))
                text = (body.get("text") or "").strip()
                if on and len(text) < 12:
                    return self._json({"error": "Describe the style in a "
                                                "sentence or two first."}, 400)
                look = (apply_custom_style(pid, text) if on
                        else clear_custom_style(pid, body.get("mode") or "2d"))
                return self._json({"ok": True, "custom": bool(look.get("custom")),
                                   "style": look.get("style", ""),
                                   **collect(pid)})

            if path == "/api/look":
                apply_look(pid, body.get("mode", "2d"))
                return self._json(collect(pid))

            if path == "/api/ref":
                ref = (save_ref(body["name"], body["data"]) if body.get("data")
                       else os.path.join(REFS, os.path.basename(body["name"])))
                if pid and os.path.isdir(folder(pid)):
                    try:
                        point_look_at(pid, ref)
                    except Exception:
                        pass
                safe_name = os.path.basename(ref)
                return self._json({"ok": True, "ref": safe_name, "filename": safe_name,
                                   "refs": studio_api.get_available_refs()})

            if path == "/api/run":
                stage = body.get("stage")
                argv = command(pid, stage, body)
                ok, note = JOB.start(stage, argv)
                return self._json({"ok": ok, "note": note,
                                   "cmd": " ".join(argv[1:])},
                                  200 if ok else 409)

            if path == "/api/select_project":
                name = (body.get("project") or "").strip()
                if name and os.path.isdir(folder(name)):
                    Handler.project = name
                return self._json(collect(Handler.project))

            if path == "/api/queue/clear":
                # Queued, not started. The film lands as an ordinary project,
                # which is what lets the sliders and the strip below this panel
                # go on being the only place the mix is chosen.
                title = (body.get("title") or "").strip()
                script = body.get("script") or ""
                if not title or not script.strip():
                    return self._json({"error": "title aur script dono chahiye"},
                                      400)
                over = {k: v for k, v in (
                    ("voice", body.get("voice")), ("look", body.get("look")),
                    ("ref", body.get("ref")), ("profile", body.get("profile")))
                    if v}
                if body.get("clip_share") is not None:
                    over["clip_share"] = float(body["clip_share"])
                if body.get("placement"):
                    over["placement"] = body["placement"]
                job = batch.add(title, script, **over)
                Handler.project = job["id"]      # so the strip below is its own
                return self._json({"ok": True, "id": job["id"],
                                   "profile": job["profile"],
                                   "jobs": batch.queue_rows()})

            if path == "/api/queue/ok":
                # The one thing this page may do to the queue: say yes to the
                # spend it is parked on. The count comes from the same
                # `mix.json` the strip above draws, so what is approved here is
                # exactly what was arranged there.
                job = batch.load_job((body.get("id") or "").strip())
                if not job:
                    return self._json({"error": "queue me nahi hai"}, 404)
                job["clips_ok"] = True
                batch.save_job(job)
                return self._json({"ok": True, "jobs": batch.queue_rows()})

            if path == "/api/update/download":
                return self._json(updater.start_download())

            if path == "/api/update/install":
                return self._json(updater.install())

            if path == "/api/update/token":
                return self._json(updater.save_token((body or {}).get("token", "")))

            if path == "/api/tts/key":
                b = body or {}
                return self._json(tts.save_key(b.get("provider", ""),
                                               b.get("key", ""),
                                               b.get("region", "")))

            if path == "/api/tts/voiceover-provider":
                b = body or {}
                return self._json(tts.set_voiceover_provider(
                    b.get("provider", "")))

            if path == "/api/tts/generate":
                b = body or {}
                try:
                    return self._json(tts.generate(b.get("provider", ""),
                                                   b.get("voice", ""),
                                                   b.get("text", ""),
                                                   b.get("language", "")))
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)[:300]})

            if path == "/api/editor/save":
                res = save_editor_data(pid, body)
                return self._json(res)

            if path == "/api/editor/upload_image":
                row_n = int(body.get("n", 1))
                data_url = body.get("data", "")
                saved_path = save_uploaded_shot_image(pid, row_n, data_url)
                return self._json({"ok": True, "path": saved_path, "image_url": f"/image/{pid}/{row_n}?t={int(time.time())}"})

            if path == "/api/profiles/create":
                name = body.get("name", "").strip()
                ok, note = studio_api.create_browser_profile(name)
                return self._json({"ok": ok, "note": note, "profiles": studio_api.get_browser_profiles()}, 200 if ok else 400)

            if path == "/api/profiles/open":
                name = body.get("name", "").strip()
                ok, note = studio_api.open_browser_profile(name)
                return self._json({"ok": ok, "note": note}, 200 if ok else 400)

            if path == "/api/profiles/open_all":
                ok, note = studio_api.open_all_browser_profiles()
                return self._json({"ok": ok, "note": note}, 200 if ok else 400)

            if path == "/api/editor/regenerate_image":
                row_n = int(body.get("n", 1))
                reason = body.get("reason", "").strip()
                profile = body.get("profile")
                res = regenerate_single_shot_image(pid, row_n, reason, profile=profile)
                return self._json(res)

            if path == "/api/editor/render":
                argv = PY + ["render", pid, "--jobs", "8"]
                ok, note = JOB.start("render", argv)
                return self._json({"ok": ok, "note": note, "cmd": " ".join(argv[1:])}, 200 if ok else 409)

            if path == "/api/start_production":
                script_path = body.get("script_path", "")
                script_text = body.get("script_text", "")
                # A pasted script with no name given starts a fresh project —
                # falling back to Handler.project (the last-selected one) here
                # used to silently overwrite whatever was open instead.
                raw_name = (body.get("project") or "").strip()
                if not raw_name and not script_text:
                    raw_name = (Handler.project or "").strip()
                proj_name = sanitize_project_name(raw_name)
                ref_path = body.get("ref", "")
                profile = body.get("profile", "acc1")

                # A film that is already finished is never redone silently:
                # make_video would only say "already complete" and the buyer,
                # who changed the category or the length, saw nothing happen.
                # The page asks; on yes the same script becomes a new project.
                # What this launch asks for, kept with the project (launch.json)
                # so the next launch can tell "the same film again" from "a new
                # look for the same script".
                try:
                    _tgt = round(float(body.get("target_seconds") or 0))
                except (TypeError, ValueError):
                    _tgt = 0
                launch_now = {"look": (body.get("look") or body.get("look_mode") or "").strip(),
                              "voice": (body.get("voice") or "").strip(),
                              "ref": os.path.basename(ref_path or ""),
                              "aspect": body.get("aspect") or "16:9", "target": _tgt}
                new_from = None
                done_film = os.path.join(PROJECTS, proj_name, "final.mp4") if proj_name else ""
                if done_film and os.path.isfile(done_film) and os.path.getsize(done_film) > 1024 * 1024:
                    before = read_json(os.path.join(PROJECTS, proj_name, "launch.json"), {}) or {}
                    changed = [k for k in launch_now if before and before.get(k) != launch_now[k]]
                    if not body.get("new_video") and not changed:
                        # The very same film again (or a film from before
                        # launch.json existed): ask, never redo silently.
                        return self._json({"ok": False, "done_before": True, "project": proj_name,
                                           "same": bool(before),
                                           "error": "this video is already made"})
                    new_from = proj_name   # a new style/voice/length: just make it
                    if not script_text and not script_path:
                        script_path = os.path.join(PROJECTS, proj_name, "script.txt")
                    base = re.sub(r"\s*\(\d+\)$", "", proj_name) or proj_name
                    n = 2
                    while os.path.exists(os.path.join(PROJECTS, "%s (%d)" % (base, n))):
                        n += 1
                    proj_name = "%s (%d)" % (base, n)

                if script_text and not script_path:
                    os.makedirs(studio_api.SCRIPTS_DIR, exist_ok=True)
                    if not proj_name:
                        proj_name = auto_project_name(script_text)
                    script_path = os.path.join(studio_api.SCRIPTS_DIR, f"{proj_name}.txt")
                    with open(script_path, "w", encoding="utf-8") as sf:
                        sf.write(script_text)
                
                # Check if project's own script.txt exists on disk
                if (not script_path or not os.path.isfile(script_path)) and proj_name:
                    p_script = os.path.join(PROJECTS, proj_name, "script.txt")
                    if os.path.isfile(p_script):
                        script_path = p_script

                if not script_path or not os.path.isfile(script_path):
                    return self._json({"error": f"Script file not found: {script_path or 'No script provided'}"}, 400)
                
                if proj_name:
                    p_dir = os.path.join(PROJECTS, proj_name)
                    os.makedirs(p_dir, exist_ok=True)
                    dst_script = os.path.join(p_dir, "script.txt")
                    if os.path.abspath(script_path) != os.path.abspath(dst_script):
                        with open(script_path, "r", encoding="utf-8") as sf:
                            with open(dst_script, "w", encoding="utf-8") as df:
                                df.write(sf.read())

                argv = [sys.executable, env.script("make_video.py"), script_path, "--profile", profile]
                if proj_name:
                    argv.extend(["--name", proj_name])
                if ref_path:
                    argv.extend(["--ref", ref_path])
                voice = body.get("voice", "meri eng")
                if voice:
                    argv.extend(["--voice", voice])
                look = body.get("look") or body.get("look_mode")
                if look:
                    argv.extend(["--look", look])
                
                clips_val = body.get("clips", 0)
                try:
                    clips_cnt = int(clips_val) if plan_limits().get("clips") else 0
                except Exception:
                    clips_cnt = 0
                placement_val = body.get("placement", "front") or "front"
                aspect_val = body.get("aspect") or (settings(proj_name).get("aspect") if proj_name else "16:9") or "16:9"
                argv.extend(["--clips", str(clips_cnt), "--placement", placement_val, "--aspect", aspect_val])
                if body.get("target_seconds"):
                    try:
                        with open(argv[2], encoding="utf-8", errors="ignore") as sf:
                            _text = sf.read()
                    except (OSError, IndexError):
                        _text = body.get("script_text") or body.get("script") or ""
                    _speed, _target = pace_for(_text, voice, body.get("target_seconds"))
                    if _target:
                        argv.extend(["--speed", str(_speed), "--target", str(_target)])
                
                ok, note = JOB.start("production", argv, cwd=ROOT_DIR)
                if ok and proj_name:
                    Handler.project = proj_name
                    try:
                        os.makedirs(os.path.join(PROJECTS, proj_name), exist_ok=True)
                        with open(os.path.join(PROJECTS, proj_name, "launch.json"), "w", encoding="utf-8") as f:
                            json.dump(launch_now, f)
                    except OSError:
                        pass
                return self._json({"ok": ok, "note": note, "cmd": " ".join(argv), "project": proj_name,
                                   "new_from": new_from}, 200 if ok else 409)

            if path == "/api/generate_voice":
                voice = body.get("voice", "meri eng")
                script_path = body.get("script_path", "")
                script_text = body.get("script_text", "")
                raw_name = (body.get("project") or Handler.project or "").strip()
                proj_name = sanitize_project_name(raw_name)
                
                if not proj_name:
                    return self._json({"error": "Please enter a project name first"}, 400)

                if script_text and not script_path:
                    os.makedirs(studio_api.SCRIPTS_DIR, exist_ok=True)
                    script_path = os.path.join(studio_api.SCRIPTS_DIR, f"{proj_name}.txt")
                    with open(script_path, "w", encoding="utf-8") as sf:
                        sf.write(script_text)

                if (not script_path or not os.path.isfile(script_path)) and proj_name:
                    p_script = os.path.join(PROJECTS, proj_name, "script.txt")
                    if os.path.isfile(p_script):
                        script_path = p_script

                if not script_path or not os.path.isfile(script_path):
                    return self._json({"error": f"Script file not found: {script_path}"}, 400)
                
                p_dir = os.path.join(PROJECTS, proj_name)
                os.makedirs(p_dir, exist_ok=True)
                Handler.project = proj_name
                dst_script = os.path.join(p_dir, "script.txt")
                if os.path.abspath(script_path) != os.path.abspath(dst_script):
                    with open(script_path, "r", encoding="utf-8") as sf:
                        with open(dst_script, "w", encoding="utf-8") as df:
                            df.write(sf.read())

                argv = [sys.executable, env.script("vidgen/pipeline.py"), "voice", proj_name, "--voice", voice, "--force"]
                ok, note = JOB.start("voice", argv, cwd=HERE)
                return self._json({"ok": ok, "note": note, "cmd": " ".join(argv), "project": proj_name}, 200 if ok else 409)

            if path == "/api/generate_images":
                proj_name = (body.get("project") or Handler.project or "").strip()
                profile = body.get("profile", "acc4")
                aspect = body.get("aspect") or settings(proj_name).get("aspect", "16:9")
                if body.get("force_switch") and JOB.running:
                    JOB.stop()
                    time.sleep(0.6)
                if not proj_name or not os.path.isdir(folder(proj_name)):
                    return self._json({"error": "Select a project first"}, 400)
                argv = [sys.executable, env.script("vidgen/pipeline.py"), "images", proj_name, "--profile", profile, "--aspect", aspect, "--resume"]
                ok, note = JOB.start("images", argv, cwd=HERE)
                return self._json({"ok": ok, "note": note, "cmd": " ".join(argv)}, 200 if ok else 409)

            if path == "/api/render_video":
                proj_name = (body.get("project") or Handler.project or "").strip()
                if not proj_name or not os.path.isdir(folder(proj_name)):
                    return self._json({"error": "Select a project first"}, 400)
                
                img_dir = os.path.join(folder(proj_name), "images")
                imgs = [f for f in os.listdir(img_dir)] if os.path.isdir(img_dir) else []
                valid_imgs = [f for f in imgs if f.lower().endswith((".png", ".jpg", ".webp"))]
                if not valid_imgs:
                    return self._json({
                        "ok": False,
                        "needs_production": True,
                        "error": f"Project '{proj_name}' has no images generated yet! Please run full production (Step 1 to 7) first.",
                        "note": "Images missing. Run full production to create voiceover and images."
                    }, 400)

                argv = [sys.executable, env.script("render_full.py"), proj_name]
                ok, note = JOB.start("render", argv, cwd=ROOT_DIR)
                return self._json({"ok": ok, "note": note, "cmd": " ".join(argv)}, 200 if ok else 409)

            if path == "/api/open_folder":
                pid = body.get("project") or Handler.project
                pub_base = studio_api.PUB_BASE
                target = pub_base
                if pid:
                    cand = os.path.join(pub_base, pid)
                    if os.path.isdir(cand):
                        target = cand
                    else:
                        local_p = folder(pid)
                        if os.path.isdir(local_p):
                            target = local_p
                if sys.platform == "win32":
                    subprocess.Popen(["explorer.exe", target])
                return self._json({"ok": True, "path": target})

            if path == "/api/play_video":
                pid = body.get("project") or Handler.project
                pub_base = studio_api.PUB_BASE
                video_file = None
                if pid:
                    cand = os.path.join(pub_base, pid, f"{pid}.mp4")
                    if os.path.isfile(cand):
                        video_file = cand
                    else:
                        local_mp4 = sub(pid, "final.mp4")
                        if os.path.isfile(local_mp4):
                            video_file = local_mp4
                if not video_file or not os.path.isfile(video_file):
                    for root, dirs, files in os.walk(pub_base):
                        for f in files:
                            if f.endswith(".mp4"):
                                video_file = os.path.join(root, f)
                                break
                        if video_file:
                            break
                if video_file and sys.platform == "win32":
                    os.startfile(video_file)
                    return self._json({"ok": True, "file": video_file})
                return self._json({"ok": False, "error": "video file not found"}, 404)



            if path == "/api/chat":
                message = (body.get("message") or "").strip()
                if not message:
                    return self._json({"error": "nothing to send"}, 400)
                profile = (body.get("profile") or "acc1").strip()
                history = body.get("history") or []
                pid = (body.get("project") or Handler.project or "").strip()
                script_context = ""
                if pid and os.path.isdir(folder(pid)):
                    s_file = sub(pid, "script.txt")
                    if os.path.isfile(s_file):
                        try:
                            script_context = open(s_file, encoding="utf-8").read()
                        except Exception:
                            pass
                try:
                    sys.path.insert(0, HERE)
                    import profile_chat
                    reply = profile_chat.ask_gemini(
                        message,
                        profile_name=profile,
                        history=history,
                        script_context=script_context
                    )
                    return self._json({"ok": True, "reply": reply})
                except Exception as exc:
                    return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

            if path == "/api/save_keys":
                ai33_key = (body.get("ai33_key") or "").strip()
                gemini_key = (body.get("gemini_key") or "").strip()

                # Check each pasted key with one free call before keeping it:
                # a mistyped or expired key used to show "Active" and fail in
                # the middle of a video. A refused key is not saved (the old
                # one stays); a check that cannot run does not block the save.
                import workspace
                key_notes, key_errors = [], []
                if ai33_key and "..." not in ai33_key:
                    st, msg = workspace.check_ai33(ai33_key)
                    if st == "invalid":
                        key_errors.append(msg + " - not saved, the previous key is kept.")
                        ai33_key = ""
                    else:
                        key_notes.append(msg)
                if gemini_key and "..." not in gemini_key:
                    st, msg = workspace.check_gemini(gemini_key)
                    if st == "invalid":
                        key_errors.append(msg + " - not saved, the previous key is kept.")
                        gemini_key = ""
                    else:
                        key_notes.append(msg)

                ai33_fp = _key_file("ai33", write=True)
                gem_fp = _key_file("gemini", write=True)
                
                if ai33_key and "..." not in ai33_key:
                    try:
                        os.makedirs(os.path.dirname(ai33_fp), exist_ok=True)
                        with open(ai33_fp, "w", encoding="utf-8") as f:
                            f.write(ai33_key)
                        # Also save directly inside workspace folder as reliable fallback
                        root_ai33 = os.path.join(ROOT_DIR, "ai_avatar_pipeline", ".ai33_key")
                        if os.path.abspath(root_ai33) != os.path.abspath(ai33_fp):
                            try:
                                os.makedirs(os.path.dirname(root_ai33), exist_ok=True)
                                with open(root_ai33, "w", encoding="utf-8") as f:
                                    f.write(ai33_key)
                            except Exception:
                                pass
                    except OSError as exc:
                        return self._json({"error": "AI33 Key save nahi ho paya: %s"
                                                    % (exc.strerror or exc)}, 500)
                    os.environ["AI33_API_KEY"] = ai33_key
                    
                if gemini_key and "..." not in gemini_key:
                    try:
                        os.makedirs(os.path.dirname(gem_fp), exist_ok=True)
                        with open(gem_fp, "w", encoding="utf-8") as f:
                            f.write(gemini_key)
                        root_gem = os.path.join(ROOT_DIR, "bulkgen", ".gemini_key")
                        if os.path.abspath(root_gem) != os.path.abspath(gem_fp):
                            try:
                                os.makedirs(os.path.dirname(root_gem), exist_ok=True)
                                with open(root_gem, "w", encoding="utf-8") as f:
                                    f.write(gemini_key)
                            except Exception:
                                pass
                    except OSError as exc:
                        return self._json({"error": "Gemini Key save nahi ho paya: %s"
                                                    % (exc.strerror or exc)}, 500)
                    os.environ["GEMINI_API_KEY"] = gemini_key
                    try:                          # keep the rotation list in step
                        _write_gemini_list([gemini_key] + [x for x in _gemini_list() if x != gemini_key])
                    except Exception:
                        pass

                # A new key can be a new account: its voices, clones and
                # favourites must show now, not after the 10-minute cache.
                _VOICES["at"] = 0.0
                _FAVS["at"] = 0.0
                saved = "; ".join(n + " and saved" for n in key_notes)
                if key_errors:
                    return self._json({"ok": False, "error": " ".join(key_errors)
                                       + ((" " + saved + ".") if saved else "")})
                return self._json({"ok": True, "note": (saved + ".") if saved
                                   else "API keys saved successfully!"})

            # ---- management: delete / clear (all recoverable via _trash) -----
            if path == "/api/delete_project":
                name = os.path.basename((body.get("project") or "").strip())
                pdir = os.path.join(PROJECTS, name)
                if not name or name in (".", "..") or not os.path.isdir(pdir):
                    return self._json({"error": "Project not found"}, 404)
                move_to_trash(pdir, os.path.join(PROJECTS, "_trash"))
                if Handler.project == name:
                    Handler.project = latest_project()
                return self._json({"ok": True, "projects": projects(),
                                   "current": Handler.project})

            if path == "/api/keys/ai33":
                import workspace
                k = (body.get("key") or "").strip()
                if not k or "..." in k:
                    return self._json({"ok": False, "error": "Paste the AI33 key first."})
                st, msg = workspace.check_ai33(k)
                if st == "invalid":
                    return self._json({"ok": False, "error": msg + " - not saved, the previous key is kept."})
                for fp in dict.fromkeys([_key_file("ai33", write=True),
                                         os.path.join(ROOT_DIR, "ai_avatar_pipeline", ".ai33_key")]):
                    try:
                        os.makedirs(os.path.dirname(fp), exist_ok=True)
                        with open(fp, "w", encoding="utf-8") as f:
                            f.write(k)
                    except OSError:
                        pass
                os.environ["AI33_API_KEY"] = k
                _VOICES["at"] = 0.0
                _FAVS["at"] = 0.0
                return self._json({"ok": True, "note": msg + " and saved."})

            if path == "/api/keys/gemini/add":
                import workspace
                k = (body.get("key") or "").strip()
                if not k or "..." in k:
                    return self._json({"ok": False, "error": "Paste a Gemini key first."})
                have = _gemini_list()
                if k in have:
                    return self._json({"ok": False, "error": "That Gemini key is already in the list."})
                st, msg = workspace.check_gemini(k)
                if st == "invalid":
                    return self._json({"ok": False, "error": msg + " - not added."})
                keys = _write_gemini_list(have + [k])
                return self._json({"ok": True, "count": len(keys),
                                   "note": f"{msg} - added as key {len(keys)} of {len(keys)}."})

            if path == "/api/keys/gemini/remove":
                have = _gemini_list()
                try:
                    i = int(body.get("index"))
                except (TypeError, ValueError):
                    i = -1
                if not 0 <= i < len(have):
                    return self._json({"ok": False, "error": "No such key."})
                keys = _write_gemini_list(have[:i] + have[i + 1:])
                return self._json({"ok": True, "count": len(keys)})

            if path == "/api/keys/remove":
                import workspace
                n = workspace.remove_keys(_all_key_paths())
                _VOICES.update(at=0.0, names=[])
                _FAVS["at"] = 0.0
                return self._json({"ok": True, "removed": n,
                                   "note": "API keys removed from this computer."})

            if path in ("/api/workspace/summary", "/api/workspace/reset"):
                import workspace
                chrome = bool(body.get("chrome"))
                videos = bool(body.get("videos"))
                where = _workspace_where()
                if path.endswith("/summary"):
                    return self._json({"ok": True, **workspace.summary(where, chrome, videos)})
                if (body.get("confirm") or "").strip().upper() != "RESET":
                    return self._json({"ok": False, "error": "Type RESET to confirm."})
                if JOB.running or any_lane_running() or CURRENT_QUEUE_ITEM_ID:
                    return self._json({"ok": False, "error": "A video is being made right now. "
                                       "Stop it (or wait until it finishes), then reset."})
                if chrome:
                    try:
                        kill_automation_browsers()
                        time.sleep(1.5)
                    except Exception:
                        pass
                res = workspace.reset_workspace(where, chrome, videos)
                Handler.project = None
                _VOICES["at"] = 0.0
                return self._json({"ok": not res["failed"], **res,
                                   "error": ("Some items could not be removed (close any open "
                                             "Chrome / video player and try again): "
                                             + "; ".join(res["failed"][:5])) if res["failed"] else ""})

            if path == "/api/clear_projects":
                trash = os.path.join(PROJECTS, "_trash")
                moved = 0
                for n in projects():           # already skips _/. folders
                    try:
                        move_to_trash(os.path.join(PROJECTS, n), trash)
                        moved += 1
                    except Exception:
                        pass
                Handler.project = None
                return self._json({"ok": True, "moved": moved, "projects": projects()})

            if path == "/api/delete_ref":
                name = os.path.basename((body.get("name") or "").strip())
                fp = os.path.join(REFS, name)
                if not os.path.isfile(fp):
                    alt = os.path.join(ROOT_DIR, name)
                    if os.path.isfile(alt):
                        fp = alt
                if not name or not os.path.isfile(fp):
                    return self._json({"error": "Reference not found"}, 404)
                move_to_trash(fp, os.path.join(REFS, "_trash"))
                return self._json({"ok": True, "refs": studio_api.get_available_refs()})

            if path == "/api/clear_queue":
                save_prod_queue([])
                return self._json({"ok": True, "queue": []})

            if path == "/api/open_profile":
                # Open one browser profile so the user can sign into their Google
                # account (the images stage needs a signed-in Chrome). Detached,
                # so the dashboard stays responsive while the window is open.
                name = (body.get("profile") or "acc1").strip()
                argv = [sys.executable, env.script("vidgen/open_profile.py"), name]
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                try:
                    subprocess.Popen(argv, cwd=HERE, creationflags=flags)
                except Exception as exc:
                    return self._json({"error": f"could not open profile: {exc}"}, 500)
                return self._json({"ok": True, "profile": name})

            if path == "/api/prod_queue/add":
                if queue_room() < 1:
                    lim = plan_limits().get("queue") or 2
                    return self._json({"ok": False, "limit": "queue", "error":
                        "The queue holds %d videos at a time. Add this one when one of them is done." % lim}, 400)
                raw_name = (body.get("project") or "").strip()
                script_path = (body.get("script_path") or "").strip()
                script_text = (body.get("script_text") or "").strip()
                
                if not raw_name and script_path:
                    raw_name = os.path.splitext(os.path.basename(script_path))[0]
                auto_named = False
                if not raw_name and script_text:
                    # No name typed: take a short piece of the script's own
                    # title so the queue makes its own project rather than
                    # refusing, or landing on the one that is already open.
                    raw_name = auto_project_name(script_text)
                    auto_named = True

                name = sanitize_project_name(raw_name)
                if not name:
                    return self._json({"error": "Please provide a valid project name"}, 400)
                if not script_path and not script_text:
                    return self._json({"error": "Please provide script text or select a script"}, 400)

                if auto_named:
                    # Two queued scripts opening with the same line would
                    # share one project; the folder check alone cannot see a
                    # name another queued job has taken but not started.
                    taken = {str(it.get("project")) for it in load_prod_queue()
                             if it.get("status") not in ("completed", "failed")}
                    base, n = name, 2
                    while name in taken or os.path.isdir(os.path.join(PROJECTS, name)):
                        name = f"{base} ({n})"
                        n += 1
                # The project exists from this moment - in the list, with its
                # script - so nobody has to create it by hand before it runs.
                try:
                    os.makedirs(os.path.join(PROJECTS, name), exist_ok=True)
                    if script_text:
                        with open(os.path.join(PROJECTS, name, "script.txt"), "w",
                                  encoding="utf-8") as sf:
                            sf.write(script_text + "\n")
                except Exception as exc:
                    return self._json({"error": f"Could not create project '{name}': {exc}"}, 500)
                
                clips_val = body.get("clips", 0)
                try:
                    clips_cnt = int(clips_val) if plan_limits().get("clips") else 0
                except Exception:
                    clips_cnt = 0
                placement_val = body.get("placement", "front") or "front"
                
                q = load_prod_queue()
                item_id = f"q_{int(time.time()*1000)}"
                item = {
                    "id": item_id,
                    "project": name,
                    "script_path": script_path,
                    "script_text": script_text,
                    # "" is a choice - no character reference for this film.
                    "ref": body.get("ref", "") or "",
                    "look": (body.get("look") or "").strip(),
                    # The length the buyer chose for the narration, in seconds
                    # (0 = the voice's own pace).
                    "target_seconds": float(body.get("target_seconds") or 0),
                    "profile": body.get("profile", "acc1"),
                    "voice": body.get("voice", "elevenlabs_XNJDyPyJ8r4J7de0WJwB"),
                    "clips": clips_cnt,
                    "placement": placement_val,
                    "status": "queued",
                    "added_at": time.time(),
                    "started_at": None,
                    "finished_at": None,
                    "error": None
                }
                q.append(item)
                save_prod_queue(q)
                return self._json({"ok": True, "item": item, "queue": q})

            if path == "/api/prod_queue/bulk_add":
                scripts = body.get("scripts", [])
                if not scripts:
                    return self._json({"error": "No scripts selected"}, 400)
                room = queue_room()
                if len(scripts) > room:
                    lim = plan_limits().get("queue") or 2
                    return self._json({"ok": False, "limit": "queue", "error":
                        "The queue holds %d videos at a time; there is room for %d more now."
                        % (lim, room)}, 400)
                ref = body.get("ref", "clean_stickman_ref.png")
                profile = body.get("profile", "acc1")
                voice = body.get("voice", "meri eng")
                aspect_val = body.get("aspect", "16:9")
                clips_val = body.get("clips", 0)
                try:
                    clips_cnt = int(clips_val) if plan_limits().get("clips") else 0
                except Exception:
                    clips_cnt = 0
                placement_val = body.get("placement", "front") or "front"
                
                q = load_prod_queue()
                added = []
                for s_path in scripts:
                    base = os.path.splitext(os.path.basename(s_path))[0]
                    proj_name = sanitize_project_name(base)
                    item_id = f"q_{int(time.time()*1000)}_{len(added)}"
                    item = {
                        "id": item_id,
                        "project": proj_name,
                        "script_path": s_path,
                        "script_text": "",
                        "ref": ref,
                        "profile": profile,
                        "voice": voice,
                        "clips": clips_cnt,
                        "placement": placement_val,
                        "aspect": aspect_val,
                        "status": "queued",
                        "added_at": time.time(),
                        "started_at": None,
                        "finished_at": None,
                        "error": None
                    }
                    q.append(item)
                    added.append(item)
                save_prod_queue(q)
                return self._json({"ok": True, "count": len(added), "queue": q})


            if path == "/api/prod_queue/remove":
                target_id = body.get("id")
                q = load_prod_queue()
                q = [x for x in q if x["id"] != target_id]
                save_prod_queue(q)
                return self._json({"ok": True, "queue": q})

            if path == "/api/prod_queue/move":
                target_id = body.get("id")
                direction = body.get("dir", "up")
                q = load_prod_queue()
                idx = next((i for i, x in enumerate(q) if x["id"] == target_id), -1)
                if idx >= 0:
                    if direction == "up" and idx > 0:
                        q[idx], q[idx - 1] = q[idx - 1], q[idx]
                    elif direction == "down" and idx < len(q) - 1:
                        q[idx], q[idx + 1] = q[idx + 1], q[idx]
                    save_prod_queue(q)
                return self._json({"ok": True, "queue": q})

            if path == "/api/voices/favorite":
                try:
                    voice_favorite(body.get("provider") or "", body.get("id") or "",
                                   body.get("voice") or {}, bool(body.get("on")))
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)[:200]})

            if path == "/api/prod_queue/parallel":
                try:
                    return self._json({"ok": True, "parallel": set_parallel(body.get("n", 1))})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)[:200]})

            if path == "/api/prod_queue/lane_stop":
                try:
                    i = int(body.get("lane", 0)) - 1
                    lane = LANES[i]
                except Exception:
                    return self._json({"ok": False, "error": "which lane?"})
                if not lane.running:
                    return self._json({"ok": False, "error": "that lane is idle"})
                return self._json({"ok": lane.stop()})

            if path == "/api/prod_queue/toggle":
                is_running = body.get("running")
                if is_running is not None:
                    QUEUE_RUNNER_ENABLED = bool(is_running)
                else:
                    QUEUE_RUNNER_ENABLED = not QUEUE_RUNNER_ENABLED
                return self._json({"ok": True, "queue_running": QUEUE_RUNNER_ENABLED})

            if path == "/api/prod_queue/retry":
                target_id = body.get("id")
                q = load_prod_queue()
                for x in q:
                    if x["id"] == target_id:
                        x["status"] = "queued"
                        x["error"] = None
                        x["started_at"] = None
                        x["finished_at"] = None
                save_prod_queue(q)
                return self._json({"ok": True, "queue": q})

            if path == "/api/prod_queue/clear_completed":
                q = load_prod_queue()
                q = [x for x in q if x.get("status") != "completed"]
                save_prod_queue(q)
                return self._json({"ok": True, "queue": q})

            if path == "/api/stop_production" or path == "/api/stop":
                QUEUE_RUNNER_ENABLED = False
                res = JOB.stop()
                kill_automation_browsers()
                return self._json({"ok": True, "stopped": True, "queue_running": False})

        except Exception as exc:                       # a bad click, not a crash
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        self._json({"error": "no such route"}, 404)


def serve(port, tries=12):
    """Bind the first free port at or after `port`.

    Something else on this machine already sits on 5153, and a launcher that
    dies on "address in use" is a launcher nobody double-clicks twice.
    Threaded on purpose too: a browser opens speculative connections that send
    nothing, and a single-threaded server blocks the whole accept loop on the
    first one — the page then hangs on "loading" forever.
    """
    for offset in range(tries):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port + offset), Handler)
        except OSError:
            continue
        srv.daemon_threads = True
        return srv, port + offset
    raise SystemExit(f"no free port between {port} and {port + tries - 1}")


_STARTED = time.time()          # lets the page notice a restarted server


def version_info():
    """The app's version (the header shows it; the launcher checks it)."""
    info = {"version": "?", "started": _STARTED}
    try:
        import importlib
        import version as _v
        importlib.reload(_v)
        info["version"] = _v.APP_VERSION
    except Exception:
        pass
    return info

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    ap.add_argument("--port", type=int, default=5154)
    ap.add_argument("--open", action="store_true",
                    help="open the page in a browser once it is listening")
    args = ap.parse_args()
    Handler.project = args.project or latest_project() or (projects()[0] if projects() else None)
    srv, port = serve(args.port)
    url = f"http://localhost:{port}"
    print(f"[dash] {url} — {Handler.project or 'no project yet'}", flush=True)
    print("[dash] band karne ke liye is window me Ctrl+C", flush=True)
    
    # Start production queue background worker
    threading.Thread(target=queue_worker_loop, daemon=True).start()

    if args.open:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[dash] bye", flush=True)


if __name__ == "__main__":
    main()

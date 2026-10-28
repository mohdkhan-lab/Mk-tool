"""
Shared plumbing for the bulk-image pipeline.

Every stage reads and writes the same project folder, so a run can be stopped
anywhere and picked up from what is already on disk. `state.json` records only
what the files themselves cannot say — which voice was used, which URL the
images came from, how many prompts the page was asked for.
"""
import json
import os
import re
import shutil
import subprocess
import sys

# The console here is cp1252, and half these messages carry an em-dash or an
# arrow. Without this a finished stage dies on its own success line, after the
# work is already on disk — unprintable characters become "?" instead.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AI_PIPE = os.path.join(REPO, "ai_avatar_pipeline")
PROJECTS = (os.environ.get("STICKFILM_PROJECTS")
            or (os.path.join(REPO, "vidgen", "projects")
                if os.path.isdir(os.path.join(REPO, "vidgen", "projects"))
                else os.path.join(HERE, "projects")))

if "STICKFILM_PROFILES" not in os.environ or not os.path.exists(os.environ.get("STICKFILM_PROFILES", "")):
    os.environ["STICKFILM_PROFILES"] = os.path.join(AI_PIPE, "browser_profiles")

for p_dir in [
    r"C:\Users\sting\AppData\Local\ffmpeg\bin",
    os.path.join(REPO, "tools"),
    os.path.dirname(sys.executable),
    r"D:\AI Autometa\bin",
]:
    if os.path.isdir(p_dir) and p_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p_dir + os.pathsep + os.environ.get("PATH", "")

def _install_root():
    """The folder the app was installed to - where tools/, fonts, looks and
    reference sheets ship. The same as REPO in a developer copy; installed,
    this code runs from the user's folder and the app sits in Program Files,
    which the launcher puts on PYTHONPATH."""
    for d in [REPO] + [x for x in sys.path if x]:
        if os.path.isfile(os.path.join(d, "vidgen", "dash.py")):
            return d
    return REPO


INSTALL_ROOT = _install_root()


def shipped(*parts):
    """A file that ships with the app: beside this code if it is there, else
    in the install folder. Returns the install-folder path if neither has it,
    so an error names the place a person would look."""
    here = os.path.join(REPO, *parts)
    if os.path.exists(here):
        return here
    return os.path.join(INSTALL_ROOT, *parts)


# A copy of ffmpeg carried next to the code, for a machine that has none. Put
# first so the bundled one wins, and done here rather than in a batch file so
# it is true however the pipeline is started — the window, a command, or an
# import from somewhere else.
TOOLS = shipped("tools")
if os.path.isdir(TOOLS) and TOOLS not in os.environ.get("PATH", ""):
    os.environ["PATH"] = TOOLS + os.pathsep + os.environ.get("PATH", "")

# The share link for the user's own "Bulk Image Generator" Gemini app. Kept
# here rather than in the browser driver so a new copy of the app is a
# one-line change and every stage reports the same URL.
BULK_APP_URL = "https://share.gemini.google/cTnZQ3bpoER7"


def log(stage, msg):
    print(f"[{stage}] {msg}", flush=True)


def project_dir(pid, create=True):
    d = os.path.join(PROJECTS, pid)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def p(pid, *parts):
    """A path inside the project folder."""
    return os.path.join(project_dir(pid), *parts)


def load_state(pid):
    path = p(pid, "state.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"id": pid}


def save_state(pid, **fields):
    """Merge fields into state.json — stages never overwrite each other's keys."""
    state = load_state(pid)
    state.update(fields)
    with open(p(pid, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    return state


def fmt(t):
    """00:02.41 — mm:ss.cc, the format the timestamp table and the file names use."""
    m = int(t // 60)
    s = t % 60
    return f"{m:02d}:{s:05.2f}"


def slug_ts(t):
    """00-02.41 — same instant, safe inside a file name."""
    return fmt(t).replace(":", "-")


def have(tool):
    return shutil.which(tool) is not None


# Every ffmpeg/ffprobe call here runs from a no-console (pythonw) parent — the
# dashboard app. Without this flag Windows still gives each child its own
# console, which flashes open and shut; with `jobs` of them firing in parallel
# during render, that is a burst of windows popping up and disappearing.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run(cmd, **kw):
    """Run a command, raising with its stderr attached rather than a bare code."""
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW, **kw)
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{cmd[0]} failed ({res.returncode}):\n  " + "\n  ".join(tail))
    return res


def probe(path):
    """Length of an audio or video file in seconds, 0.0 if it cannot be read."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, creationflags=_NO_WINDOW,
    )
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def natural_key(name):
    """Sort image_2.png before image_10.png, which a plain sort does not."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", os.path.basename(name))]


def key_files(folder, name):
    """Every place a saved API key may be, most likely first.

    The API Keys screen saves under the user's own folder once the app is
    installed, because Program Files cannot be written. This code runs from
    the "code" folder inside it, one level away, so looking only beside it found
    nothing and the voice stage stopped with "no ai33 key" while the screen
    showed the key as saved.
    """
    out = []
    try:
        import userdata
        out.append(os.path.join(userdata.base(), folder, name))
    except Exception:
        pass
    home = os.environ.get("STICKFILM_HOME")
    if home:
        out.append(os.path.join(home, folder, name))
    out.append(os.path.join(REPO, folder, name))
    seen = []
    for path in out:
        if path not in seen:
            seen.append(path)
    return seen


def _saved_key(folder, name):
    for path in key_files(folder, name):
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    key = f.read().strip()
                if key:
                    return key
        except OSError:
            pass
    return None


def ai33_key(explicit=None):
    """
    The ai33 key, from the flag, the environment, or the file the API Keys
    screen saved it to.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("AI33_API_KEY")
    if env:
        return env.strip()
    return _saved_key("ai_avatar_pipeline", ".ai33_key")


def gemini_key():
    env = os.environ.get("GEMINI_API_KEY")
    if env:
        return env.strip()
    return _saved_key("bulkgen", ".gemini_key")


def gemini_keys():
    """Every saved Gemini key, in the order they are used (the API Keys window
    keeps a list so the prompt stage can move to the next key when one hits
    its quota). Falls back to the single key when no list exists."""
    keys = []
    for path in key_files("bulkgen", ".gemini_keys"):
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    keys = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                if keys:
                    break
        except OSError:
            pass
    if not keys:
        one = gemini_key()
        keys = [one] if one else []
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# Hand the keys to everything started from here - make_voiceover reads its
# default from the environment, not from a file.
for _var, _get in (("AI33_API_KEY", ai33_key), ("GEMINI_API_KEY", gemini_key)):
    if not os.environ.get(_var):
        try:
            _k = _get()
        except Exception:
            _k = None
        if _k:
            os.environ[_var] = _k

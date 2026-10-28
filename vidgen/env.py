"""
Where this pipeline lives, and what a clip costs.

The image pipeline's modules are reused as they are — the voice stage, the
whisper timings, the prompt writer, the publisher. They all reach for the
project folder through `bulkgen/common.py`, so the one thing that has to change
is where that folder is. Importing this module does it, and every stage imports
it first.

Deliberately *not* called `common`: the modules being borrowed do
`from common import …` themselves, and a second file by that name on the path
shadows the one they mean.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# The install folder: the code, ffmpeg and the bundled tools all live here.
REPO = os.environ.get("STICKFILM_INSTALL") or os.path.dirname(HERE)
if os.path.join(REPO, "vidgen") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "vidgen"))
BULKGEN = os.path.join(REPO, "bulkgen")
AI_PIPE = os.path.join(REPO, "ai_avatar_pipeline")

for path in (BULKGEN, AI_PIPE):
    if path not in sys.path:
        sys.path.insert(0, path)


def script(rel):
    """Full path of a pipeline script inside the install folder."""
    child_env_path()
    return os.path.join(REPO, *rel.split("/"))


def child_env_path():
    """Put the code folders on PYTHONPATH for every process this one starts."""
    dirs = []
    for sub in ("vidgen", "bulkgen", "ai_avatar_pipeline", ""):
        d = os.path.join(REPO, sub) if sub else REPO
        if os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    old = [d for d in os.environ.get("PYTHONPATH", "").split(os.pathsep)
           if d and d not in dirs]
    os.environ["PYTHONPATH"] = os.pathsep.join(dirs + old)


child_env_path()

# Where this copy may write. Installed under Program Files the app folder is
# read-only, so userdata sends everything to the user's own folder instead.
try:
    sys.path.insert(0, HERE)
    import userdata as _userdata
    _UD = _userdata.setup()
except Exception:
    _UD = None

# Hand the user's folders to every process started from here, so every stage
# reads and writes the same data folder.
for _key, _var in (("projects", "STICKFILM_PROJECTS"), ("scripts", "STICKFILM_SCRIPTS"),
                   ("refs", "STICKFILM_REFS"), ("looks", "STICKFILM_LOOKS")):
    if _UD and _UD.get(_key) and not os.environ.get(_var):
        os.environ[_var] = _UD[_key]

if "STICKFILM_PROFILES" not in os.environ or not os.path.exists(os.environ.get("STICKFILM_PROFILES", "")):
    os.environ["STICKFILM_PROFILES"] = (
        (_UD or {}).get("profiles") or os.path.join(AI_PIPE, "browser_profiles"))

for p_dir in [
    os.path.join(REPO, "tools"),
    os.path.dirname(sys.executable),
]:
    if os.path.isdir(p_dir) and p_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p_dir + os.pathsep + os.environ.get("PATH", "")

try:
    import common                                              # noqa: E402
except ImportError:
    common = None

# `p()` reads this at call time, so reassigning it moves every project path for
# the rest of the process. A film made of clips never lands in the image
# pipeline's folder, and the two can be worked on side by side.
PROJECTS = (os.environ.get("STICKFILM_PROJECTS")
            or (_UD or {}).get("projects")
            or os.path.join(HERE, "projects"))
if common is not None:
    common.PROJECTS = PROJECTS
try:
    os.makedirs(PROJECTS, exist_ok=True)
except OSError:
    # An installed copy cannot write beside itself. userdata has already put
    # the real folder under the user's own directory; failing here would take
    # the whole app down on its first import.
    pass

# Clips are bought, not rendered. OmniFlows' Growth plan carries 400 video
# credits and OmniFlash Fast spends 3 a clip, so a ten-minute film costs about
# 260 of them — one film per top-up, near enough. Every stage that spends
# prints the clip count and the credits before it submits anything.
# Tuned against a real ten-minute narration: at these numbers it cuts into 87
# rows averaging 7.9s, with a single 12.2s row that no comma could break. The
# ceiling is 10s because that is exactly what OmniFlash Fast returns — a row
# under it is covered by its clip outright, with nothing to hold or loop.
CLIP_SECONDS = 7.0          # what one clip should cover
CLIP_MIN = 4.0              # never leave a row shorter than this
CLIP_MAX = 10.0             # a clip is 10s, so a row past this needs padding
CREDITS_PER_CLIP = 3

"""Where the app is allowed to write.

Until now every project, profile and render landed next to the code. That works
from a folder you unzipped yourself and fails the moment the app is installed:
Program Files is read-only, so creating the six browser profiles raises
PermissionError and the profiles page comes up empty.

The rule here is deliberately boring:

  * `STICKFILM_HOME` wins, always - that is how a test pins a throwaway folder.
  * If the install folder is writable, keep using it. An unzipped copy and a
    developer checkout behave exactly as before, and nobody's existing projects
    move.
  * Otherwise write under `%LOCALAPPDATA%\\StickFilm Studio`.

Everything the user creates lives under one base, so it survives an uninstall
and a reinstall, and it can be backed up by copying a single folder.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL_ROOT = os.path.dirname(HERE)

# Named after the folder this copy was installed into, so a side-by-side build
# - "StickFilm Studio TEST" next to "StickFilm Studio" - keeps its own projects,
# profiles and logins instead of quietly sharing the live ones.
APP_NAME = os.path.basename(INSTALL_ROOT.rstrip(os.sep)) or "StickFilm Studio"
if APP_NAME.lower() in ("stickfilm", "app", "src"):
    APP_NAME = "StickFilm Studio"


def _writable(path):
    probe = os.path.join(path, ".write_test")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _under_program_files(path):
    here = os.path.normcase(os.path.abspath(path))
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        pf = os.environ.get(var)
        if pf and here.startswith(os.path.normcase(os.path.abspath(pf)) + os.sep):
            return True
    return False


def installed():
    """True for a copy the installer put down. From 2.2.6 that is a per-user
    folder the app can write to - so it can update itself without asking for
    admin - and writability alone would then read it as an unzipped copy."""
    return os.path.isfile(os.path.join(INSTALL_ROOT, ".installed"))


def base():
    """The one folder everything the user makes lives under."""
    pinned = os.environ.get("STICKFILM_HOME")
    if pinned:
        return pinned

    # An unzipped copy stays self-contained, so existing projects do not move.
    # Never a copy under Program Files, even when this process could write
    # there: the installer's "Open now" runs the app elevated, and that first
    # run put the buyer's keys and projects into Program Files, where every
    # later, normal launch could read them but never save.
    # Never an installed copy either: its data stays in LocalAppData, where
    # every earlier version kept it, so projects and logins carry over and an
    # update that replaces the install folder cannot touch them.
    if (not installed() and not _under_program_files(INSTALL_ROOT)
            and _writable(os.path.join(INSTALL_ROOT, "vidgen"))):
        return INSTALL_ROOT

    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    candidate = os.path.join(local, APP_NAME)
    if _writable(candidate):
        return candidate
    # A previous elevated install can leave the LocalAppData folder owned by
    # another account. Keep the app usable for the current user instead of
    # failing later with WinError 5 while creating a project.
    fallback = os.path.join(os.path.expanduser("~"), APP_NAME)
    _writable(fallback)
    return fallback


def paths():
    """Resolve every writable location, creating what is missing."""
    b = base()
    p = {
        "base": b,
        "projects": os.path.join(b, "vidgen", "projects"),
        "profiles": os.path.join(b, "ai_avatar_pipeline", "browser_profiles"),
        "refs": os.path.join(b, "vidgen", "refs"),
        "looks": os.path.join(b, "vidgen", "looks"),
        "scripts": os.path.join(b, "scripts"),
        # The finished films are the thing the buyer bought, so they go in
        # their own space under one obvious name - not inside LOCALAPPDATA,
        # which is hidden and which an uninstall might one day sweep.
        "output": os.path.join(os.path.expanduser("~"), "StickFilm"),
    }
    for key, d in p.items():
        if key == "base":
            continue
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    return p


def _seed(src, dst):
    """Copy the read-only material the app ships with into the user's folder,
    once. Reference images and looks are needed to start a film and the
    install folder may not be readable-and-writable at the same time."""
    if not os.path.isdir(src):
        return
    try:
        if not os.listdir(dst):
            for name in os.listdir(src):
                s, d = os.path.join(src, name), os.path.join(dst, name)
                if os.path.isfile(s):
                    shutil.copy2(s, d)
                elif os.path.isdir(s) and not os.path.exists(d):
                    shutil.copytree(s, d)
    except Exception:
        pass


def setup():
    """Call once, as early as possible. Publishes the locations as environment
    variables so every module resolves the same folders without importing this
    one."""
    p = paths()

    if p["base"] != INSTALL_ROOT:
        # An installed copy keeps its data in the user's folder: the built-in
        # reference pictures and looks are copied there from the install folder.
        _seed(os.path.join(INSTALL_ROOT, "vidgen", "refs"), p["refs"])
        _seed(os.path.join(INSTALL_ROOT, "vidgen", "looks"), p["looks"])

    os.environ.setdefault("STICKFILM_PROJECTS", p["projects"])
    os.environ.setdefault("STICKFILM_REFS", p["refs"])
    os.environ.setdefault("STICKFILM_SCRIPTS", p["scripts"])
    os.environ["STICKFILM_PROFILES"] = p["profiles"]
    os.environ.setdefault("STICKFILM_PUB_BASE", p["output"])
    return p


if __name__ == "__main__":
    for k, v in setup().items():
        print("%-9s %s" % (k, v))
    print()
    print("install root :", INSTALL_ROOT)
    print("writable     :", _writable(os.path.join(INSTALL_ROOT, "vidgen")))

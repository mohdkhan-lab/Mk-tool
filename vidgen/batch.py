"""
BATCH — many scripts at once, and the two places a human is still needed.

The dashboard runs one stage at a time on purpose, which is right when a person
is watching one film. It is wrong when ten scripts are waiting: `voice` is an
API call that spends nothing but time, `rows` is whisper on the CPU, `render`
is ffmpeg, and none of those three care what the others are doing. Only the
browser stages do, and even they only care about sharing a signed-in account.

So this is not "run everything at once". It is lanes:

    net             voice     an HTTP call — several at a time is fine
    cpu             rows      whisper and ffmpeg — one, or they fight
                    render    so the films finish one after another
    images:<acc>    images    one per browser profile: five films on five
                              accounts draw at the same time
    clips           clips     ONE, ever

The rule this pipeline learned the expensive way is that two stages must never
share a browser *profile* — that is how a batch lands in the wrong account. So
image runs are separated by profile rather than queued behind each other, and
every queued script is handed a profile nothing else is holding.

`clips` stays a single lane whatever happens: OmniFlows is one session for the
whole machine, held open on purpose, and there is no second one to run beside
it. A film planned with no clips at all does not queue for it — it goes
straight to render.

One stage is not run at all, because it is judgement, not work:

    scenes   one picture per row, written by hand into `scenes.txt`

That one is a *gate*: the queue walks a film up to it, parks it, and moves on
to another film. Everything else runs through to a published film without
asking — starting a run is the approval, credits included. `clips_ok` and
`publish_ok` exist so a job *can* be held, but they default to open.

    python batch.py add                 a script into the queue
    python batch.py run                 work the queue until it is empty
    python batch.py list                what is where
    python batch.py ok <id>             re-open a job that was held back
    python batch.py retry <id>          a failed job, from where it stopped

Every job is one json file under `queue/`, and every stage is the same
`pipeline.py` command the dashboard would run — so a film made by the queue and
a film made by hand are the same film, and a queue that is not running leaves
nothing behind but ordinary projects.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import env                       # redirects the project root; must come first
from env import AI_PIPE, CREDITS_PER_CLIP
import mix

PROFILES_FILE = os.path.join(AI_PIPE, "profiles.json")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.environ.get("STICKFILM_PROJECTS") or os.path.join(HERE, "projects")
LOOKS = os.environ.get("STICKFILM_LOOKS") or os.path.join(HERE, "looks")
REFS = os.environ.get("STICKFILM_REFS") or os.path.join(HERE, "refs")
QUEUE = os.path.join(HERE, "queue")
ASKS = os.path.join(QUEUE, "scenes-needed")
LOGS = os.path.join(QUEUE, "logs")
STATUS = os.path.join(QUEUE, "status.json")

PY = [sys.executable, "-u", os.path.join(HERE, "pipeline.py")]

# What a job carries when nothing is said. These are the dashboard's own
# defaults; a job written here shows up on that page reading the same way.
JOB_DEFAULTS = {
    "voice": "STIckman Voice",
    "profile": "acc3",
    "look": "2d_modern",
    "ref": "",
    "aspect": "landscape",
    "engine": "omniflash",
    "batch": 50,
    "jobs": 8,
    "clip_share": 40.0,
    "clip_secs": 10.0,
    "still_secs": 2.3,
    "placement": "spread",
    "manual": [],
    # Both gates open by default. Starting a film is the approval: it goes
    # through clips, render and publish without coming back for a yes. The
    # flags stay so a run *can* be held, but holding is not the default —
    # a film waiting on a question whose answer was always yes cost more than
    # the gate ever protected.
    "clips_ok": True,
    "publish_ok": True,
    "title": "",
    "description": "",
    "hashtags": "",
}

ORDER = ["voice", "mix", "rows", "scenes", "prompts", "images", "clips",
         "render", "publish"]

# The lanes that exist whatever is queued. Image lanes are not here because
# there is one per browser profile and the profiles are not known until jobs
# are read — see `Lanes`.
WIDTH = {"net": 3, "cpu": 1, "clips": 1}


def lane_for(job, stage):
    """Which lane a stage must hold, or None if it conflicts with nothing.

    The two browser stages are split, because they are not the same kind of
    busy. `images` is one signed-in Gemini profile driving its own window, so
    five films on five profiles is five windows that never meet — the rule was
    only ever that two stages must not share a *profile*. `clips` is one
    OmniFlows session for the whole machine, so it stays a single lane no
    matter how many films are waiting on it.
    """
    if stage == "voice":
        return "net"
    if stage in ("rows", "render"):
        return "cpu"
    if stage == "images":
        return f"images:{job.get('profile') or JOB_DEFAULTS['profile']}"
    if stage == "clips":
        return "clips"
    return None                    # prompts is formatting; publish is a copy


class Lanes:
    """A semaphore per lane, image lanes appearing as their profiles do."""

    def __init__(self, width=None):
        self._fixed = {k: threading.Semaphore(v)
                       for k, v in {**WIDTH, **(width or {})}.items()}
        self._per_profile = {}
        self._lock = threading.Lock()

    def get(self, name):
        if name in self._fixed:
            return self._fixed[name]
        with self._lock:
            if name not in self._per_profile:
                self._per_profile[name] = threading.Semaphore(1)
            return self._per_profile[name]


# ── the queue on disk ────────────────────────────────────────────────────────

_write_lock = threading.Lock()


def slug(title):
    """A folder name from a title: lowercase, words joined by underscores."""
    out = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return out[:48] or "untitled"


def job_path(pid):
    return os.path.join(QUEUE, f"{pid}.json")


def load_job(pid):
    try:
        with open(job_path(pid), encoding="utf-8") as f:
            return {**JOB_DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return None


def save_job(job):
    """A job file rewritten whole, under a lock.

    Two threads touch a job: its own, and whichever one the user talks to from
    the command line. A half-written json here would strand a film that has
    already been paid for, so the write is atomic — a temp file and a replace,
    never a truncate-then-write.
    """
    with _write_lock:
        os.makedirs(QUEUE, exist_ok=True)
        tmp = job_path(job["id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=1, ensure_ascii=False)
        os.replace(tmp, job_path(job["id"]))
    return job


def jobs():
    if not os.path.isdir(QUEUE):
        return []
    out = []
    for name in sorted(os.listdir(QUEUE)):
        if name.endswith(".json") and name != "status.json":
            job = load_job(name[:-5])
            if job:
                out.append(job)
    return sorted(out, key=lambda j: j.get("added", ""))


def note(job, state, message=""):
    job["state"] = state
    job["note"] = message
    job["seen"] = datetime.now().strftime("%H:%M:%S")
    save_job(job)
    write_status()
    print(f"[{job['id']}] {state}" + (f" - {message}" if message else ""),
          flush=True)
    return job


# ── what the project itself says is done ─────────────────────────────────────
#
# Read off the disk, never remembered in the job file. A stage run by hand from
# the shell, or from the dashboard, counts exactly as much as one the queue
# ran — which is what makes a half-finished project safe to drop into the
# queue, and the queue safe to restart.

def sub(pid, name):
    return os.path.join(PROJECTS, pid, name)


def read_json(path, fallback=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def count_dir(path):
    return len(os.listdir(path)) if os.path.isdir(path) else 0


def row_count(pid):
    rows = read_json(sub(pid, "segments.json"), []) or []
    return len(rows)


def line_count(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def planned_clips(pid):
    rows = row_count(pid)
    if not rows:
        return 0
    return sum(1 for k in mix.kinds_for(pid, rows) if k == "clip")


def duration_of(pid):
    return (read_json(sub(pid, "state.json"), {}) or {}).get("duration") or 0


def mix_settled(pid):
    """Whether a clip plan exists that was actually chosen for this narration.

    Deliberately strict about the duration. `mix.load` re-plans when the
    narration has moved under it, and its fallback is *every clip that would
    fit* — so a stale plan here is not a smaller mistake than a missing one,
    it is the same runaway number wearing an old date.
    """
    data = read_json(sub(pid, "mix.json"), None)
    if not data or not data.get("targets"):
        return False
    duration = duration_of(pid)
    return bool(duration) and abs(data.get("duration", 0) - duration) < 0.5


def done(pid, stage):
    rows = row_count(pid)
    if stage == "voice":
        return os.path.isfile(sub(pid, "voiceover.mp3"))
    if stage == "mix":
        return mix_settled(pid)
    if stage == "rows":
        return rows > 0
    if stage == "scenes":
        return rows > 0 and line_count(sub(pid, "scenes.txt")) == rows
    if stage == "prompts":
        return rows > 0 and line_count(sub(pid, "prompts.txt")) == rows
    if stage == "images":
        return rows > 0 and count_dir(sub(pid, "images")) >= rows
    if stage == "clips":
        # A film planned with no clips at all is not an unfinished film. Read
        # naively — "done when the clips are on disk" — zero would never be
        # reached and an all-stills film would sit at the money gate forever,
        # waiting to buy nothing.
        if not mix_settled(pid):
            return False
        want = planned_clips(pid)
        return want == 0 or count_dir(sub(pid, "clips")) >= want
    if stage == "render":
        return os.path.isfile(sub(pid, "final.mp4"))
    if stage == "publish":
        return bool((read_json(sub(pid, "state.json"), {}) or {}).get("published"))
    return False


def next_stage(pid):
    for stage in ORDER:
        if not done(pid, stage):
            return stage
    return None


# ── adding a script ──────────────────────────────────────────────────────────

def apply_look(pid, mode, ref_name=""):
    """The project's `look.json`, from a preset, pointed at a reference.

    The style reference stays empty whatever happens. That is not a preference:
    the bulk app copies what is *in* a reference, so a finished scene used as
    one drags its background into every frame — the rule that once marked all
    235 frames of a finished film.
    """
    preset = read_json(os.path.join(LOOKS, f"{mode}.json"))
    if not preset:
        raise ValueError(f"no look preset called {mode!r} in vidgen/looks/")
    look = dict(preset)
    ref = os.path.join(REFS, ref_name) if ref_name else preset.get("character_ref", "")
    look["character_ref"] = ref if (ref and os.path.isfile(ref)) else ""
    look["style_ref"] = ""
    os.makedirs(os.path.join(PROJECTS, pid), exist_ok=True)
    with open(sub(pid, "look.json"), "w", encoding="utf-8") as f:
        json.dump(look, f, indent=2, ensure_ascii=False)
    return look


def write_dash_settings(job):
    """The same `dash.json` the page writes, so the page agrees with the queue.

    Without this the film would be planned twice — once by the job file and
    once by whatever the dashboard happened to remember — and the two would
    disagree about how many clips to buy.
    """
    pid = job["id"]
    data = {k: job[k] for k in
            ("voice", "profile", "aspect", "engine", "batch", "jobs",
             "clip_share", "clip_secs", "still_secs", "placement", "manual")}
    data["look_mode"] = job["look"]
    os.makedirs(os.path.join(PROJECTS, pid), exist_ok=True)
    with open(sub(pid, "dash.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def profile_pool():
    data = read_json(PROFILES_FILE, []) or []
    return [p["name"] for p in data
            if isinstance(p, dict) and p.get("name")]


def free_profile():
    """A browser profile no other unfinished job is already holding.

    Images are the stage that actually parallelises, and they parallelise per
    profile — so handing two queued films the same profile is handing them a
    queue of one. Past the end of the pool it round-robins rather than
    refusing: eleven films on ten accounts should still run, just not all at
    the same moment.
    """
    pool = profile_pool()
    if not pool:
        return JOB_DEFAULTS["profile"]
    held = [j.get("profile") for j in jobs() if j.get("state") != "done"]
    return min(pool, key=lambda name: (held.count(name), pool.index(name)))


def add(title, script_text, **over):
    """One script into the queue, as a real project from the first moment."""
    pid = over.pop("id", None) or slug(title)
    if os.path.isfile(job_path(pid)):
        raise SystemExit(f"'{pid}' pehle se queue me hai — dusra naam do")
    over.setdefault("profile", free_profile())

    os.makedirs(os.path.join(PROJECTS, pid), exist_ok=True)
    with open(sub(pid, "script.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(script_text.strip() + "\n")

    job = {**JOB_DEFAULTS, **over, "id": pid, "title": title.strip(),
           "added": datetime.now().isoformat(timespec="seconds"),
           "state": "queued", "note": "", "stage": "voice"}
    apply_look(pid, job["look"], job["ref"])
    write_dash_settings(job)
    save_job(job)
    write_status()
    return job


# ── the gates ────────────────────────────────────────────────────────────────

def ask_for_scenes(pid, title):
    """The rows, written out where whoever writes the scenes will find them.

    One file per waiting film, holding the row number, when it is spoken and
    what is said — everything needed to write one picture per row and nothing
    else. It is deleted the moment `scenes.txt` is valid, so the folder is a
    to-do list that empties itself.
    """
    os.makedirs(ASKS, exist_ok=True)
    rows = read_json(sub(pid, "segments.json"), []) or []
    path = os.path.join(ASKS, f"{pid}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title or pid} — {len(rows)} scenes chahiye\n\n")
        f.write(f"Likho: `{sub(pid, 'scenes.txt')}`\n")
        f.write(f"Exactly **{len(rows)} lines**, ek row ke liye ek picture.\n\n")
        f.write("Style, character aur negatives `look.json` se aate hain — "
                "scene line me sirf **kya dikh raha hai** likhna hai.\n\n")
        f.write("| # | at | secs | narration |\n|---|---|---|---|\n")
        for r in rows:
            text = r["text"].replace("|", "/").strip()
            f.write(f"| {r['n']} | {int(r['start'])//60:d}:"
                    f"{r['start']%60:05.2f} | {r['dur']:.2f} | {text} |\n")
    return path


def wanted(job):
    """What the film is meant to be, sliders first.

    `dash.json` is what the page last wrote. The page can only save
    *preferences* — not a plan — until the narration exists, so a share moved
    on the strip before `voice` ran lives nowhere else on disk. Reading the job
    file first would quietly throw that away and plan the film from whatever
    was typed when the script was queued.
    """
    return {**job, **(read_json(sub(job["id"], "dash.json"), {}) or {})}


def resolve_mix(job):
    """The share you chose, turned into a real plan before `rows` invents one.

    This exists because of what `mix.load` does when nothing has been chosen:
    it plans *every clip that would fit* — 63 of them on a nine minute film,
    189 credits — and `rows` calls it, so a queued film that nobody had opened
    would arrive at the money gate quoting a number no one picked. The share
    is stored as a percentage precisely so it can survive being set before the
    narration exists; this is where it stops being a percentage.

    A plan already on disk is never overwritten. If the strip was used, the
    strip wins.
    """
    pid = job["id"]
    duration = duration_of(pid)
    if not duration:
        return None

    want = wanted(job)
    clip_secs = min(float(want.get("clip_secs", job["clip_secs"])),
                    mix.engine_seconds(want.get("engine", job["engine"])))
    clips_n = round(mix.most_clips(duration, clip_secs) *
                    float(want.get("clip_share", job["clip_share"])) / 100)
    return mix.save(pid, duration, clips_n, clip_secs,
                    float(want.get("still_secs", job["still_secs"])),
                    want.get("placement", "spread"), want.get("manual"))


def clear_ask(pid):
    path = os.path.join(ASKS, f"{pid}.md")
    if os.path.isfile(path):
        os.remove(path)


def scenes_ready(pid):
    """`scenes.txt` with exactly one line per row, or why not.

    The count is the whole check, and it is worth failing loudly on: the bulk
    app pairs pictures to rows by position, so a scenes file one line short
    slides every picture after it onto the wrong words — the exact failure this
    pipeline has already shipped once.
    """
    rows = row_count(pid)
    have = line_count(sub(pid, "scenes.txt"))
    if not have:
        return False, f"scenes.txt abhi nahi hai - {rows} lines chahiye"
    if have != rows:
        return False, f"scenes.txt me {have} lines hain, {rows} chahiye"
    return True, ""


# ── running one stage ────────────────────────────────────────────────────────

def command(job, stage, dry_run=False):
    pid = job["id"]
    if stage == "voice":
        return PY + ["voice", pid, "--voice", job["voice"]]
    if stage == "rows":
        return PY + ["rows", pid]
    if stage == "prompts":
        return PY + ["prompts", pid, "--scenes", sub(pid, "scenes.txt")]
    if stage == "images":
        return PY + ["images", pid, "--profile", job["profile"],
                     "--batch", str(int(job["batch"]))]
    if stage == "clips":
        argv = PY + ["clips", pid, "--aspect", job["aspect"],
                     "--model", job["engine"]]
        return argv + ["--dry-run"] if dry_run else argv
    if stage == "render":
        return PY + ["render", pid, "--jobs", str(int(job["jobs"]))]
    if stage == "publish":
        argv = PY + ["publish", pid, "--title", job["title"] or pid]
        if job.get("description"):
            argv += ["--description", job["description"]]
        if job.get("hashtags"):
            argv += ["--hashtags", job["hashtags"]]
        return argv
    raise ValueError(f"unknown stage {stage!r}")


def run_stage(job, stage, lanes, dry_run=False):
    """One stage as its own process, holding its lane for as long as it runs."""
    pid = job["id"]
    argv = command(job, stage, dry_run)
    lane = lane_for(job, stage)
    label = stage + (" (dry run)" if dry_run else "")

    if lane:
        note(job, "waiting", f"{label} - {lane} lane khali hone ka intezaar")
        lanes.get(lane).acquire()
    try:
        note(job, "running", label)
        os.makedirs(LOGS, exist_ok=True)
        tail = []
        with open(os.path.join(LOGS, f"{pid}.log"), "a", encoding="utf-8") as log:
            log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} {label} ===\n")
            proc = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace",
                                    bufsize=1)
            for line in proc.stdout:
                log.write(line)
                log.flush()
                tail.append(line.rstrip("\n"))
                del tail[:-40]
            proc.wait()
        return proc.returncode, tail
    finally:
        if lane:
            lanes.get(lane).release()


# ── one film, start to finish ────────────────────────────────────────────────

def work(pid, lanes, stop):
    """Walk one film up the stages, parking at each gate until it opens."""
    while not stop.is_set():
        job = load_job(pid)
        if job is None:                       # removed from the queue mid-run
            return
        stage = next_stage(pid)

        if stage is None:
            clear_ask(pid)
            note(job, "done", "published")
            return

        job["stage"] = stage

        # ── the clip plan, settled before `rows` can guess at one ───────────
        if stage == "mix":
            want = wanted(job)
            if want.get("placement") == "manual" and not want.get("manual"):
                note(job, "needs mix", "manual placement chuna hai - dash pe "
                                       "strip se shots pick karo")
                if stop.wait(10):
                    return
                continue
            plan = resolve_mix(job)
            if not plan:
                note(job, "waiting", "duration abhi nahi - voice ke baad")
                if stop.wait(10):
                    return
                continue
            note(job, "queued", f"plan: {plan['clips']} clips "
                                f"({float(want.get('clip_share', 0)):.0f}%, "
                                f"{want.get('placement','spread')}) = "
                                f"{plan['credits']} credits")
            continue

        # ── gate: the scenes are written by a person, not by a stage ─────────
        if stage == "scenes":
            ok, why = scenes_ready(pid)
            if not ok:
                path = ask_for_scenes(pid, job.get("title"))
                note(job, "needs scenes",
                     f"{why} - {os.path.relpath(path, HERE)}")
                if stop.wait(10):
                    return
                continue
            clear_ask(pid)
            continue

        # ── gate: clips are bought ──────────────────────────────────────────
        if stage == "clips":
            # Reaching here at all means the plan is settled and asks for more
            # than zero: `done` sends an all-stills film straight to render.
            want = planned_clips(pid)
            if not job.get("clips_ok"):
                if not job.get("dry_run_seen"):
                    code, tail = run_stage(job, "clips", lanes, dry_run=True)
                    job = load_job(pid)
                    job["dry_run_seen"] = True
                    job["dry_run_tail"] = tail[-12:]
                    save_job(job)
                    if code != 0:
                        note(job, "failed", f"clips dry run exit {code}")
                        return
                note(job, "needs ok",
                     f"{want} clips = {want * CREDITS_PER_CLIP} credits - "
                     f"`python batch.py ok {pid}`")
                if stop.wait(10):
                    return
                continue

        # ── gate: publishing needs a title someone wrote ────────────────────
        if stage == "publish" and not job.get("publish_ok"):
            note(job, "needs publish ok",
                 f"title/description likh ke `python batch.py publish {pid}`")
            if stop.wait(15):
                return
            continue

        code, tail = run_stage(job, stage, lanes)
        job = load_job(pid) or job
        if code != 0:
            note(job, "failed", f"{stage} exit {code}: " +
                 (tail[-1] if tail else "no output"))
            return
        if not done(pid, stage):
            # A stage that exits clean without producing what it promises is
            # the one failure worth stopping on rather than looping: images
            # returning 48 of 50 pictures would otherwise be retried forever.
            note(job, "failed",
                 f"{stage} theek chala par uska output pura nahi hai")
            return


# ── the runner ───────────────────────────────────────────────────────────────

def queue_rows():
    """The whole queue as the dashboard wants to draw it.

    Counted off the disk every time rather than trusted from the job file: the
    queue is not the only thing that touches these projects, and a panel that
    disagrees with the folder is worse than no panel.
    """
    rows = []
    for job in jobs():
        pid = job["id"]
        rows.append({
            "id": pid, "title": job.get("title", ""),
            "state": job.get("state", "queued"),
            "stage": job.get("stage", ""),
            "note": job.get("note", ""),
            "seen": job.get("seen", ""),
            "rows": row_count(pid),
            "images": count_dir(sub(pid, "images")),
            "clips": count_dir(sub(pid, "clips")),
            "planned_clips": planned_clips(pid),
            "clips_ok": bool(job.get("clips_ok")),
            "credits": planned_clips(pid) * CREDITS_PER_CLIP,
            "profile": job.get("profile", ""),
        })
    return rows


def write_status():
    """One file the dashboard (or a person) can read the whole queue out of."""
    try:
        os.makedirs(QUEUE, exist_ok=True)
        rows = queue_rows()
        tmp = STATUS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"),
                       "jobs": rows}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, STATUS)
    except OSError:
        pass                                   # a status file is not the job


def run(width=None):
    """Work the queue, picking up scripts added while it is already running."""
    lanes = Lanes(width)
    stop = threading.Event()
    live = {}

    waiting = [j for j in jobs() if j.get("state") not in ("done", "failed")]
    print("[batch] lanes: " +
          ", ".join(f"{k} x {(width or {}).get(k, v)}"
                    for k, v in WIDTH.items())
          + f", images x 1 per profile ({len(profile_pool())} profiles)",
          flush=True)
    if waiting:
        print("[batch] " + ", ".join(f"{j['id']}={j.get('profile','?')}"
                                     for j in waiting), flush=True)
    print("[batch] band karne ke liye Ctrl+C", flush=True)
    try:
        while True:
            for job in jobs():
                pid = job["id"]
                if job.get("state") in ("done", "failed"):
                    continue
                if pid in live and live[pid].is_alive():
                    continue
                t = threading.Thread(target=work, args=(pid, lanes, stop),
                                     daemon=True, name=pid)
                live[pid] = t
                t.start()
            write_status()
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()
        print("\n[batch] rukh raha hoon - jo stage chal raha hai woh "
              "khatam hone do", flush=True)
        for t in live.values():
            t.join(timeout=2)


# ── the command line ─────────────────────────────────────────────────────────

def cmd_add(args):
    """A script into the queue, typed or from a file."""
    title = args.title or input("Video ka title: ").strip()
    if not title:
        raise SystemExit("title ke bina nahi")

    if args.file:
        with open(args.file, encoding="utf-8-sig") as f:
            text = f.read()
    else:
        pid = slug(title)
        scratch = os.path.join(QUEUE, "_typing", f"{pid}.txt")
        os.makedirs(os.path.dirname(scratch), exist_ok=True)
        if not os.path.isfile(scratch):
            with open(scratch, "w", encoding="utf-8") as f:
                f.write("")
        print("Notepad khul raha hai - script paste karo, save karo, "
              "phir window band kar do.")
        subprocess.run(["notepad.exe", scratch])
        with open(scratch, encoding="utf-8-sig") as f:
            text = f.read()
        if text.strip():
            os.remove(scratch)

    if not text.strip():
        raise SystemExit("script khali hai — kuch add nahi kiya")

    over = {k: v for k, v in (
        ("look", args.look), ("ref", args.ref), ("profile", args.profile),
        ("voice", args.voice), ("aspect", args.aspect),
        ("placement", args.placement)) if v}
    if args.clips is not None:
        over["clip_share"] = float(args.clips)
    if args.id:
        over["id"] = args.id

    # Asked here, at the only moment the answer is cheap. Left unasked, the
    # number would be decided by `mix.load`'s fallback — every clip that would
    # fit — and the first time anyone saw it would be at the money gate.
    if args.clips is None:
        raw = input(f"Kitni clips? % me "
                    f"(Enter = {JOB_DEFAULTS['clip_share']:.0f}%): ").strip()
        if raw:
            over["clip_share"] = max(0.0, min(100.0, float(raw.rstrip("%"))))
    if not args.placement:
        raw = input("Kahan? spread / front / manual "
                    "(Enter = spread): ").strip().lower()
        if raw in mix.PLACEMENTS:
            over["placement"] = raw

    job = add(title, text, **over)
    words = len(text.split())
    print(f"\n  queue me daal diya: {job['id']}")
    print(f"  {words} words - takreeban {words/160:.1f} min narration")
    print(f"  look {job['look']}, profile {job['profile']}, "
          f"clips {job['clip_share']:.0f}% ({job['placement']})")
    if job["placement"] == "manual":
        print("  manual chuna hai - queue rukegi, dash ki strip pe shots "
              "pick karne ke baad aage badhegi")
    print(f"\n  runner chal raha ho to apne aap uthega. Nahi to: "
          f"Batch.bat double-click karo.")


def cmd_list(args):
    rows = jobs()
    if not rows:
        print("queue khali hai")
        return
    print(f"{'id':28} {'stage':9} {'state':16} note")
    print("-" * 100)
    for job in rows:
        print(f"{job['id'][:28]:28} {job.get('stage',''):9} "
              f"{job.get('state',''):16} {job.get('note','')[:44]}")


def cmd_ok(args):
    job = load_job(args.id) or sys.exit(f"{args.id} queue me nahi hai")
    want = planned_clips(args.id)
    job["clips_ok"] = True
    save_job(job)
    print(f"{args.id}: clips gate khul gaya - {want} clips, "
          f"{want * CREDITS_PER_CLIP} credits")


def cmd_publish(args):
    job = load_job(args.id) or sys.exit(f"{args.id} queue me nahi hai")
    if args.title:
        job["title"] = args.title
    if args.description:
        job["description"] = args.description
    if args.hashtags:
        job["hashtags"] = args.hashtags
    job["publish_ok"] = True
    save_job(job)
    print(f"{args.id}: publish gate khul gaya - \"{job['title']}\"")


def cmd_retry(args):
    job = load_job(args.id) or sys.exit(f"{args.id} queue me nahi hai")
    job["state"] = "queued"
    job["note"] = ""
    save_job(job)
    print(f"{args.id}: phir se try karega - {next_stage(args.id)} se")


def cmd_drop(args):
    """Out of the queue, never off the disk.

    The project folder is left exactly where it is: everything in it was either
    paid for or took real minutes to make, and a queue command is not the place
    a film gets deleted.
    """
    if not os.path.isfile(job_path(args.id)):
        sys.exit(f"{args.id} queue me nahi hai")
    os.remove(job_path(args.id))
    clear_ask(args.id)
    write_status()
    print(f"{args.id}: queue se hata diya - folder waise hi hai")


def main():
    ap = argparse.ArgumentParser(description="scripts ki queue")
    subs = ap.add_subparsers(dest="cmd", required=True)

    s = subs.add_parser("add", help="ek script queue me")
    s.add_argument("--title")
    s.add_argument("--file", help="script ek text file se")
    s.add_argument("--id", help="folder ka naam (default: title se banega)")
    s.add_argument("--look", choices=["2d", "2d_modern", "3d", "realistic"])
    s.add_argument("--ref", help="refs/ me se character reference")
    s.add_argument("--profile", help="bulk app ka browser profile")
    s.add_argument("--voice")
    s.add_argument("--aspect", choices=["landscape", "portrait"])
    s.add_argument("--clips", type=float,
                   help="clip share %%, jitna fit ho sakta hai uska")
    s.add_argument("--placement", choices=["spread", "front", "manual"])
    s.set_defaults(fn=cmd_add)

    s = subs.add_parser("run", help="queue chalao")
    s.add_argument("--net", type=int, help="ek saath kitni voice calls")
    s.add_argument("--cpu", type=int, help="ek saath kitne whisper/ffmpeg")
    s.set_defaults(fn=lambda a: run({k: v for k, v in
                                     (("net", a.net), ("cpu", a.cpu))
                                     if v}))

    s = subs.add_parser("list", help="queue ka haal")
    s.set_defaults(fn=cmd_list)

    s = subs.add_parser("ok", help="clips khareedne ki ijazat")
    s.add_argument("id")
    s.set_defaults(fn=cmd_ok)

    s = subs.add_parser("publish", help="publish ki ijazat, title ke saath")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--description")
    s.add_argument("--hashtags")
    s.set_defaults(fn=cmd_publish)

    s = subs.add_parser("retry", help="failed job phir se")
    s.add_argument("id")
    s.set_defaults(fn=cmd_retry)

    s = subs.add_parser("drop", help="queue se hatao (folder rahega)")
    s.add_argument("id")
    s.set_defaults(fn=cmd_drop)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

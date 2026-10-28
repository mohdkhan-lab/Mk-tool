"""
MAKE — one script in, one published film out, driven by a single command.

    python make.py scripts/myfilm.txt --clips 5 --after 30

Run it, and it carries the film as far as it can on its own: the narration,
the row cut, the prompts, the pictures, the clips, the render and the publish.
It stops at exactly one place, and it stops on purpose.

    THE ONE GATE — scenes.txt

    Every stage here reads a file. One does not: somebody has to decide what
    each line of narration should *look* like. So when the rows are cut this
    writes `NEXT.md` into the project — every row with its timestamp and its
    words — and stops. Write one picture per row into `scenes.txt`, run the
    same command again, and it continues from where it stopped.

Nothing else asks. Credits are spent, the film is rendered and it is filed
without coming back for a yes, because starting the run is the yes.

Re-running is always safe. Every stage decides whether it is done by looking
at the project folder, not by remembering, so an interrupted run resumes and a
finished one does nothing.

    python make.py scripts/myfilm.txt --clips 5 --after 30
    python make.py scripts/myfilm.txt --status      # where is it?
    python make.py scripts/myfilm.txt --redo images # force one stage again

WHAT TO SET, AND WHEN

  --clips N     how many shots move instead of holding still. Decide this
                BEFORE the first run: the narration is cut to it, and the cut
                decides the prompts, which decide every picture. Changing it
                later throws all of that away.
  --after S     keep the first S seconds free of clips. An opening that moves
                is judged before the film has said anything.
  --look        2d_modern for anything present-day, 2d for prehistory and
                ancient history. The character has to belong to the era the
                script is about.

WHAT IT COSTS

  Pictures are free. Clips are bought — three credits each — so `--clips 5`
  is fifteen credits. The narration costs TTS credits by length.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VIDGEN = os.path.join(HERE, "vidgen")
# The user's projects, which an installed copy keeps away from this code.
PROJECTS = os.environ.get("STICKFILM_PROJECTS") or os.path.join(VIDGEN, "projects")
# Looks and reference sheets likewise: the user's copies once installed.
LOOKS = os.environ.get("STICKFILM_LOOKS") or os.path.join(VIDGEN, "looks")
REFS = os.environ.get("STICKFILM_REFS") or os.path.join(VIDGEN, "refs")
PIPELINE = os.path.join(VIDGEN, "pipeline.py")
PY = [sys.executable, "-u", PIPELINE]

sys.path.insert(0, VIDGEN)


def log(msg):
    print(f"[make] {msg}", flush=True)


def die(msg, code=1):
    print(f"\n[make] {msg}\n", flush=True)
    raise SystemExit(code)


# ── the things that must exist before anything can run ──────────────────────

def check_setup():
    """The two secrets this cannot run without, and where they go.

    Deliberately not shipped inside this folder. Copying an API key into a
    directory that gets handed around is how a key ends up somewhere nobody
    meant it to be, so the check names the file and the destination and lets a
    person move it once.
    """
    missing = []
    # The same lookup the stages use: the user's folder first, where the API
    # Keys screen saves, then beside this code.
    sys.path.insert(0, os.path.join(HERE, "bulkgen"))
    from common import key_files
    ai33s = key_files("ai_avatar_pipeline", ".ai33_key")
    gems = key_files("bulkgen", ".gemini_key")
    ai33, gem = ai33s[0], gems[0]
    if not any(os.path.isfile(f) for f in ai33s) and not os.environ.get("AI33_API_KEY"):
        missing.append(("the voice API key", ai33, "AI33_API_KEY"))
    if not any(os.path.isfile(f) for f in gems) and not os.environ.get("GEMINI_API_KEY"):
        # Only the optional `--mode gemini` prompt writer needs this, and this
        # pipeline never uses that mode — the scenes are written by hand. Noted
        # rather than fatal.
        pass
    if missing:
        lines = ["setup is not finished yet:"]
        for what, where, envvar in missing:
            lines.append(f"  {what} is missing")
            lines.append(f"    put it in   {where}")
            lines.append(f"    or set      {envvar}")
        die("\n".join(lines))


# ── the project ─────────────────────────────────────────────────────────────

def project_id(script_path):
    return os.path.splitext(os.path.basename(script_path))[0]


def sub(pid, name):
    return os.path.join(PROJECTS, pid, name)


def read_json(path, fallback=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def prepare(pid, script_path, args):
    """The folder, the script, the look and the settings — written once."""
    folder = os.path.join(PROJECTS, pid)
    os.makedirs(folder, exist_ok=True)

    with open(script_path, encoding="utf-8-sig") as f:
        script = f.read()
    if not script.strip():
        die(f"{script_path} is empty")

    dest = sub(pid, "script.txt")
    if not os.path.isfile(dest) or open(dest, encoding="utf-8").read() != script:
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        log(f"script.txt — {len(script.split())} words")

    # The look is written every run so a corrected --look takes effect, but the
    # reference is only ever the character. A style reference drags whatever is
    # behind it into all several hundred frames.
    preset = read_json(os.path.join(LOOKS, f"{args.look}.json"))
    if not preset:
        die(f"no look called {args.look!r} in vidgen/looks/")
    name = args.ref or preset.get("character_ref", "")
    ref = os.path.join(REFS, name) if name else ""
    look = dict(preset)
    look["character_ref"] = ref if ref and os.path.isfile(ref) else ""
    look["style_ref"] = ""
    # A look that needs a recurring character and cannot find him is the
    # quietest failure in this folder: every stage runs, every count agrees,
    # and the stickman is a different person in all several hundred shots.
    # Nothing in the finished film says why. So it stops here instead.
    if preset.get("needs_ref") and not look["character_ref"] and not preset.get("eras"):
        die(f"look {args.look!r} needs a character reference, and "
            + (f"vidgen/refs/{name} does not exist"
               if name else "its look file names none")
            + " — without it the character is drawn again from scratch "
              "in every shot")

    # A look with eras carries one character sheet per era instead of one for
    # the film — the present-day stickman and the ice-age one are not the same
    # man. Each is resolved and checked exactly like the single one above,
    # because a missing sheet fails just as silently whichever era it is in.
    if preset.get("eras"):
        look["eras"] = {}
        for era, spec in preset["eras"].items():
            spec = dict(spec)
            era_name = spec.get("character_ref", "")
            era_ref = os.path.join(REFS, era_name) if era_name else ""
            if not era_name and not spec.get("character"):
                # An era naming neither is "nobody is in these shots" — a map,
                # an object, an animal. It wants no reference at all, and
                # attaching one is what puts a stickman in a shot of a world
                # map.
                spec["character_ref"] = ""
                spec["style_ref"] = ""
                look["eras"][era] = spec
                continue
            if not (era_ref and os.path.isfile(era_ref)):
                die(f"look {args.look!r} era {era!r} needs a character "
                    + (f"reference and vidgen/refs/{era_name} does not exist"
                       if era_name else "reference and names none")
                    + " — without it that stretch of the film is drawn with a "
                      "different man in every shot")
            spec["character_ref"] = era_ref
            spec["style_ref"] = ""
            look["eras"][era] = spec
        log("look: " + ", ".join(
            f"{era} -> {os.path.basename(s['character_ref']) if s['character_ref'] else 'no character'}"
            for era, s in look["eras"].items()))
    with open(sub(pid, "look.json"), "w", encoding="utf-8") as f:
        json.dump(look, f, indent=2, ensure_ascii=False)

    with open(sub(pid, "dash.json"), "w", encoding="utf-8") as f:
        json.dump({"voice": args.voice, "profile": args.profile,
                   "engine": "omniflash", "aspect": args.aspect,
                   "seconds": 7.0, "batch": 50, "jobs": 8,
                   "look_mode": args.look, "clip_secs": 10.0,
                   "still_secs": 2.3, "placement": "spread", "manual": []},
                  f, indent=1)
    return folder


def run(stage, *extra):
    argv = PY + [stage] + list(extra)
    log("$ " + " ".join(argv[2:]))
    # Relayed, not inherited: under pythonw a child's output goes nowhere.
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(argv, cwd=VIDGEN, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    for line in proc.stdout:
        print(line.rstrip("\n"), flush=True)
    code = proc.wait()
    if code != 0:
        die(f"{stage} stopped with exit {code} — the log above says why", code)


# ── choosing which rows move ────────────────────────────────────────────────

def place_clips(pid, want, after):
    """`want` clips, none before `after` seconds, spread across the film.

    Left to itself the plan puts a clip on the opening shot and spreads the
    rest evenly, which is right for most films and wrong for one that should
    settle before it starts moving. This picks by hand instead: the film after
    `after` is cut into as many bands as there are clips, and each band gives
    up its longest row.

    Length is the whole point of the choice. A clip is ten seconds and is
    trimmed to its row, so buying one for a row of half a second throws the
    credit away on two frames — which is why short rows are skipped even when
    they sit exactly where a clip was wanted.
    """
    import mix

    rows = read_json(sub(pid, "segments.json"), []) or []
    if not rows or not want:
        return []
    after_rows = [r for r in rows if r["start"] >= after]
    if not after_rows:
        die(f"every row starts before {after}s — nothing left to put a clip on")

    first, last = after, rows[-1]["end"]
    long_enough = [r for r in after_rows if r["dur"] >= 6.0]
    picks = []
    for k in range(want):
        lo = first + (last - first) * k / want
        hi = first + (last - first) * (k + 1) / want
        band = [r for r in long_enough if lo <= r["start"] < hi] \
            or [r for r in after_rows if lo <= r["start"] < hi]
        if not band:
            continue
        mid = (lo + hi) / 2
        pick = min(band, key=lambda r: abs(r["start"] - mid))
        if pick["n"] not in [x["n"] for x in picks]:
            picks.append(pick)

    nums = [r["n"] for r in picks]
    state = read_json(sub(pid, "state.json"), {}) or {}
    mix.save(pid, state.get("duration"), len(nums), 10.0, 2.3, "manual", nums)
    for r in picks:
        log(f"  clip on row {r['n']} at {int(r['start'])//60}:"
            f"{r['start'] % 60:05.2f} ({r['dur']:.1f}s)")
    return nums


# ── the gate ────────────────────────────────────────────────────────────────

def ask_for_scenes(pid):
    """Write down what is needed, in the place whoever writes it will look."""
    rows = read_json(sub(pid, "segments.json"), []) or []
    have = 0
    if os.path.isfile(sub(pid, "scenes.txt")):
        with open(sub(pid, "scenes.txt"), encoding="utf-8-sig") as f:
            have = sum(1 for line in f if line.strip())

    path = sub(pid, "NEXT.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {pid} — {len(rows)} scenes needed\n\n")
        if have:
            f.write(f"**scenes.txt currently has {have} lines. It needs exactly "
                    f"{len(rows)}.**\n\n")
        f.write(f"Write `{sub(pid, 'scenes.txt')}` with exactly **{len(rows)} "
                f"lines**, one per row below.\n\n")
        f.write("Rules that matter:\n\n")
        f.write("* One line describes one PICTURE — what a viewer sees while "
                "those words are spoken. Not the sentence again.\n")
        f.write("* No style words. The drawing style, the character and the "
                "things to avoid all come from `look.json` and are added "
                "automatically.\n")
        f.write("* Consecutive lines are consecutive moments. Keep the place "
                "and the people continuous unless the narration moves them.\n")
        f.write("* Nothing written INSIDE the picture — no text, letters or "
                "numbers. Draw the thing instead of spelling it.\n")
        f.write("* Where the narration says something is missing, wrong or "
                "absent, put a bold red X over it; where it says something "
                "works, a thick green tick. Only where the words call for it.\n")
        f.write("* Number each line `1| ...` through "
                f"`{len(rows)}| ...`. The numbers are stripped before use and "
                "they are the only thing that stops a miscount going "
                "unnoticed.\n\n")
        f.write("| # | at | secs | narration |\n|---|---|---|---|\n")
        for r in rows:
            said = " ".join(r["text"].split()).replace("|", "/")
            f.write(f"| {r['n']} | {int(r['start'])//60}:"
                    f"{r['start'] % 60:05.2f} | {r['dur']:.1f} | {said} |\n")
    return path, len(rows), have


# ── what is done, read off the disk ─────────────────────────────────────────

def status(pid):
    import batch
    return batch.next_stage(pid), batch.ORDER


def main():
    ap = argparse.ArgumentParser(
        description="one script in, one published film out",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", help="path to the narration, one blank line "
                                   "between sentences")
    ap.add_argument("--clips", type=int, default=5,
                    help="how many shots move (default 5). Three credits each. "
                         "Set it before the first run — the cut is made to it")
    ap.add_argument("--after", type=float, default=30.0,
                    help="keep the first N seconds clip-free (default 30)")
    # Read off the folder rather than listed here, so a new look is usable the
    # moment its file exists — a hardcoded list is how 2d_mixed was written and
    # then refused by the only command that runs it.
    looks = sorted(f[:-5] for f in os.listdir(LOOKS)
                   if f.endswith(".json"))
    ap.add_argument("--look", default="2d_modern", choices=looks,
                    help="2d_modern for present-day, 2d for ancient history, "
                         "2d_mixed for a script that crosses between them "
                         "(every scene line then names its era)")
    ap.add_argument("--ref", help="character reference in vidgen/refs/ "
                                  "(default: the look's own)")
    ap.add_argument("--voice", default="STIckman Voice")
    ap.add_argument("--profile", default="acc1",
                    help="which signed-in browser profile draws the pictures")
    ap.add_argument("--aspect", default="landscape",
                    choices=["landscape", "portrait"])
    ap.add_argument("--title", help="used when publishing (default: the "
                                    "project name)")
    ap.add_argument("--description", default="")
    ap.add_argument("--hashtags", default="")
    ap.add_argument("--still-max", dest="still_max", type=float, default=3.0,
                    help="no still may hold longer than this (default 3s). "
                         "Longer rows are split into several pictures by the "
                         "narrator's word timings. 0 turns it off")
    ap.add_argument("--flat", dest="parallax", action="store_false",
                    default=True,
                    help="render stills without the parallax push. On by "
                         "default: a flat still reads as a slide, a still "
                         "with depth reads as a shot")
    ap.add_argument("--no-cutouts", dest="cutouts", action="store_false",
                    default=True,
                    help="skip the crossed-out overlays for things the "
                         "narration says are missing. On by default, free, "
                         "and does nothing when nothing is called missing")
    ap.add_argument("--status", action="store_true",
                    help="say where the film is and stop")
    ap.add_argument("--redo", help="run this stage again even if it looks done")
    args = ap.parse_args()

    if not os.path.isfile(args.script):
        die(f"{args.script} does not exist")
    check_setup()

    pid = project_id(args.script)
    prepare(pid, args.script, args)
    import batch

    if args.status:
        at = batch.next_stage(pid)
        log(f"{pid}: " + (f"next is {at}" if at else "finished"))
        for stage in batch.ORDER:
            print(f"   {'done' if batch.done(pid, stage) else '    '}  {stage}")
        return

    if args.redo:
        run(args.redo, pid, *redo_flags(args, args.redo))
        return

    while True:
        at = batch.next_stage(pid)

        if at is None:
            state = read_json(sub(pid, "state.json"), {}) or {}
            log(f"finished — {state.get('published') or 'published'}")
            return

        if at == "voice":
            run("voice", pid, "--voice", args.voice)

        elif at == "mix":
            # The clip count is settled here, before the rows are cut, because
            # the cut is made to it. Answered late it is not a change, it is a
            # different film.
            run("rows", pid, "--clips", str(args.clips), "--align")

        elif at == "rows":
            run("rows", pid, "--align")

        elif at == "scenes":
            path, need, have = ask_for_scenes(pid)
            log("")
            log(f"STOPPED — {pid} needs {need} scene lines"
                + (f" (scenes.txt has {have})" if have else ""))
            log(f"  what to write : {sub(pid, 'scenes.txt')}")
            log(f"  what goes in  : {path}")
            log(f"  then run again: python make.py {args.script} "
                f"--clips {args.clips} --after {args.after:g}")
            return

        elif at == "prompts":
            # Placing the clips needs the real rows, so it happens once they
            # exist and before anything is drawn against them.
            if args.after > 0:
                place_clips(pid, args.clips, args.after)
            run("prompts", pid, "--scenes", sub(pid, "scenes.txt"))

        elif at == "images":
            run("images", pid, "--profile", args.profile, "--batch", "50")
            # A row whose sentence has no comma cannot be cut any shorter, so
            # it arrives here as one picture held for its whole length. Past
            # about three seconds a still stops reading as a shot and starts
            # reading as a stall, so those rows are split by the narrator's own
            # word timings into several pictures instead. Free, and it does
            # nothing when no row is long.
            if args.still_max > 0:
                run("refill", pid, "--stills",
                    "--longer-than", f"{args.still_max:g}",
                    "--seconds-each", f"{args.still_max * 0.8:g}",
                    "--profile", args.profile, "--aspect", args.aspect)
            # When the narration says a thing is absent — "no stove", "no
            # road" — the picture has to say it too. Each named thing is drawn
            # on its own, keyed onto a transparent overlay and crossed out over
            # the shot. The scene image is never touched, so a bad overlay is
            # deleted rather than the whole picture redrawn. Free, and it says
            # so and returns when the narration calls nothing missing.
            if args.cutouts:
                run("cutouts", pid, "--profile", args.profile,
                    "--aspect", args.aspect)

        elif at == "clips":
            # The one place real money is spent. Three credits a clip, and an
            # account that runs dry mid-batch leaves half a film waiting on
            # clips that will never arrive.
            log("")
            log(f"about to buy {args.clips} clip(s) — {args.clips * 3} credits")
            log("  balance: cd ai_avatar_pipeline && python omni.py --quota")
            run("clips", pid, "--aspect", args.aspect, "--model", "omniflash")
            # OmniFlows refuses some generations outright, usually a scene with
            # people in it. Left alone that row holds one picture for ten
            # seconds and the run waits for a clip that is never coming — the
            # single worst stall this pipeline has had. So every refused row is
            # immediately given four stills of its own, drawn from the words
            # actually spoken across it. This spends nothing, and says
            # "nothing to refill" when every clip did arrive.
            run("refill", pid, "--profile", args.profile, "--aspect", args.aspect)

        elif at == "render":
            extra = ["--parallax"] if args.parallax else []
            extra.append("--burn-captions")
            run("render", pid, "--jobs", "8", *extra)

        elif at == "publish":
            extra = []
            if args.description:
                extra += ["--description", args.description]
            if args.hashtags:
                extra += ["--hashtags", args.hashtags]
            run("publish", pid, "--title", args.title or pid, *extra)

        else:
            die(f"no idea how to run {at!r}")


def redo_flags(args, stage):
    if stage == "voice":
        return ["--voice", args.voice, "--force"]
    if stage == "rows":
        return ["--clips", str(args.clips), "--align"]
    if stage == "prompts":
        return ["--scenes", os.path.join(PROJECTS,
                                         project_id(args.script), "scenes.txt")]
    if stage == "images":
        return ["--profile", args.profile, "--batch", "50"]
    if stage == "clips":
        return ["--aspect", args.aspect, "--model", "omniflash", "--redo"]
    if stage == "refill":
        return ["--profile", args.profile, "--aspect", args.aspect]
    if stage == "cutouts":
        return ["--profile", args.profile, "--aspect", args.aspect]
    if stage == "hook":
        return ["--voice", args.voice]
    if stage == "render":
        return ["--jobs", "8", "--force"] + (["--parallax"] if args.parallax
                                             else [])
    if stage == "publish":
        return ["--title", args.title or project_id(args.script)]
    return []


if __name__ == "__main__":
    main()

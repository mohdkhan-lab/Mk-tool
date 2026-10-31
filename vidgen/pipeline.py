"""
VIDGEN — the same film, made of moving clips instead of stills.

    script.txt
       -> voiceover.mp3        a cloned voice, through the ai33 API
       -> segments.json        whisper timings cut on SENTENCE ends
       -> prompts.txt          one image prompt per row
       -> images/              the Gemini bulk app, free, character-consistent
       -> clips/scene_NN.mp4   OmniFlows animates each still (this costs credits)
       -> final.mp4            each clip cut to its own row, over the narration
       -> D:\\Video Projects\\7. Some Title\\

Two things make this different from the image pipeline next door, and both
follow from the same fact — a clip has motion of its own:

  * **rows are cut where sentences end**, not every 2.4 seconds, because a
    moving shot that stops mid-sentence reads as a mistake;
  * **the stills are still drawn first**, and the clips are made *from* them,
    so the stickman cannot drift between shots the way text-to-video would let
    him.

Everything that was already solved is borrowed rather than rewritten: the voice
stage, the whisper timings, the prompt writer, the Gemini bulk image app, the
publisher. `env.py` points them at `vidgen/projects/` so the two pipelines never
share a folder.

    python pipeline.py voice   myfilm --script script.txt --voice "STIckman Voice"
    python pipeline.py rows    myfilm
    python pipeline.py prompts myfilm --look look.json --scenes scenes.txt
    python pipeline.py images  myfilm --profile acc1 --batch 50
    python pipeline.py clips   myfilm --aspect landscape
    python pipeline.py render  myfilm --jobs 4
    python pipeline.py publish myfilm --title "..."
    python pipeline.py status  myfilm
"""
import argparse
import json
import os
import re
import shutil
import sys

import env                       # redirects the project root; must come first
from env import CLIP_MAX, CLIP_SECONDS, CREDITS_PER_CLIP
from common import ai33_key, load_state, log, p, probe, project_dir, slug_ts

import clips as clipstage
import rows as rowstage

HERE = os.path.dirname(os.path.abspath(__file__))
REFS = os.environ.get("STICKFILM_REFS") or os.path.join(HERE, "refs")


def need(pid, name, why):
    path = p(pid, name)
    if not os.path.isfile(path) and not os.path.isdir(path):
        sys.exit(f"{path} is missing — {why}")
    return path


def do_voice(args):
    import voice
    # The buyer picks the production voiceover engine in API Keys
    # (default AI33). The choice is saved beside the TTS keys, so this
    # separate process reads the same one the window shows.
    engine = "ai33"
    try:
        import tts as _tts
        engine = _tts.get_voiceover_provider()
    except Exception:
        engine = "ai33"
    key = ai33_key(args.key) or ""
    if engine == "ai33" and not key:
        # Never fail quietly on a missing AI33 key: say so in the log
        # and speak with free Edge TTS instead, so the film still gets
        # a voice. Adding the AI33 key (API Keys, top right) switches
        # production back to AI33 on the next run.
        log("voice", "AI33 key nahi mili - voiceover FREE Edge TTS se "
                     "ban raha hai. AI33 chahiye to API Keys me AI33 "
                     "key dalo.")
        engine = "edge"
    script = args.script or p(args.id, "script.txt")
    if not os.path.isfile(script):
        sys.exit(f"{script} is missing — write the narration first")
    voice.generate(args.id, script, args.voice, key, speed=args.speed,
                   force=args.force, target=args.target, engine=engine)


def do_rows(args):
    need(args.id, "voiceover.mp3", "run the voice stage first")

    # The clip count is decided HERE or not at all, and this is the one place
    # every path goes through — the page, the queue and the shell.
    #
    # Changing how many clips a film has changes the timestamps the narration
    # is cut at; different timestamps mean different rows; different rows mean
    # different prompts; different prompts mean every image is a different
    # image. So the number cannot be a default picked up on the way past, and
    # it cannot be revisited after the cut without throwing away the pictures.
    #
    # Left alone, `mix.load` answers the question itself with *every clip that
    # would fit* — 61 of them on a ten minute narration, 183 credits — and the
    # first anyone hears of that number is at the money gate. So: refuse.
    import mix as mixstage
    if not os.path.isfile(p(args.id, "mix.json")):
        if args.clips is None:
            sys.exit(
                "kitni clips chahiye? - pehle ye tay kijiye:\n"
                f"    python pipeline.py rows {args.id} --clips 5\n"
                "  ya dashboard par slider set karke 'save mix'.\n\n"
                "  Rows isi hisaab se katti hain. Clips badalne par timestamps\n"
                "  badalte hain, timestamps badalne par prompts, aur prompts\n"
                "  badalne par saari images - isliye ye cut se pehle hi tay\n"
                "  hota hai, baad me nahi.")
        duration = probe(p(args.id, "voiceover.mp3"))
        placement = getattr(args, "placement", "front") or "front"
        mixstage.save(args.id, duration, args.clips, placement=placement)
    elif args.clips is not None:
        duration = probe(p(args.id, "voiceover.mp3"))
        placement = getattr(args, "placement", "front") or "front"
        mixstage.save(args.id, duration, args.clips, placement=placement)
    if args.align:
        import align
        align.build(args.id)
        rowstage.build(args.id, target=args.seconds, language=args.language,
                       force=False)
    else:
        rowstage.build(args.id, target=args.seconds, model_size=args.whisper,
                       language=args.language, force=args.retranscribe)


def do_prompts(args):
    import promptgen
    need(args.id, "segments.json", "run the rows stage first")
    if not (args.scenes or args.from_file or args.mode):
        # `--mode template` used to be the default, so a `prompts` call that
        # simply forgot `--scenes` did not fail — it pasted the narration back
        # in as the picture and carried on. That is how a 408-row film went out
        # with 296 of its prompts reading "a stickman in an expressive scene
        # showing: <the sentence>". Nothing in the run said which mode it took.
        sys.exit(
            "prompts needs to be told where the pictures come from.\n"
            f"    python pipeline.py prompts {args.id} "
            f"--scenes {p(args.id, 'scenes.txt')}\n"
            "scenes.txt is one picture per row, written by hand — that is the "
            "one thing in this pipeline a program cannot decide. --mode "
            "template and --mode gemini both exist and both ruin the film; "
            "pass one explicitly if you really mean it.")
    look = args.look
    proj_look = p(args.id, "look.json")
    if os.path.isfile(proj_look) and (not look or look == "look.json"):
        look = proj_look
    promptgen.build(args.id, mode=args.mode, look_file=look,
                    from_file=args.from_file, scenes_file=args.scenes)


def wipe_stale(pid):
    """Clear last run's leftovers so this run cannot inherit them.

    `assemble.unpack` and `name_by_time` both copy with "skip if the
    destination already exists". That is right for resuming one interrupted
    run and catastrophic across two different ones: every zip names its
    members from 01_001 again, so a second run silently keeps the *first*
    run's pictures wherever the names collide. A re-cut film then shows the
    old cut's images against the new cut's narration — which is exactly the
    drift that made a finished film unusable, with 90 of 154 frames belonging
    to row cuts that no longer existed.

    Pass --resume to keep them when genuinely continuing an interrupted run.
    """
    for name in ("downloads", "images_raw", "images"):
        path = p(pid, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
            log("images", f"cleared {name}/ from the previous run")


def do_images(args):
    import bulkgem
    look = args.look
    proj_look = p(args.id, "look.json")
    if os.path.isfile(proj_look) and (not look or look == "look.json"):
        look = proj_look
    if not (args.check or args.dump):
        need(args.id, "prompts.txt", "run the prompts stage first")
        if not args.resume:
            wipe_stale(args.id)
    bulkgem.run(args.id, profile=args.profile, look_file=look,
                aspect=args.aspect, batch=args.batch,
                do_check=args.check, do_dump=args.dump)
    # Every look forbids panels and the app obeys most of the time. The two it
    # does not obey look like any other file, so the pictures are looked at.
    if not (args.check or args.dump):
        try:
            import panels
            panels.check(args.id)
        except Exception as exc:
            log("panels", f"could not check for split pictures — {exc}")


def do_panels(args):
    import panels
    panels.check(args.id, threshold=args.threshold)


def do_repair(args):
    """Pictures for one stretch of rows, taken from a throwaway project.

    A round that fails is not a reason to redraw the whole film. The images
    stage is all-or-nothing — it clears `images/` and starts again — so a
    single bad round used to cost every picture, and repairing it by hand cost
    the same thing twice over. This puts the repair in the pipeline:

        python pipeline.py images  _refill_myfilm --profile acc3
        python pipeline.py repair  myfilm --from _refill_myfilm --rows 151-197

    The rows are replaced from `--rows` to the end of the film, because that is
    the shape a broken round actually has: the round that failed and everything
    the bad zip pushed out of place after it. The raw count is checked against
    the row count before anything is written, since equal counts are the only
    thing standing between the film and a one-picture drift.
    """
    import assemble
    need(args.id, "segments.json", "run the rows stage first")

    with open(p(args.id, "segments.json"), encoding="utf-8") as f:
        rows = json.load(f)

    first, _, last = args.rows.partition("-")
    first = int(first)
    last = int(last) if last else len(rows)
    if not 1 <= first <= last <= len(rows):
        sys.exit(f"--rows {args.rows} is outside 1-{len(rows)}")
    if last != len(rows):
        sys.exit("refill replaces from --rows to the end of the film; "
                 f"use --rows {first}- for that")

    raw_dir, from_dir = p(args.id, "images_raw"), p(args.from_id, "images_raw")
    if not os.path.isdir(from_dir):
        sys.exit(f"{from_dir} is missing — run the images stage on "
                 f"{args.from_id} first")

    keep = assemble.sorted_raw(raw_dir)[:first - 1]
    fresh = assemble.sorted_raw(from_dir)
    want = len(rows) - (first - 1)
    if len(fresh) != want:
        sys.exit(f"{args.from_id} has {len(fresh)} images but rows {first}-"
                 f"{len(rows)} need {want} — the refill prompts must be "
                 f"exactly those rows")

    log("refill", f"keeping {len(keep)} pictures, replacing {want} from "
                  f"{args.from_id}")
    assemble.replace_tail(raw_dir, keep, fresh)
    images = assemble.sorted_raw(raw_dir)
    if len(images) != len(rows):
        sys.exit(f"after the refill there are {len(images)} pictures for "
                 f"{len(rows)} rows — refusing to name them")

    shutil.rmtree(p(args.id, "images"), ignore_errors=True)
    assemble.name_by_time(args.id, images, rows)
    log("refill", f"{len(images)} pictures now line up with {len(rows)} rows")


def do_clips(args):
    need(args.id, "segments.json", "run the rows stage first")
    if not args.collect:
        need(args.id, "images", "run the images stage first")
    clipstage.run(args.id, aspect=args.aspect, model=args.model,
                  limit=args.limit, redo=args.redo, dry_run=args.dry_run,
                  collect_only=args.collect)


def do_render(args):
    import assemble_clips
    need(args.id, "segments.json", "run the rows stage first")
    
    # Universal Automatic Negation Cross (❌) Stamping Rule
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from apply_negation_red_crosses import stamp_project_negations
        stamp_project_negations(args.id)
    except Exception as exc:
        pass

    assemble_clips.render(args.id, aspect=args.aspect, size=args.size,
                          fit=args.fit, jobs=args.jobs, parallax=args.parallax,
                          burn_captions=args.burn_captions, force=args.force)


def do_charref(args):
    """Draw the character sheet an era needs, from the words that describe him.

    A look that crosses eras carries one sheet per era, and a sheet that does
    not exist stops the film before it starts — deliberately, because a missing
    reference is otherwise silent and the man is drawn afresh in every shot.
    This is how the missing one gets made: the era's own character description,
    drawn on a plain background so nothing but the character can be copied out
    of it, seeded from a sheet that already exists so he stays the same person.

    Several are drawn and none is installed automatically. Which one reads as
    the same character is a judgement, and the file it becomes is attached to
    every frame of every film that uses the look.
    """
    import bulkgem
    from promptgen import load_look

    look = load_look(args.look)
    spec = (look.get("eras") or {}).get(args.era)
    if spec is None:
        sys.exit(f"{args.look} has no era called {args.era!r} — it knows "
                 + (", ".join(sorted(look.get("eras") or {})) or "none"))

    cast = (spec.get("character") or look.get("character") or "").strip()
    style = spec.get("style") or look.get("style")

    # Every look forbids "no panels, no split screen, no comic strip layout",
    # and it is right to — a panelled frame in a finished film is two shots at
    # once, each half size. A character sheet is the one picture in this folder
    # that wants panels, and asking for one with that clause still attached
    # gets a single standing figure back every time. Four candidates in a row
    # came back that way before the clause was found.
    negative = ", ".join(
        bit for bit in (look.get("negative") or "").split(",")
        if not re.search(r"panel|split screen|comic strip", bit, re.I))
    if not cast:
        sys.exit(f"era {args.era!r} describes no character to draw")

    # Several poses in ONE picture, not one pose four times.
    #
    # A reference teaches the app two things at once: who the character is, and
    # what a shot of him looks like. A single clean full-body portrait on a
    # plain background teaches both — and the second one is poison. Drawn from
    # a reference like that, a hundred and sixty-five shots came back as the
    # same composition: the figure centred, front on, arms down, on flat grey.
    # The film had nowhere to be.
    #
    # The ancient sheet in this folder does not do that, and it is worth
    # understanding why: it holds the same character three times over, in three
    # different poses in three different places. Nothing in it can be copied as
    # "the shot", because its own panels disagree. Only the character survives,
    # which is the whole job of the file.
    poses = [
        "a three-panel character sheet, the same character three times: "
        "standing full length facing forward with his arms at his sides in the "
        "left panel, walking in profile mid-stride in the centre panel, and "
        "crouching low reaching one hand forward in the right panel, each panel "
        "a different plain flat colour",
        "a three-panel character sheet, the same character three times: "
        "seen from a low angle with both arms raised in the left panel, from "
        "behind looking over his shoulder in the centre panel, and a head and "
        "shoulders close-up smiling in the right panel, each panel a different "
        "plain flat colour",
        "a four-panel character sheet, the same character four times in "
        "different poses — sitting, running, pointing away from the viewer, and "
        "lying on his side — each panel a different plain flat colour",
        "a three-panel character sheet, the same character three times: "
        "leaning on something out of frame in the left panel, arms folded in "
        "the centre panel, and turning sharply to look behind him in the right "
        "panel, each panel a different plain flat colour",
    ]

    pid = f"_charref_{args.era}"
    # Cleared every run. The images stage keeps a round's zip and skips the
    # round when it is still valid — right for resuming an interrupted film,
    # wrong here: a second charref call handed back the first call's four
    # candidates, byte for byte, and looked like the prompt had been ignored.
    shutil.rmtree(os.path.abspath(p(pid, "")), ignore_errors=True)
    os.makedirs(p(pid), exist_ok=True)
    with open(p(pid, "prompts.txt"), "w", encoding="utf-8") as f:
        for pose in poses:
            f.write(f"{style}. {cast}. {pose}. Every panel shows the same "
                    f"character with no scenery, no ground and no props behind "
                    f"him. {negative}." + chr(10))

    seed = spec.get("seed_ref") or look.get("character_ref")
    log("charref", f"drawing {len(poses)} sheets for era {args.era!r}")
    if seed:
        log("charref", f"  seeded from {os.path.basename(seed)}")
    bulkgem.run(pid, profile=args.profile, character=seed, style=None,
                aspect="16:9", batch=0)

    raw = p(pid, "images_raw")
    got = sorted(os.listdir(raw)) if os.path.isdir(raw) else []
    log("charref", f"{len(got)} candidate(s) in {raw}")
    for name in got:
        log("charref", f"  {os.path.join(raw, name)}")
    log("charref", "Look at them. Copy the one that is clearly the same "
                   "character, on nothing but background, to:")
    want = spec.get("character_ref") or (args.era + ".png")
    log("charref", f"  {want if os.path.isabs(want) else os.path.join(REFS, want)}")
    log("charref", "The app copies what is *in* a reference, so a candidate "
                   "with scenery behind him drags that scenery into every "
                   "frame of every film using this look.")


def do_redraw(args):
    """Draw named rows again and put the new pictures in their place.

    `refill` gives a row *several* stills, for a clip that was refused or a
    sentence too long to hold on one picture. There was no way to simply redraw
    a row that came back wrong — which meant the only repair for a handful of
    bad frames was copying files in by hand, and hand-work leaves nothing
    behind for the next film.

    A picture is free. When one contradicts its line, fix the scene line and
    redraw the row.
    """
    import bulkgem
    rows_wanted = sorted({int(x) for x in str(args.rows).replace(" ", "").split(",") if x})
    if not rows_wanted:
        sys.exit("redraw needs --rows 100,104,114")

    with open(p(args.id, "segments.json"), encoding="utf-8") as f:
        rows = json.load(f)
    by_n = {r["n"]: r for r in rows}
    unknown = [n for n in rows_wanted if n not in by_n]
    if unknown:
        sys.exit(f"no such row(s) in this cut: {unknown}")

    scenes_txt = need(args.id, "scenes.txt", "redraw re-uses the scene lines")
    lines = [l.rstrip() for l in open(scenes_txt, encoding="utf-8") if l.strip()]
    if len(lines) != len(rows):
        sys.exit(f"scenes.txt has {len(lines)} lines for {len(rows)} rows")

    scratch = f"_redraw_{args.id}".replace(" ", "_")
    d = os.path.abspath(p(scratch, ""))
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    shutil.copy(p(args.id, "look.json"), os.path.join(d, "look.json"))

    # Renumbered 1..N for the scratch project, with the era tag kept so each
    # row is still drawn with the character its own shot calls for.
    with open(os.path.join(d, "scenes.txt"), "w", encoding="utf-8") as f:
        for k, n in enumerate(rows_wanted, 1):
            body = lines[n - 1].split("|", 1)[1]
            f.write(f"{k}|{body}" + chr(10))

    with open(os.path.join(d, "look.json"), encoding="utf-8") as f:
        look = json.load(f)
    scratch_prompts(d, os.path.join(d, "scenes.txt"), look, len(rows_wanted))

    log("redraw", f"{len(rows_wanted)} row(s): {rows_wanted}")
    bulkgem.run(scratch, profile=args.profile,
                look_file=os.path.join(d, "look.json"),
                aspect=args.aspect, batch=args.batch)

    # Which scratch picture belongs to which of the rows asked for. The rounds
    # are grouped by character, so this map is the only thing that knows.
    with open(os.path.join(d, "rounds.json"), encoding="utf-8") as f:
        plan = json.load(f)
    raw = os.path.join(d, "images_raw")
    groups = {}
    for name in sorted(os.listdir(raw), key=lambda s: [int(t) if t.isdigit() else t
                                                       for t in re.split(r"(\d+)", s)]):
        head = name.split("_", 1)[0]
        groups.setdefault(int(head) if head.isdigit() else 0, []).append(
            os.path.join(raw, name))
    got = [groups[k] for k in sorted(groups)]
    if len(got) != len(plan):
        sys.exit(f"{len(got)} round(s) came back for {len(plan)} planned — "
                 "nothing replaced")

    fresh = {}
    for entry, files in zip(plan, got):
        if len(files) != len(entry["rows"]):
            sys.exit(f"round {entry['round']} came back with {len(files)} "
                     f"picture(s) for {len(entry['rows'])} — nothing replaced")
        for k, src in zip(entry["rows"], files):
            fresh[rows_wanted[k - 1]] = src

    with open(p(args.id, "edit.json"), encoding="utf-8") as f:
        edit = json.load(f)
    for item in edit:
        src = fresh.get(item["n"])
        if not src:
            continue
        dst = item["image"]
        shutil.copyfile(src, dst)
        item["source"] = os.path.basename(src)
        log("redraw", f"  row {item['n']:>3}  {os.path.basename(dst)}")
    with open(p(args.id, "edit.json"), "w", encoding="utf-8") as f:
        json.dump(edit, f, indent=1, ensure_ascii=False)
    log("redraw", f"{len(fresh)} picture(s) replaced — render again to see them")


def do_publish(args):
    import publish
    need(args.id, "final.mp4", "render the film first")
    publish.publish(args.id, title=args.title, description=args.description,
                    hashtags=args.hashtags, force=args.force,
                    number=args.number)


def do_mix(args):
    """Choose the shot plan: how many clips, how long each kind holds."""
    import mix as mixstage
    state = load_state(args.id)
    duration = state.get("duration") or probe(p(args.id, "voiceover.mp3"))
    if not duration:
        sys.exit("no narration yet — run the voice stage first")
    clip_secs = args.clip_seconds or mixstage.engine_seconds(args.engine)
    clips = args.clips
    if clips is None:                       # a share of what would ever fit
        clips = round(mixstage.most_clips(duration, clip_secs) * args.share / 100)
    data = mixstage.save(args.id, duration, clips, clip_secs,
                         args.still_seconds,
                         getattr(args, "placement", "spread"))
    print(f"  {data['shots']} shots -> {data['images']} pictures to draw")
    print(f"  re-cut the rows to it:  python pipeline.py rows {args.id} --force")
    return data


def do_auto(args):
    """Script in, film out — stopping only where money would be spent.

    Everything before the clips stage is free and repeatable, so there is no
    reason to hold any of it behind a question. The clips stage is the one that
    buys something, and it stays a dry run until a human looks at the pairing
    count the site itself reports.
    """
    steps = [("voice", do_voice), ("mix", do_mix), ("rows", do_rows),
             ("prompts", do_prompts), ("images", do_images)]
    for name, fn in steps:
        log("auto", f"— {name} —")
        fn(args)

    log("auto", "— clips (dry run) —")
    args.dry_run = True
    do_clips(args)
    log("auto", "stopped before spending. The pairing count above is the one "
                "the site itself sees; when it matches, run:")
    log("auto", f"  python pipeline.py clips {args.id} --aspect {args.aspect}")
    log("auto", f"  python pipeline.py render {args.id}")


def scratch_prompts(scratch_dir, scenes_path, look, count):
    """Dress a scratch batch of scene lines, carrying their eras with them.

    The refill and cutout batches are drawn in their own throwaway project, and
    a film whose look has eras needs that project to know them too — otherwise
    a picture redrawn for an ice-age row comes back wearing a t-shirt. The tags
    ride along on the scene lines these paths copy from `scenes.txt`, so all
    that is needed is to write them out where the images stage will look.
    """
    import promptgen
    prompts, eras = promptgen.from_scenes(scenes_path,
                                          [{"n": i + 1} for i in range(count)],
                                          look)
    with open(os.path.join(scratch_dir, "prompts.txt"), "w",
              encoding="utf-8") as f:
        f.write(chr(10).join(prompts) + chr(10))
    era_file = os.path.join(scratch_dir, "eras.json")
    if any(eras):
        with open(era_file, "w", encoding="utf-8") as f:
            json.dump([{"n": i + 1, "era": e} for i, e in enumerate(eras)],
                      f, indent=1, ensure_ascii=False)
    elif os.path.isfile(era_file):
        os.remove(era_file)
    return prompts


def do_refill(args):
    """Draw several pictures for the rows whose clip never came back.

    OmniFlows refuses some generations outright — a safety filter, usually on
    a scene with people in it — and a refused row is often one of the long
    ones. Left alone it becomes a single picture held for ten seconds, which
    is exactly the deadness the still length was tuned away from. So the row
    gets four or five pictures of its own instead, and the render shows them
    in turn across the same span.

    Pictures are free; this spends nothing.
    """
    import json
    import shutil
    import bulkgem
    import mix as mixstage

    with open(p(args.id, "segments.json"), encoding="utf-8") as f:
        rows = json.load(f)
    kinds = mixstage.kinds_for(args.id, len(rows))
    wanted = {i + 1 for i, k in enumerate(kinds) if k == "clip"}

    have = set()
    scenes_file = p(args.id, "scenes.json")
    if os.path.isfile(scenes_file):
        with open(scenes_file, encoding="utf-8") as f:
            for sc in json.load(f).get("scenes", []):
                rel = sc.get("video_path")
                if rel and os.path.isfile(p(args.id, rel)):
                    have.add(sc["scene_number"])

    if args.stills:
        # A different job with the same machinery. Rows are cut on sentence
        # ends, and a sentence with no comma in it cannot be split any
        # further — 41 of this script's long rows were single unbroken
        # clauses, which is why the cut sat at four seconds a row however low
        # the target went. Those rows still hold one picture for six or eight
        # seconds. Splitting them by the words being spoken gives each part
        # its own picture without touching the cut the narration is timed to.
        kinds_by_row = {i + 1: k for i, k in enumerate(kinds)}
        short = [r["n"] for r in rows
                 if kinds_by_row.get(r["n"]) == "still"
                 and r["dur"] >= args.longer_than
                 and not r.get("listed")]
        wanted = set(short)
        have = set()
    else:
        short = sorted(wanted - have)
        # Refill draws several pictures for every row it thinks is empty, so
        # running it while the clips stage is still going would burn a batch on
        # rows whose clip is minutes away. Almost nothing on disk means the run
        # has not finished, not that everything was refused.
        if not args.rows and have and len(short) > len(have):
            raise SystemExit(
                f"only {len(have)} of {len(wanted)} clips are on disk — the "
                f"clips stage looks like it is still running. Let it finish, "
                f"then refill what is genuinely missing (or use --rows).")
    if args.rows:
        pick = {int(x) for x in args.rows.replace(",", " ").split()}
        short = [n for n in short if n in pick]
    if not short:
        log("refill", "every wanted clip is on disk — nothing to refill")
        return

    scenes_txt = p(args.id, "scenes.txt")
    if not os.path.isfile(scenes_txt):
        raise FileNotFoundError(f"{scenes_txt} — refill re-uses the scene lines")
    lines = [l.strip() for l in open(scenes_txt, encoding="utf-8") if l.strip()]

    def want_for(row):
        # One picture per couple of seconds, which is the pace the rest of the
        # film runs at. A flat count would give a four-second row the same
        # four pictures as a nine-second one.
        if not args.stills:
            return max(2, int(args.pictures))
        return max(2, min(6, int(round(row["dur"] / args.seconds_each))))

    by_row_all = {r["n"]: r for r in rows}
    n_each = max(2, int(args.pictures))
    log("refill", f"{len(short)} row(s) with no clip: {short[:12]}"
                  f"{' …' if len(short) > 12 else ''}")
    planned = sum(want_for(by_row_all[n]) for n in short)
    log("refill", f"  drawing {planned} picture(s) in all"
                  + ("" if args.stills else f" — {n_each} per row"))

    # What is actually being said inside the row, cut on word timings. A
    # refused row is ten seconds of narration naming several things, and four
    # pictures of the same sentence at four camera angles is still one idea
    # held for ten seconds. Splitting on the words means each picture arrives
    # as its own thing is named, which is the whole reason the film feels
    # alive anywhere else.
    with open(p(args.id, "words.json"), encoding="utf-8") as f:
        words = json.load(f)

    def pieces(row, n_want):
        inside = [w for w in words
                  if w["start"] >= row["start"] - 0.01
                  and w["end"] <= row["end"] + 0.01]
        if len(inside) < n_want:
            return [(row["start"], row["text"])]
        # Cut near the even split but on a comma or full stop where one is in
        # reach. Splitting on the count alone breaks phrases in half — "the
        # most important effects may" / "have happened much farther away" —
        # and the picture is then drawn from half an idea.
        import re as _re
        ends = _re.compile(r"[.,!?;:]$")
        step = len(inside) / n_want
        cuts, used = [], set()
        for j in range(1, n_want):
            ideal = int(round(j * step))
            reach = max(1, int(step / 2))
            near = [i for i in range(max(1, ideal - reach),
                                     min(len(inside) - 1, ideal + reach) + 1)
                    if i not in used and ends.search(inside[i - 1]["text"].strip())]
            at = min(near, key=lambda i: abs(i - ideal)) if near else ideal
            if 0 < at < len(inside) and at not in used:
                cuts.append(at)
                used.add(at)
        bounds = [0] + sorted(cuts) + [len(inside)]
        out = []
        for a, b in zip(bounds, bounds[1:]):
            chunk = inside[a:b]
            if not chunk:
                continue
            out.append((chunk[0]["start"],
                        " ".join(w["text"] for w in chunk).strip()))
        return out

    angles = ["wide establishing shot", "medium shot, closer in",
              "close-up on the detail", "low angle looking up",
              "high angle looking down", "over-the-shoulder view"]
    scratch = f"_refill_{args.id}".replace(" ", "_")
    out_dir = p(args.id, "images_extra")
    os.makedirs(out_dir, exist_ok=True)

    scratch_dir = os.path.abspath(p(scratch, ""))
    shutil.rmtree(scratch_dir, ignore_errors=True)
    os.makedirs(scratch_dir, exist_ok=True)
    shutil.copy(p(args.id, "look.json"), os.path.join(scratch_dir, "look.json"))

    by_row = {r["n"]: r for r in rows}
    order, out_lines = [], []
    for n in short:
        base = lines[n - 1] if n - 1 < len(lines) else ""
        for k, (at, said) in enumerate(pieces(by_row[n], want_for(by_row[n]))):
            # The row's own scene keeps the place, the cast and the period;
            # the words say what this instant is about.
            out_lines.append(f"{base}. At this moment the narration says: "
                             f"{said}. {angles[k % len(angles)]}.")
            order.append((n, k, at))
    with open(os.path.join(scratch_dir, "scenes.txt"), "w", encoding="utf-8") as f:
        f.write(chr(10).join(out_lines) + chr(10))

    with open(os.path.join(scratch_dir, "look.json"), encoding="utf-8") as f:
        look = json.load(f)
    scratch_prompts(scratch_dir, os.path.join(scratch_dir, "scenes.txt"),
                    look, len(out_lines))

    bulkgem.run(scratch, profile=args.profile,
                look_file=os.path.join(scratch_dir, "look.json"),
                aspect=args.aspect, batch=args.batch)

    raw = os.path.join(scratch_dir, "images_raw")
    got = sorted(os.listdir(raw)) if os.path.isdir(raw) else []
    if len(got) != len(order):
        log("refill", f"  {len(got)} picture(s) came back for {len(order)} asked "
                      f"— filling what arrived")
    # Named by where each picture actually lands, like every other image in
    # the pipeline. A refused row is split between its pictures, so each one
    # gets its own instant rather than the row's — that is the timestamp a
    # person scrubbing the film would look for.
    for (n, k, at), src in zip(order, got):
        name = f"{slug_ts(at)}_i{n:03d}_{k:02d}.png"
        shutil.copy(os.path.join(raw, src), os.path.join(out_dir, name))
    log("refill", f"{out_dir} — {min(len(got), len(order))} picture(s) filed")
    log("refill", "  render now: those rows play as several stills instead of "
                  "one held picture")


def do_hook(args):
    """A cold open in front of the finished cut."""
    import hook as hookstage
    key = ai33_key(args.key)
    if not key:
        sys.exit("no ai33 key - put one in ai_avatar_pipeline/.ai33_key")
    need(args.id, "final.mp4", "render the film before hooking it")
    hookstage.build(args.id, args.voice, key, speed=args.speed,
                    aspect=args.aspect, size=args.size, fit=args.fit,
                    out_name=args.out)


def do_cutouts(args):
    """The things the narration says are missing, drawn and crossed out.

    Free: the objects come from the same Gemini bulk app the stills do. The
    scene image is never touched — each object becomes its own transparent
    overlay, so a bad one is deleted rather than redrawn.
    """
    import json
    import shutil
    import bulkgem
    import cutout as cutoutstage
    import promptgen

    with open(p(args.id, "segments.json"), encoding="utf-8") as f:
        rows = json.load(f)

    wanted = []
    for r in rows:
        for k, thing in enumerate(cutoutstage.targets(r["text"])):
            wanted.append((r["n"], k, thing, r["start"]))
    if args.rows:
        pick = {int(x) for x in args.rows.replace(",", " ").split()}
        wanted = [w for w in wanted if w[0] in pick]
    if not wanted:
        log("cutouts", "nothing the narration calls missing that can be drawn")
        return

    log("cutouts", f"{len(wanted)} thing(s) to draw across "
                   f"{len(set(w[0] for w in wanted))} row(s)")
    for n, k, thing, _ in wanted[:12]:
        log("cutouts", f"  row {n:>3}  {thing}")

    scratch = f"_cut_{args.id}".replace(" ", "_")
    d = p(scratch, "")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)

    # An object on flat white, keyed later. Everything that could put pixels
    # near the edges is refused here rather than cleaned up afterwards: a
    # shadow or a horizon survives the key and reads as a grey box.
    look = {
        "label": "cutout",
        "needs_ref": False,
        "style": ("product photograph of a single object, centred, isolated on a "
                  "pure flat white background, even studio lighting, sharp focus, "
                  "full object visible with clear space around it"),
        "character": "",
        "negative": ("no text, no letters, no numbers, no logo, no watermark, "
                     "no people, no background scenery, no floor, no horizon, "
                     "no gradient, no shadow, no reflection, no border, no frame"),
        "shots": ["centred product shot"],
    }
    with open(os.path.join(d, "look.json"), "w", encoding="utf-8") as f:
        json.dump(look, f, indent=1)
    with open(os.path.join(d, "scenes.txt"), "w", encoding="utf-8") as f:
        f.write(chr(10).join(f"a {t}" for _, _, t, _ in wanted) + chr(10))

    scratch_prompts(d, os.path.join(d, "scenes.txt"), look, len(wanted))

    bulkgem.run(scratch, profile=args.profile,
                look_file=os.path.join(d, "look.json"),
                aspect=args.aspect, batch=args.batch)

    raw = os.path.join(d, "images_raw")
    got = sorted(os.listdir(raw)) if os.path.isdir(raw) else []
    out_dir = p(args.id, "cutouts")
    os.makedirs(out_dir, exist_ok=True)
    for (n, k, thing, at), src in zip(wanted, got):
        name = f"{slug_ts(at)}_i{n:03d}_{k:02d}.png"
        cutoutstage.prepare(os.path.join(raw, src),
                            os.path.join(out_dir, name), args.height)
    log("cutouts", f"{out_dir} - {min(len(got), len(wanted))} overlay(s) ready")
    log("cutouts", "  render now: they are laid over their own rows")
    shutil.rmtree(d, ignore_errors=True)


def do_status(args):
    state = load_state(args.id)
    folder = project_dir(args.id, create=False)
    print(f"{args.id} — {folder}")
    if not os.path.isdir(folder):
        print("  nothing here yet")
        return

    marks = (("script.txt", "narration"), ("voiceover.mp3", "voice"),
             ("segments.json", "rows"), ("prompts.txt", "prompts"),
             ("images", "images"), ("clips", "clips"), ("final.mp4", "film"))
    for name, label in marks:
        path = p(args.id, name)
        if os.path.isdir(path):
            count = len(os.listdir(path))
            print(f"  {label:9} {'yes' if count else 'no ':3}  {count} file(s)")
        elif os.path.isfile(path):
            extra = f"  {probe(path):.1f}s" if name.endswith((".mp3", ".mp4")) else ""
            print(f"  {label:9} yes{extra}")
        else:
            print(f"  {label:9} no")

    n = state.get("segments")
    if n:
        print(f"  {n} rows, cut on {state.get('cut_on', '?')}, "
              f"{state.get('seconds_per_clip', '?')}s a clip")
        # What it costs depends on the mix, not on the row count: rows left as
        # stills are free, and quoting the whole film every time hides that.
        import mix as mixstage
        kinds = mixstage.kinds_for(args.id, n)
        planned = sum(1 for k in kinds if k == "clip")
        print(f"  mix: {planned} clip(s), {n - planned} still(s) "
              f"— about {planned * CREDITS_PER_CLIP} credits to animate")
    if state.get("published"):
        print(f"  published: {state['published']}")


def main():
    ap = argparse.ArgumentParser(
        description="Make a film out of clips, one clip per sentence-length row.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="stage", required=True)

    def stage(name, fn, help_text):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("id", help="the project name (a folder under projects/)")
        s.set_defaults(run=fn)
        return s

    s = stage("voice", do_voice, "script -> voiceover.mp3")
    s.add_argument("--script")
    s.add_argument("--voice", default="STIckman Voice")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--target", type=float, default=None,
                   help="seconds the narration should last (fitted after speaking)")
    s.add_argument("--key")
    s.add_argument("--force", action="store_true")

    s = stage("rows", do_rows, "voiceover.mp3 -> rows cut on sentence ends")
    s.add_argument("--clips", type=int,
                   help="how many shots move. Required the first time: the cut "
                        "is made to this number and every prompt and image "
                        "follows from it")
    s.add_argument("--seconds", type=float, default=CLIP_SECONDS,
                   help=f"roughly how long one clip should cover "
                        f"(default {CLIP_SECONDS:g}s; rows never pass {CLIP_MAX:g}s)")
    s.add_argument("--language")
    s.add_argument("--placement", default="front", choices=["front", "spread", "manual"],
                   help="where clips land: front (opening hook scenes) or spread")
    # On by default. It was opt-in, and a Hindi film was cut without it: the
    # transcriber lost 31% of the narration, the rows collapsed from 193 to
    # 101, one of them ran 48 seconds, and nothing in the run said so. There is
    # no case where a transcriber's guess beats the script that was read aloud.
    s.add_argument("--align", action="store_true", default=True,
                   help="cut on the SCRIPT's words, timed by the voice parts "
                        "(default: on)")
    s.add_argument("--no-align", dest="align", action="store_false",
                   help="cut on the TRANSCRIBER's words instead. Almost never "
                        "right: on any language whisper reads badly the "
                        "transcript loses whole stretches of the narration")
    s.add_argument("--whisper", default="small",
                   help="whisper model size (default small). Non-English "
                        "narrations — Hindi included — lose sentence-ending "
                        "punctuation on small; medium or large-v3 reads it "
                        "correctly at the cost of more CPU minutes")
    s.add_argument("--retranscribe", "--force", dest="retranscribe",
                   action="store_true",
                   help="run whisper again instead of reusing words.json "
                        "(minutes on CPU; only needed when the narration "
                        "itself changed — re-cutting the rows does not)")

    s = stage("mix", do_mix, "how many shots move, and how long each holds")
    s.add_argument("--clips", type=int,
                   help="how many shots are clips (default: --share of the most "
                        "that would fit)")
    s.add_argument("--share", type=float, default=40.0,
                   help="if --clips is not given, this %% of the maximum")
    s.add_argument("--clip-seconds", dest="clip_seconds", type=float,
                   help="default: whatever the engine returns")
    s.add_argument("--still-seconds", dest="still_seconds", type=float,
                   default=2.3, help="how long one still holds (default 2.3s)")
    s.add_argument("--engine", default="omniflash",
                   choices=["omniflash", "veo"])

    s = stage("prompts", do_prompts, "rows -> one image prompt each")
    s.add_argument("--look", default="look.json")
    s.add_argument("--mode", default=None,
                   choices=["template", "gemini", "llm"],
                   help="template pastes the narration sentence and draws the "
                        "words instead of the scene — use --from-file with "
                        "hand-written prompts instead. gemini rate-limits on "
                        "the free tier and falls back to template silently, "
                        "which quietly half-ruins a film")
    s.add_argument("--scenes", help="scenes you wrote yourself, one per row")
    s.add_argument("--from-file", dest="from_file")
    s.add_argument("--force", action="store_true", help="overwrite existing prompts")

    s = sub.add_parser("charref",
                       help="draw the character sheet an era's look needs")
    s.add_argument("--look", required=True, help="path to the look file")
    s.add_argument("--era", required=True, help="which era in it")
    s.add_argument("--profile", default="acc1")
    s.set_defaults(run=do_charref)

    s = stage("redraw", do_redraw, "draw named rows again, in place")
    s.add_argument("--rows", required=True, help="which rows, e.g. 100,104,114")
    s.add_argument("--profile", default="acc1")
    s.add_argument("--aspect", default="16:9")
    s.add_argument("--batch", type=int, default=50)

    s = stage("images", do_images, "prompts -> the still for every row")
    s.add_argument("--look", default="look.json")
    s.add_argument("--profile", default="acc1")
    s.add_argument("--aspect", default="16:9")
    s.add_argument("--batch", type=int, default=50,
                   help="prompts per round; each round downloads its own zip")
    s.add_argument("--resume", action="store_true",
                   help="keep downloads/ and images_raw/ from an interrupted "
                        "run instead of starting clean")
    s.add_argument("--check", action="store_true")
    s.add_argument("--dump", action="store_true")

    s = stage("repair", do_repair,
              "one bad round's pictures, replaced from a throwaway project")
    s.add_argument("--from", dest="from_id", required=True,
                   help="the project whose images_raw holds the new pictures")
    s.add_argument("--rows", required=True,
                   help="first row to replace, e.g. 151 or 151-197 "
                        "(always runs to the end of the film)")

    s = stage("clips", do_clips, "stills -> clips, through OmniFlows (costs credits)")
    s.add_argument("--aspect", default="landscape",
                   choices=["landscape", "portrait"])
    s.add_argument("--model", default="omniflash", choices=["omniflash", "veo"],
                   help="omniflash: 10s, 3 credits. veo: 8s")
    s.add_argument("--limit", type=int, help="only animate this many rows")
    s.add_argument("--redo", action="store_true", help="animate rows that already have a clip")
    s.add_argument("--dry-run", action="store_true",
                   help="pair everything and stop at the submit button")
    s.add_argument("--collect", action="store_true",
                   help="pick up clips that finished after the run gave up")

    s = stage("render", do_render, "clips + narration -> final.mp4")
    s.add_argument("--aspect", default="16:9")
    s.add_argument("--size")
    s.add_argument("--fit", default="cover", choices=["cover", "contain"])
    s.add_argument("--parallax", action="store_true",
                   help="stills get depth-driven motion instead of a flat "
                        "zoom - a depth model reads each picture once")
    s.add_argument("--jobs", type=int, default=8,
                   help="shots encoded at once; pure parallelism, "
                        "the output is identical either way")
    s.add_argument("--burn-captions", action="store_true")
    s.add_argument("--force", action="store_true", help="rebuild every shot")

    s = stage("publish", do_publish, "final.mp4 -> a numbered folder on D:")
    s.add_argument("--title")
    s.add_argument("--description", default="")
    s.add_argument("--hashtags", default="")
    s.add_argument("--force", action="store_true")
    # Re-publishing a film that is already filed used to walk the number on
    # every time — one film ended up as 105, 106 and 107 in the library while
    # it was being corrected. The number is read off the folder names, so the
    # only way to land on the one it already has is to say it.
    s.add_argument("--number", type=int,
                   help="file it as this number instead of the next free one")

    s = stage("auto", do_auto, "script -> everything, stopping before credits")
    s.add_argument("--script")
    s.add_argument("--voice", default="STIckman Voice")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--key")
    s.add_argument("--clips", type=int)
    s.add_argument("--share", type=float, default=40.0)
    s.add_argument("--clip-seconds", dest="clip_seconds", type=float)
    s.add_argument("--still-seconds", dest="still_seconds", type=float,
                   default=2.3)
    s.add_argument("--placement", default="front", choices=["front", "spread"])
    s.add_argument("--seconds", type=float, default=CLIP_SECONDS)
    s.add_argument("--language")
    s.add_argument("--retranscribe", action="store_true")
    s.add_argument("--mode", default="template",
                   choices=["template", "gemini", "llm"])
    s.add_argument("--look", default="look.json")
    s.add_argument("--scenes")
    s.add_argument("--from-file", dest="from_file")
    s.add_argument("--profile", default="acc1")
    s.add_argument("--batch", type=int, default=50)
    s.add_argument("--resume", action="store_true")
    s.add_argument("--check", action="store_true")
    s.add_argument("--dump", action="store_true")
    s.add_argument("--engine", default="omniflash", choices=["omniflash", "veo"])
    s.add_argument("--aspect", default="landscape",
                   choices=["landscape", "portrait"])
    s.add_argument("--model", default="omniflash", choices=["omniflash", "veo"])
    s.add_argument("--limit", type=int)
    s.add_argument("--redo", action="store_true")
    s.add_argument("--collect", action="store_true")
    s.add_argument("--force", action="store_true")

    s = stage("refill", do_refill,
              "rows whose clip was refused -> several stills instead")
    s.add_argument("--rows", help="only these rows, e.g. 12,40,57")
    s.add_argument("--pictures", type=int, default=4,
                   help="how many pictures to draw per row (default 4)")
    s.add_argument("--stills", action="store_true",
                   help="split long still rows instead of refilling refused "
                        "clips - a sentence with no comma cannot be cut any "
                        "shorter, so its picture is split by the words instead")
    s.add_argument("--longer-than", dest="longer_than", type=float, default=3.6,
                   help="with --stills, which rows count as long (default 3.6s)")
    s.add_argument("--seconds-each", dest="seconds_each", type=float, default=2.4,
                   help="with --stills, roughly how long each picture holds")
    s.add_argument("--profile", default="acc1")
    s.add_argument("--aspect", default="landscape",
                   choices=["landscape", "portrait"])
    s.add_argument("--batch", type=int, default=50)

    s = stage("hook", do_hook, "hook.json + final.mp4 -> final_hook.mp4")
    s.add_argument("--voice", default="STIckman Voice")
    s.add_argument("--speed", type=float, default=1.0)
    s.add_argument("--key")
    s.add_argument("--aspect", default="16:9")
    s.add_argument("--size")
    s.add_argument("--fit", default="cover", choices=["cover", "contain"])
    s.add_argument("--out", default="final_hook.mp4")

    s = stage("cutouts", do_cutouts,
              "things the narration calls missing -> crossed-out overlays")
    s.add_argument("--rows", help="only these rows, e.g. 15,16,17")
    s.add_argument("--height", type=int, default=380,
                   help="overlay height in pixels on a 1080p shot")
    s.add_argument("--profile", default="acc1")
    s.add_argument("--aspect", default="landscape",
                   choices=["landscape", "portrait"])
    s.add_argument("--batch", type=int, default=50)

    s = stage("panels", do_panels,
              "pictures that came back as two pictures, by row")
    s.add_argument("--threshold", type=float, default=0.95,
                   help="how much of the frame's height the divider must run "
                        "down (default 0.95)")

    stage("status", do_status, "what this project has on disk")

    args = ap.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()

"""
ASSEMBLE — the clips cut to their rows and laid over the narration.

The image pipeline invents motion, because a still has none: it zooms and pans
so neighbouring pictures never move the same way. Here the motion is already in
the footage, so this stage does the opposite job — it takes it away where there
is too much and holds it where there is not enough.

A clip comes back a fixed length; a row is however long its sentences took. So
each shot is fitted to its own row:

  * clip longer than the row  -> trimmed, keeping the opening of the move
  * clip shorter than the row -> its last frame held to the end

Holding beats looping and beats slowing down. A loop snaps back to the start
mid-sentence, and slowing a clip to fit reads as slow motion — the same
mistake `omni_videos.py` records having made with 8s clips under 10s lines.

    clips/ + segments.json + voiceover.mp3 -> final.mp4
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import env
from common import log, p, probe, run, save_state

from assemble import (FPS, SIZES, ff_path, still_filter,   # shared with the
                      write_srt)                           # image pipeline

import clips as clipstage
import cutout as cutoutstage
import mix


def extras_for(pid, n):
    """Extra pictures drawn for one row, in order.

    A row can lose its clip — OmniFlows refuses some of them outright, and a
    refused row is usually one of the long ones. Holding a single picture for
    ten seconds is exactly the deadness the still length was tuned to avoid,
    so `refill` draws several pictures for that row and this hands them over
    to be shown in turn.
    """
    d = p(pid, "images_extra")
    if not os.path.isdir(d):
        return []
    # Named by timestamp like the rest of the images, so the row is found by
    # its number inside the name rather than at the front of it.
    tag = f"_i{n:03d}_"
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if tag in f]


def plan_from(pid):
    """Every row paired with the clip that covers it, in film order."""
    with open(p(pid, "segments.json"), encoding="utf-8") as f:
        rows = json.load(f)

    scenes = {}
    scenes_file = p(pid, "scenes.json")
    if os.path.isfile(scenes_file):
        with open(scenes_file, encoding="utf-8") as f:
            for scene in json.load(f).get("scenes", []):
                scenes[scene["scene_number"]] = scene

    # Rows the mix never meant to animate are not missing — they are stills,
    # and they get the image pipeline's slow move instead of a clip. Only a row
    # that was supposed to come back with a clip and did not is a problem.
    wanted = mix.clip_rows(pid, len(rows))
    stills = clipstage.images_in(pid) if wanted != set(r["n"] for r in rows) \
        else []

    plan, missing = [], []
    for i, row in enumerate(rows):
        scene = scenes.get(row["n"], {})
        rel = scene.get("video_path")
        clip = p(pid, rel) if rel else None
        if clip and os.path.isfile(clip):
            plan.append({**row, "clip": clip, "clip_dur": probe(clip),
                         "kind": "clip",
                         "cutouts": cutoutstage.for_row(p(pid, ""), row["n"])})
            continue
        if row["n"] in wanted:
            extra = extras_for(pid, row["n"])
            if extra:
                plan.append({**row, "still": extra[0], "extras": extra[1:],
                             "clip_dur": 0.0, "kind": "still"})
                continue
            # If no extra refill, fall through to use the row's main still image!
        if not stills:
            raise RuntimeError(
                f"row {row['n']} is meant to stay still but project has no images — "
                f"run the images stage first")
        still_idx = min(i, len(stills) - 1)
        extra = extras_for(pid, row["n"])
        plan.append({**row, "still": p(pid, os.path.join("images", stills[still_idx])),
                     "extras": extra, "clip_dur": 0.0, "kind": "still",
                     "cutouts": cutoutstage.for_row(p(pid, ""), row["n"])})

    if missing:
        raise RuntimeError(
            f"{len(missing)} row(s) have no clip yet: {missing[:12]}"
            f"{' …' if len(missing) > 12 else ''} — run the clips stage with "
            f"--collect, or --redo those rows. Rendering now would drop them "
            f"and slide the whole film off its narration.")
    return plan


def build_shot(job):
    """One row's shot — a clip cut to the row, or a still given a slow move."""
    item, out, width, height, fit = job
    if os.path.isfile(out) and os.path.getsize(out) > 5000:
        return stamp_cutouts(item, out)
    # Frames are counted from the row's absolute place in the film, not from
    # its own length. Rounding each shot on its own looks harmless — half a
    # frame at worst — but it accumulates: 179 shots drifted 220ms, which is
    # nothing under a nine-second shot and 40% of a half-second one. Naming a
    # thing and showing it at the same instant only works if the instant does
    # not move.
    frames = item["frames"]
    need = frames / FPS
    have = item["clip_dur"] or 0.0

    if item.get("kind") == "still":
        pics = [item["still"]] + list(item.get("extras") or [])
        if item.get("board"):
            return stamp_cutouts(item, board_shot(item, out, width, height))
        if item.get("parallax") and len(pics) == 1:
            # Depth-driven motion instead of the flat zoom: the near of the
            # picture travels further than the far, which is the only cue that
            # says the shot is a place rather than a photograph being pushed.
            import parallax
            # The depth map goes in its own folder, never beside the picture.
            # Written into images/ it became a file the stills list picked up:
            # `images_in` reads that folder positionally, so 66 grey depth maps
            # shifted every row onto the wrong picture and one of them played
            # in the finished film.
            # build_shot only ever sees the job, so the project is taken from
            # the picture's own path: <project>/images/<name>.png.
            depth_dir = os.path.join(
                os.path.dirname(os.path.dirname(item["still"])), "depth")
            os.makedirs(depth_dir, exist_ok=True)
            cache = os.path.join(
                depth_dir,
                os.path.splitext(os.path.basename(item["still"]))[0] + ".png")
            parallax.clip(pics[0], out, frames / FPS, fps=FPS,
                          size=(width, height), depth_cache=cache,
                          direction=(1.0 if item["n"] % 2 else -1.0,
                                     0.22 if item["n"] % 4 < 2 else -0.22))
            return stamp_cutouts(item, out)
        if len(pics) > 1:
            # Several pictures share the row: split the frames between them so
            # the row still ends exactly where the narration does, and give
            # each its own move so the cuts do not all look the same.
            parts, base = [], os.path.splitext(out)[0]
            each, spare = divmod(frames, len(pics))
            for k, pic in enumerate(pics):
                n_f = each + (1 if k < spare else 0)
                if n_f < 1:
                    continue
                sub = f"{base}_{k:02d}.mp4"
                run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-i", pic, "-an",
                     "-vf", still_filter(n_f / FPS, width, height,
                                         item["n"] + k, fit),
                     "-frames:v", str(n_f), "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "20", sub])
                parts.append(sub)
            lst = f"{base}_parts.txt"
            with open(lst, "w", encoding="utf-8") as f:
                for s in parts:
                    f.write(_concat_line(s))
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
            for s in parts + [lst]:
                os.remove(s)
            return stamp_cutouts(item, out)

        # The image pipeline's own move, and its own reason for existing: a
        # still with no move at all sits dead between two shots that do move.
        # The row number picks the direction, so neighbours never match.
        vf = still_filter(need, width, height, item["n"], fit)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", item["still"], "-an", "-vf", vf,
               "-frames:v", str(frames),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out]
        run(cmd)
        return stamp_cutouts(item, out)

    scale = ("increase" if fit == "cover" else "decrease")
    steps = [f"scale={width}:{height}:force_original_aspect_ratio={scale}"]
    if fit == "cover":
        steps.append(f"crop={width}:{height}")
    else:
        steps.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")

    # tpad clones the final frame. It has to come before fps so the held
    # section is generated at the same rate as the rest of the shot.
    if have and have < need - 0.04:
        steps.append(f"tpad=stop_mode=clone:stop_duration={need - have:.3f}")
    steps += [f"fps={FPS}", "setsar=1", "format=yuv420p"]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", item["clip"], "-an", "-vf", ",".join(steps),
           # The frame count is what makes the row exact; -t would leave the
           # last frame's length up to the source's timebase.
           "-frames:v", str(frames),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out]
    run(cmd)
    return stamp_cutouts(item, out)


def _file_sig(path):
    try:
        st = os.stat(path)
        return [os.path.abspath(path), st.st_size, int(st.st_mtime)]
    except OSError:
        return [path, 0, 0]


def shot_sig(item, width, height, fit):
    """Everything a shot is made from. A shot on disk with the same signature
    is the same shot, so a render that stopped half-way picks up where it was
    instead of drawing all of them again."""
    board_spec = item.get("board") or {}
    return json.dumps({
        "v": 1, "n": item.get("n"), "kind": item.get("kind"),
        "frames": item.get("frames"), "size": [width, height], "fit": fit,
        "parallax": bool(item.get("parallax")),
        "still": _file_sig(item["still"]) if item.get("still") else None,
        "extras": [_file_sig(x) for x in (item.get("extras") or [])],
        "clip": _file_sig(item["clip"]) if item.get("clip") else None,
        "cutouts": [_file_sig(x) for x in (item.get("cutouts") or [])],
        "board": [[pt.get("card"), pt.get("frames"), pt.get("x"), pt.get("y")]
                  for pt in board_spec.get("parts") or []],
    }, sort_keys=True, default=str)


def build_shot_cached(job):
    """build_shot, but a finished shot with a matching signature is kept.

    The shot is made under a side name and renamed only when whole, so a
    render killed mid-shot never leaves a half-written file that looks done.
    A kept shot is not stamped again: its crosses are already on it."""
    item, out, width, height, fit = job
    sig = shot_sig(item, width, height, fit)
    sig_file = os.path.splitext(out)[0] + ".sig"
    try:
        if (os.path.isfile(out) and os.path.getsize(out) > 5000
                and open(sig_file, encoding="utf-8").read() == sig):
            return out
    except OSError:
        pass
    for stale in (out, sig_file):
        if os.path.exists(stale):
            os.remove(stale)
    part = os.path.splitext(out)[0] + ".part.mp4"
    if os.path.exists(part):
        os.remove(part)
    build_shot((item, part, width, height, fit))
    os.replace(part, out)
    with open(sig_file, "w", encoding="utf-8") as f:
        f.write(sig)
    return out


def board_shot(item, out, width, height):
    """A Vox board shot: each picture of the row slides in as a cut-out card.

    The row's frames are shared between its pictures exactly as a plain still
    shares them, so the shot still ends where the narration does."""
    import board
    spec = item["board"]
    parts = spec["parts"]
    if len(parts) == 1:
        pt = parts[0]
        return board.render_card(spec["png"], pt["card"], out, pt["frames"],
                                 width, height, FPS, pt["x"], pt["y"])
    base = os.path.splitext(out)[0]
    subs = []
    for k, pt in enumerate(parts):
        sub = f"{base}_{k:02d}.mp4"
        board.render_card(spec["png"], pt["card"], sub, pt["frames"],
                          width, height, FPS, pt["x"], pt["y"])
        subs.append(sub)
    lst = f"{base}_parts.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for sub in subs:
            f.write(_concat_line(sub))
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    for f in subs + [lst]:
        os.remove(f)
    return out


def prepare_board(pid, plan, width, height):
    """Cards for every still and all their slides, before any shot renders.

    Returns how many cards were made. Every slide comes back in one request,
    so the render never waits on the network per shot."""
    import board
    b = board.style(width)
    folder = p(pid, "")
    png = board.board_for(folder, width, height, b)
    parts_all, idx = [], 0
    for item in plan:
        if item.get("kind") != "still":
            continue
        pics = [item["still"]] + list(item.get("extras") or [])
        each, spare = divmod(item["frames"], len(pics))
        parts = []
        for k, pic in enumerate(pics):
            n_f = each + (1 if k < spare else 0)
            if n_f < 1:
                continue
            card, (cw, ch) = board.card_for(folder, pic, width, height, b, idx)
            parts.append({"card": card, "frames": n_f, "cw": cw, "ch": ch, "idx": idx})
            idx += 1
        item["board"] = {"png": png, "parts": parts}
        parts_all += parts
    if parts_all:
        exprs = board.slides([(pt["idx"], width, height, pt["cw"], pt["ch"], pt["frames"])
                              for pt in parts_all])
        for pt, (x, y) in zip(parts_all, exprs):
            pt["x"], pt["y"] = x, y
    return len(parts_all)


def stamp_cutouts(item, shot):
    """Lay this row's crossed-out things over the finished shot.

    The narration naming a thing it says is absent — "no supermarket ten
    kilometres away" — reads as an empty field unless the thing is on the
    screen with a line through it. The overlay is already keyed and crossed;
    all that is left is deciding where on this shot it covers the least.
    """
    overlays = item.get("cutouts") or []
    if not overlays:
        return shot

    from PIL import Image
    probe_png = os.path.splitext(shot)[0] + "_f.png"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", shot, "-frames:v", "1", probe_png])

    args, filters, last = [], [], "0:v"
    for k, ov in enumerate(overlays):
        with Image.open(ov) as im:
            size = im.size
        x, y = cutoutstage.place(probe_png, size)
        args += ["-i", ov]
        nxt = f"v{k}"
        filters.append(f"[{last}][{k + 1}:v]overlay={x}:{y}[{nxt}]")
        last = nxt

    out = os.path.splitext(shot)[0] + "_c.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", shot]
        + args + ["-filter_complex", ";".join(filters), "-map", f"[{last}]",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out])
    os.remove(probe_png)
    os.replace(out, shot)
    return shot


class _RenderTurn:
    """One render on the PC at a time. With the queue running films in
    parallel, two renders at once would each take every core and the PC with
    them; the second waits here until the first is done. The lock belongs to
    the process, so a render that dies lets go of it with the process."""

    def __init__(self, pid):
        projects = os.path.dirname(os.path.dirname(os.path.abspath(p(pid, "segments.json"))))
        self.path = os.path.join(projects, ".render.lock")
        self.f = None

    def __enter__(self):
        import time
        self.f = open(self.path, "a+")
        told = False
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if not told:
                    log("render", "another video is rendering - this one starts when it is done")
                    told = True
                time.sleep(5)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            self.f.close()
        except OSError:
            pass
        return False


def render(pid, aspect="16:9", size=None, fit="cover", jobs=4,
           burn_captions=True, force=False, parallax=False):
    """The whole stage: clips + narration in, `final.mp4` out."""
    with _RenderTurn(pid):
        return _render(pid, aspect, size, fit, jobs, burn_captions, force, parallax)


def _concat_line(path):
    """One line of an ffmpeg concat list; a ' in the path must be escaped."""
    q = os.path.abspath(path).replace(chr(92), "/").replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))
    return "file " + chr(39) + q + chr(39) + chr(10)


def _render(pid, aspect="16:9", size=None, fit="cover", jobs=4,
            burn_captions=True, force=False, parallax=False):
    audio = p(pid, "voiceover.mp3")
    if not os.path.isfile(audio):
        raise FileNotFoundError(f"{audio} — the cut is built against the narration")

    plan = plan_from(pid)
    write_srt(plan, p(pid, "captions.srt"))

    width, height = (tuple(int(x) for x in size.lower().split("x")) if size
                     else SIZES.get(aspect, SIZES["16:9"]))
    shots_dir = p(pid, "shots")
    os.makedirs(shots_dir, exist_ok=True)
    if force:
        for name in os.listdir(shots_dir):
            os.remove(os.path.join(shots_dir, name))

    # One pass over the whole film first, so every shot knows exactly which
    # frames it owns. Shot k runs from frame round(start*FPS) to the frame the
    # next shot starts on, which makes each boundary land where the narration
    # put it however many shots came before.
    edges = [int(round(i["start"] * FPS)) for i in plan]
    edges.append(int(round(plan[-1]["end"] * FPS)))
    for k, item in enumerate(plan):
        item["frames"] = max(edges[k + 1] - edges[k], 1)
        item["parallax"] = parallax

    # Vox style: the category says so in the project's look.json.
    look = {}
    try:
        with open(p(pid, "look.json"), encoding="utf-8") as f:
            look = json.load(f) or {}
    except (OSError, ValueError):
        pass
    board_cards = 0
    if look.get("motion") == "board":
        board_cards = prepare_board(pid, plan, width, height)

    queue = [(item, os.path.join(shots_dir, f"{item['n']:04d}.mp4"),
              width, height, fit) for item in plan]
    moving = [i for i in plan if i.get("kind") != "still"]
    stills = len(plan) - len(moving)
    held = sum(1 for i in moving if i["clip_dur"] < i["dur"] - 0.04)
    trimmed = sum(1 for i in moving if i["clip_dur"] > i["dur"] + 0.04)
    log("render", f"{len(queue)} shots at {width}x{height} ({fit}), {jobs} at a time")
    log("render", f"  {len(moving)} clip(s): {trimmed} trimmed to their row, "
                  f"{held} held on the last frame")
    if stills:
        log("render", f"  {stills} still(s) given "
                      + (f"the Vox board ({board_cards} cut-out cards)" if board_cards
                         else "depth parallax" if parallax else "a slow move"))

    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for _ in pool.map(build_shot_cached, queue):
            done += 1
            if done % 10 == 0 or done == len(queue):
                log("render", f"  {done}/{len(queue)} shots")

    list_file = p(pid, "shots.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for job in queue:
            f.write(_concat_line(job[1]))

    out = p(pid, "final.mp4")
    bgm_file = p(pid, "bgm.mp3")
    if not os.path.isfile(bgm_file):
        bgm_file = p(pid, "bgm.wav")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", list_file, "-i", audio]
    if os.path.isfile(bgm_file):
        a_len = probe(audio)
        fade_out_st = max(0, a_len - 4)
        cmd += ["-i", bgm_file,
                "-filter_complex",
                f"[2:a]volume=0.11,afade=t=in:st=0:d=2,afade=t=out:st={fade_out_st:.2f}:d=4[bgm];"
                f"[bgm][1:a]sidechaincompress=threshold=0.08:ratio=3:attack=100:release=400[ducked];"
                f"[1:a][ducked]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]"]
        log("render", f"  mixed background music: {os.path.basename(bgm_file)} (dynamic ducking, volume=0.11)")

    # Animated captions chosen on the Production tab. Off by default; a
    # failure here never costs the film, it renders without them.
    styled = None
    try:
        import captions as capstage
        styled = capstage.ass_for(p(pid, ""), width, height)
        if styled:
            cs = capstage.load_settings()
            log("render", f"  burning captions: {capstage.STYLES[cs['style']][0]}, "
                          f"{cs['color']}, {cs['position']}, {cs['size']}")
    except Exception as exc:
        log("render", f"  captions skipped ({exc}) - rendering without them")
        styled = None

    if styled:
        cmd += ["-vf", capstage.ffmpeg_filter(styled, ff_path),
                "-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    elif burn_captions:
        cmd += ["-vf", f"subtitles='{ff_path(p(pid, 'captions.srt'))}'"
                       ":force_style='FontName=Arial,FontSize=14,Outline=2,Alignment=2,MarginV=15'",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    else:
        cmd += ["-c:v", "copy"]
    # Written beside the final name and swapped in whole. ffmpeg writing
    # straight into final.mp4 left a half-made file on disk for the whole
    # encode: the Deliverables page listed it, it would not play, and a
    # re-render wiped the previous good film before the new one existed.
    part = p(pid, "final.part.mp4")
    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", part]
    run(cmd)
    os.replace(part, out)

    length = probe(out)
    save_state(pid, final=out, final_seconds=round(length, 2), shots=len(plan))
    log("render", f"{out} — {length:.1f}s ({length/60:.1f} min), "
                  f"{len(moving)} clip(s) + {stills} still(s)")
    if abs(length - probe(audio)) > 1.5:
        log("render", f"  note: narration is {probe(audio):.1f}s, the cut is "
                      f"{length:.1f}s — check the last shot")
    return out

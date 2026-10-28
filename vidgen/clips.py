"""
CLIPS — every row's still turned into a moving shot, through OmniFlows.

The picture is already right by the time this stage runs: the image pipeline's
Gemini app drew it from the row's own words, with the character reference
attached, and it costs nothing. So this stage does not draw anything. It hands
those stills to OmniFlows' **Bulk Image to Video** tool, which animates each one
against a motion line and gives back a clip.

Doing it that way is what keeps the character from drifting. Text-to-video would
invent the stickman afresh for every shot; image-to-video can only move what is
already in the frame.

    images/ + segments.json -> scenes.json -> clips/scene_NN.mp4

`omni_videos.py` does the browser work — it is the script that already knows
this tool, including the two things that have cost real generations: the panel
keeps the previous batch's uploads unless the page is reloaded, and the submit
button prints the number of image-prompt pairs it can see, which is the only
honest check that nothing shifted by one.
"""
import json
import os
import subprocess
import sys

import env
from env import AI_PIPE, CREDITS_PER_CLIP
from common import log, natural_key, p, probe, save_state

import mix

# The doc's hard-won warning: asking for "gestures, expressions" in a frame with
# nobody in it makes the model invent people. Anything this stage writes by
# itself only ever moves what the still already contains.
DEFAULT_MOTION = ("Animate only what is already in this picture. Slow, gentle "
                  "camera push with subtle natural movement. Do not add people, "
                  "objects, text or captions that are not already in the frame.")

MODELS = {
    # label substring omni_videos matches on -> what it gives back
    "omniflash": ("OmniFlash", 10.0),
    "veo": ("Google Flow", 8.0),
}


def images_in(pid):
    """The row images in row order — the file names sort into the film."""
    folder = p(pid, "images")
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"{folder} — run the images stage first")
    names = [n for n in os.listdir(folder)
             if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    return sorted(names, key=natural_key)


def load_rows(pid):
    path = p(pid, "segments.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} — run the rows stage first")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def motion_lines(pid, count):
    """
    One motion instruction per row, from `motions.txt` when it exists.

    Written by hand or by the prompts stage; a missing file means every clip
    gets the cautious default, which is a real film, just a calmer one.
    """
    path = p(pid, "motions.txt")
    if not os.path.isfile(path):
        log("clips", f"no motions.txt — every clip gets the default motion")
        return [DEFAULT_MOTION] * count
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if len(lines) != count:
        raise RuntimeError(f"motions.txt has {len(lines)} lines but there are "
                           f"{count} rows — one motion line per row, or delete "
                           f"the file to use the default")
    log("clips", f"motions.txt — {len(lines)} motion lines")
    return lines


def build_scenes(pid, aspect="landscape"):
    """
    `scenes.json` in the shape `omni_videos.py` reads.

    One scene per row, in row order, each naming the still that belongs to it.
    The pairing is the whole game here: the tool matches uploads to prompts by
    position, so a row whose image is missing would slide every later clip onto
    the wrong words. Rather than skip it quietly, this refuses.
    """
    rows = load_rows(pid)
    names = images_in(pid)
    if len(names) != len(rows):
        raise RuntimeError(f"{len(names)} images but {len(rows)} rows — the "
                           f"clip for each row comes from that row's own "
                           f"picture, so these have to match")

    motions = motion_lines(pid, len(rows))
    prompts_path = p(pid, "prompts.txt")
    prompts = []
    if os.path.isfile(prompts_path):
        with open(prompts_path, encoding="utf-8") as f:
            prompts = [line.strip() for line in f if line.strip()]

    # Not every row has to move. `mix.json` says which ones buy a clip; the
    # rest keep their still and are given a slow move at render time. Every row
    # still needs its picture — that is why the count check above is on all of
    # them — but only these are uploaded, and only these are paid for.
    wanted = mix.clip_rows(pid, len(rows))

    scenes = []
    for i, row in enumerate(rows):
        if row["n"] not in wanted:
            continue
        scenes.append({
            "scene_number": row["n"],
            "image_path": os.path.join("images", names[i]),
            "scene": prompts[i] if i < len(prompts) else row["text"],
            "video_instruction": motions[i],
            # What the row needs the clip to cover, so the stitcher can check
            # what it actually got against what it asked for.
            "row_seconds": row["dur"],
            "row_start": row["start"],
        })

    data = {"id": pid, "aspect_ratio": aspect, "scenes": scenes}
    path = p(pid, "scenes.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    log("clips", f"{path} — {len(scenes)} scene(s) paired with their images"
                 f" of {len(rows)} row(s); the rest stay still")
    return path, data


def run(pid, aspect="landscape", model="omniflash", limit=None, redo=False,
        dry_run=False, collect_only=False):
    """The whole stage: stills in, `clips/scene_NN.mp4` out."""
    path, data = build_scenes(pid, aspect)
    total = len(data["scenes"])
    todo = total if (redo or collect_only) else sum(
        1 for s in data["scenes"] if not s.get("video_path"))
    if limit:
        todo = min(todo, limit)

    label, seconds = MODELS.get(model, MODELS["omniflash"])
    if not collect_only:
        log("clips", f"{todo} clip(s) to buy on {label} "
                     f"— about {todo * CREDITS_PER_CLIP} credits, "
                     f"{seconds:.0f}s each")

    cmd = [sys.executable, "-u", os.path.join(AI_PIPE, "omni_videos.py"), path,
           "--aspect", aspect]
    if collect_only:
        cmd.append("--collect")
    else:
        cmd += ["--model", label]
        if redo:
            cmd.append("--redo")
        if limit:
            cmd += ["--limit", str(limit)]
        if dry_run:
            cmd.append("--dry-run")

    log("clips", "  " + " ".join(cmd[2:]))
    # Run it where it lives: it reaches for its own siblings and its browser
    # profile by relative path.
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    code = subprocess.call(cmd, cwd=AI_PIPE, creationflags=flags)
    if code != 0:
        raise RuntimeError(f"omni_videos.py exited {code} — the browser was "
                           f"left open, look at what it is showing")

    return record(pid)


def record(pid):
    """Read back which rows now have a clip, and how long each one really is."""
    path = p(pid, "scenes.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    have, missing, lengths = [], [], {}
    for scene in data.get("scenes", []):
        rel = scene.get("video_path")
        full = p(pid, rel) if rel else None
        if full and os.path.isfile(full):
            have.append(scene["scene_number"])
            lengths[str(scene["scene_number"])] = round(probe(full), 3)
        else:
            missing.append(scene["scene_number"])

    save_state(pid, clips_done=len(have), clips_missing=len(missing),
               clip_lengths=lengths)
    log("clips", f"{len(have)} clip(s) on disk, {len(missing)} still missing")
    if missing:
        log("clips", f"  missing rows: {missing[:12]}"
                     f"{' …' if len(missing) > 12 else ''}")
        log("clips", "  they may still be rendering — re-run with --collect "
                     "later today (the media expire the same day)")
    return have, missing

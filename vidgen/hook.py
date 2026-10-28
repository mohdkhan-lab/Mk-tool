"""
HOOK — the payoff shown first, bolted onto the front of a finished cut.

A documentary that opens quietly earns its ending and loses the scroll. The
genre's fix is to show the worst of it up front: a few escalating lines over
the film's own strongest pictures, and then drop into the calm opening that
explains how it started.

No new footage is drawn. The hook reuses shots that are already in the film,
which is what a cold open is — a flash of what is coming, shown properly
later. Only the narration is new, so it costs a few TTS lines and nothing
else. The last line is the only one that says what the video is about.

    hook.json + final.mp4 -> final_hook.mp4
"""
import json
import os
import shutil

import env
from common import log, p, probe, run

from assemble import FPS, SIZES, still_filter


def load_lines(pid):
    """The hook as written: a line and the row whose picture carries it."""
    path = p(pid, "hook.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{path} — write the hook first: a list of "
            '{"line": "...", "row": N} in the order they should play')
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not data:
        raise ValueError(f"{path} is empty")
    for i, item in enumerate(data, 1):
        if not item.get("line") or not item.get("row"):
            raise ValueError(f"{path}: entry {i} needs both a line and a row")
    return data


def say(pid, lines, voice_name, api_key, speed=1.0):
    """Each hook line spoken on its own, so its length is known exactly."""
    import voice as voicestage
    out = []
    for i, item in enumerate(lines, 1):
        scratch = f"_hook_{pid}_{i}".replace(" ", "_")
        d = p(scratch, "")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "script.txt"), "w", encoding="utf-8") as f:
            f.write(item["line"] + "\n")
        voicestage.generate(scratch, os.path.join(d, "script.txt"),
                            voice_name, api_key, speed=speed)
        mp3 = p(scratch, "voiceover.mp3")
        keep = p(pid, "hook_parts", f"{i:02d}.mp3")
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.copy(mp3, keep)
        shutil.rmtree(d, ignore_errors=True)
        out.append((item, keep, probe(keep)))
        log("hook", f"  {i}. {probe(keep):.1f}s  {item['line'][:60]}")
    return out


def picture_for(pid, row_n):
    """The film's own picture for a row — its clip if it has one, else its still."""
    scenes = {}
    sf = p(pid, "scenes.json")
    if os.path.isfile(sf):
        with open(sf, encoding="utf-8") as f:
            for s in json.load(f).get("scenes", []):
                scenes[s["scene_number"]] = s
    rel = (scenes.get(row_n) or {}).get("video_path")
    if rel and os.path.isfile(p(pid, rel)):
        return p(pid, rel), "clip"

    import clips as clipstage
    stills = clipstage.images_in(pid)
    if row_n - 1 < len(stills):
        return p(pid, os.path.join("images", stills[row_n - 1])), "still"
    raise FileNotFoundError(f"row {row_n} has neither a clip nor a picture")


def build(pid, voice_name, api_key, speed=1.0, aspect="16:9", size=None,
          fit="cover", out_name="final_hook.mp4"):
    """The whole stage: hook.json + final.mp4 in, a hooked cut out."""
    film = p(pid, "final.mp4")
    if not os.path.isfile(film):
        raise FileNotFoundError(f"{film} — render the film before hooking it")

    lines = load_lines(pid)
    log("hook", f"{len(lines)} line(s) over the film's own shots")
    spoken = say(pid, lines, voice_name, api_key, speed)

    width, height = (tuple(int(x) for x in size.lower().split("x")) if size
                     else SIZES.get(aspect, SIZES["16:9"]))
    shots_dir = p(pid, "hook_shots")
    shutil.rmtree(shots_dir, ignore_errors=True)
    os.makedirs(shots_dir, exist_ok=True)

    parts = []
    for i, (item, mp3, secs) in enumerate(spoken, 1):
        src, kind = picture_for(pid, item["row"])
        frames = max(int(round(secs * FPS)), 2)
        out = os.path.join(shots_dir, f"{i:02d}.mp4")
        if kind == "still":
            vf = still_filter(frames / FPS, width, height, item["row"], fit)
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", src, "-an", "-vf", vf, "-frames:v", str(frames),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out])
        else:
            scale = "increase" if fit == "cover" else "decrease"
            steps = [f"scale={width}:{height}:force_original_aspect_ratio={scale}"]
            steps.append(f"crop={width}:{height}" if fit == "cover"
                         else f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
            have = probe(src) or 0.0
            # A hook line usually runs longer than the clip it plays over, and
            # a loop snapping back mid-sentence is the one thing an opening
            # cannot afford. Hold the last frame instead, as the render does.
            if have and have < frames / FPS - 0.04:
                steps.append("tpad=stop_mode=clone:stop_duration="
                             f"{frames / FPS - have:.3f}")
            steps += [f"fps={FPS}", "setsar=1", "format=yuv420p"]
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", src, "-an", "-vf", ",".join(steps),
                 "-frames:v", str(frames),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", out])
        parts.append((out, mp3))
        log("hook", f"  shot {i} from row {item['row']} ({kind})")

    # The hook's own audio, laid end to end in the same order as its shots.
    concat_v = p(pid, "hook_v.txt")
    concat_a = p(pid, "hook_a.txt")
    for path, items in ((concat_v, [x[0] for x in parts]),
                        (concat_a, [x[1] for x in parts])):
        with open(path, "w", encoding="utf-8") as f:
            for s in items:
                f.write("file " + chr(39) + os.path.abspath(s).replace(chr(92), "/")
                        + chr(39) + chr(10))

    hook_mp4 = p(pid, "hook.mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", concat_v,
         "-f", "concat", "-safe", "0", "-i", concat_a,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
         "-movflags", "+faststart", hook_mp4])

    # Joined through the concat *demuxer*, not the concat filter. The filter
    # produced a file whose pictures were right and whose index was not: it
    # played the hook and then the film, and reported the film's own length
    # and frame count. A remux could not repair that, because the index it
    # copied was already wrong. The demuxer builds one from the packets it
    # actually writes.
    joined = p(pid, out_name)
    lst = p(pid, "join.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for s_ in (hook_mp4, film):
            f.write("file " + chr(39) + os.path.abspath(s_).replace(chr(92), "/")
                    + chr(39) + chr(10))
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", lst,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", joined])
    os.remove(lst)

    log("hook", f"{joined} — {probe(joined):.1f}s "
                f"({probe(hook_mp4):.1f}s hook + {probe(film):.1f}s film)")
    return joined

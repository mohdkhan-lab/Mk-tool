"""
STEP 5 — the images laid back onto the voiceover.

The zip comes out of the generator in prompt order, and prompt order is row
order, so image k belongs to row k and holds for exactly that row's span. That
is the whole edit: no cut is guessed, every one of them was measured off the
narration in step 2.

Images are renamed to where they land — `03-12.45_i047.png` — before anything
is rendered. A folder of `image_47.png` cannot be reviewed against a script; a
folder sorted by timestamp can, and the name says which second to listen to.

A held image is a dead frame, so each one gets a slow move — in, right, out,
left, up, down, taken in turn, so neighbouring images never do the same thing
and a ten-minute film does not spend all of it breathing in and out.

    python pipeline.py render mystory
    python pipeline.py render mystory --burn-captions --jobs 6
"""
import json
import os
import re
import shutil
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

from common import log, natural_key, p, probe, run, slug_ts

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
FPS = 30
SIZES = {
    "16:9": (1920, 1080), "landscape": (1920, 1080), "horizontal": (1920, 1080), "16/9": (1920, 1080),
    "9:16": (1080, 1920), "portrait": (1080, 1920), "vertical": (1080, 1920), "9/16": (1080, 1920), "shorts": (1080, 1920), "reels": (1080, 1920),
    "1:1": (1440, 1440), "square": (1440, 1440), "1/1": (1440, 1440),
    "4:3": (1440, 1080), "3:4": (1080, 1440)
}


# ------------------------------------------------------------------ the zips

def unpack(pid, force=False):
    """Every zip in `downloads/` flattened into `images_raw/`, in order."""
    raw = p(pid, "images_raw")
    if force and os.path.isdir(raw):
        shutil.rmtree(raw)
    os.makedirs(raw, exist_ok=True)

    downloads = p(pid, "downloads")
    zips = sorted(
        (os.path.join(downloads, f) for f in os.listdir(downloads)
         if f.lower().endswith(".zip")), key=natural_key) if os.path.isdir(downloads) else []
    if not zips:
        raise FileNotFoundError(f"no zip in {downloads} — run the images stage first")

    files = []
    for i, archive in enumerate(zips, 1):
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(IMAGE_EXT)]
            members.sort(key=natural_key)
            for member in members:
                # Prefixed by zip so several rounds keep their order, and
                # flattened so a folder inside the zip cannot reorder anything.
                out = os.path.join(raw, f"{i:02d}_{os.path.basename(member)}")
                if not os.path.isfile(out):
                    with zf.open(member) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                files.append(out)
        log("render", f"{os.path.basename(archive)} — {len(members)} images")
    return sorted(files, key=natural_key)


def sorted_raw(directory):
    """The unpacked pictures in the order the rounds produced them."""
    if not os.path.isdir(directory):
        return []
    return sorted((os.path.join(directory, f) for f in os.listdir(directory)
                   if f.lower().endswith(IMAGE_EXT)), key=natural_key)


def replace_tail(raw_dir, keep, fresh):
    """Keep the good pictures, put fresh ones after them, drop the rest.

    Renumbered into one flat sequence rather than left under their old round
    prefixes: the whole pipeline reads this folder in sorted order, and a
    refill whose names sort before the pictures it replaces would reorder the
    film instead of repairing it.
    """
    keep = list(keep)
    staged = []
    for i, src in enumerate(keep + list(fresh), 1):
        staged.append((f"{i:04d}_r{os.path.splitext(src)[1]}", src))

    tmp = raw_dir + ".refill"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    for name, src in staged:
        shutil.copyfile(src, os.path.join(tmp, name))
    shutil.rmtree(raw_dir, ignore_errors=True)
    os.replace(tmp, raw_dir)
    return sorted_raw(raw_dir)


def _by_prompt_number(files, count):
    """`files` placed at the slot their prompt number names, gaps filled from
    the nearest earlier (else later) picture — or None when the names do not
    carry a usable prompt number (then nothing is assumed)."""
    slots = {}
    for path_ in files:
        stem = os.path.splitext(os.path.basename(path_))[0]
        tail = stem.split("_", 1)[1] if "_" in stem else stem
        digits = re.sub(r"\D", "", tail)
        if not digits:
            return None
        n = int(digits)
        if not 1 <= n <= count or n in slots:
            return None
        slots[n] = path_
    if not slots or len(slots) == count:
        return None
    out = []
    for i in range(1, count + 1):
        if i in slots:
            out.append(slots[i])
            continue
        near = [k for k in range(i - 1, 0, -1) if k in slots] or \
               [k for k in range(i + 1, count + 1) if k in slots]
        out.append(slots[near[0]])
    return out


def name_by_rounds(pid, images, rows, plan):
    """Copies named for where they land, paired by the map the rounds recorded.

    Once the character reference is chosen per shot, the image rounds can no
    longer follow row order — they are grouped by which character they draw, so
    a round holds rows from all over the film. Position tells you nothing about
    which row a picture belongs to any more, so `rounds.json` says outright.

    This is stricter than pairing by position was, not looser. Position could
    only ever check a total; here every round's own count is checked against
    what it was asked for, and a round that came back short is named instead of
    sliding every later picture onto the wrong words.
    """
    out_dir = p(pid, "images")
    os.makedirs(out_dir, exist_ok=True)

    # `unpack` prefixes each file with the zip it came out of, in order, and
    # sorts naturally inside it — so the groups are the rounds, in round order.
    groups = {}
    for path_ in images:
        head = os.path.basename(path_).split("_", 1)[0]
        groups.setdefault(int(head) if head.isdigit() else 0, []).append(path_)
    got = [groups[k] for k in sorted(groups)]

    if len(got) != len(plan):
        raise ValueError(
            f"{len(got)} round(s) of pictures came back for {len(plan)} "
            "planned. A zip is missing from downloads/ — re-run the images "
            "stage with --resume so the rounds that finished are kept.")

    pairs = {}
    for entry, files in zip(plan, got):
        want = entry["rows"]
        if len(files) > len(want):
            files = files[-len(want):]
        elif len(files) < len(want):
            numbered = _by_prompt_number(files, len(want))
            if numbered:
                # The app numbers each picture by its prompt (001, 002, ...).
                # When some are missing, the numbers say which: every picture
                # stays on its own row and only the gaps borrow a neighbour,
                # instead of every later picture sliding up onto the wrong words.
                log("bulk", f"round {entry['round']} returned {len(files)}/{len(want)} images — "
                            "paired by prompt number; missing slots reuse the nearest picture")
                files = numbered
            else:
                log("bulk", f"round {entry['round']} returned {len(files)}/{len(want)} images — padding missing slots with last image")
                while len(files) < len(want):
                    files.append(files[-1] if files else "")
        for n, src_path in zip(want, files):
            pairs[n] = src_path

    missing = [r["n"] for r in rows if r["n"] not in pairs]
    if missing:
        raise ValueError(f"{len(missing)} row(s) have no picture: {missing[:20]}")

    plan_out = []
    last_valid_src = None
    for row in rows:
        src = pairs[row["n"]]
        if os.path.isfile(src) and (os.path.getsize(src) == 39370 or os.path.getsize(src) < 15000):
            if last_valid_src:
                src = last_valid_src
        elif os.path.isfile(src) and os.path.getsize(src) > 39370:
            last_valid_src = src

        name = f"{slug_ts(row['start'])}_i{row['n']:03d}{os.path.splitext(src)[1]}"
        dst = os.path.join(out_dir, name)
        # Always overwrite if destination is blank or does not exist
        if not os.path.isfile(dst) or os.path.getsize(dst) == 39370 or os.path.getsize(dst) < 15000:
            shutil.copyfile(src, dst)
        plan_out.append({"n": row["n"], "start": row["start"], "end": row["end"],
                         "dur": round(row["end"] - row["start"], 3),
                         "image": dst, "source": os.path.basename(src),
                         "text": row["text"]})

    with open(p(pid, "edit.json"), "w", encoding="utf-8") as f:
        json.dump(plan_out, f, indent=1, ensure_ascii=False)
    distinct = len({e["source"] for e in plan_out})
    log("render", f"{out_dir} — {len(plan_out)} images named by timestamp, "
                  f"{distinct} distinct")
    return plan_out


# How short the picture set may run before spreading it stops being a repair.
# A handful of tiles lost out of four hundred, spread over the whole timeline,
# is invisible. A quarter of them missing is not a film with a few doubles in
# it — it is the same picture held over four rows at a time, which is what
# "spreading" quietly becomes once the shortfall is large.
SPREAD_FLOOR = 0.98


def name_by_time(pid, images, rows):
    """
    Copies named for where they land, and the row-to-image map.

    A *small* shortfall is not fatal — those images are spread across the whole
    timeline so the film still covers the narration end to end rather than
    running out of picture halfway. A large one is refused, because spreading
    it produces a film that passes every count and shows nothing new for
    seconds at a time. Pictures are free; the answer to a short set is always
    to draw the missing ones, never to stretch the ones that arrived.
    """
    out_dir = p(pid, "images")
    os.makedirs(out_dir, exist_ok=True)
    if images and len(images) < len(rows) * SPREAD_FLOOR:
        # This shipped. A 408-row film came back with 100 distinct pictures and
        # was spread to fill: 308 of its 408 rows showed the same picture as
        # the row before, 30 stills ran past three seconds, the longest held
        # 10.8s — and the only thing said about it was one line in this log.
        short = len(rows) - len(images)
        raise ValueError(
            f"{len(images)} images for {len(rows)} rows — {short} rows would "
            f"have to reuse a picture, about one row in "
            f"{max(1, len(rows) // max(1, short))}. Spreading that is not a "
            "film, it is the same drawing held over several lines at a time. "
            "Draw the missing ones instead:\n"
            f"    python pipeline.py refill {pid} --rows <the missing ones>\n"
            "Check downloads/ for a round that failed or a zip that never "
            "arrived. Pictures cost nothing.")
    if len(images) < len(rows):
        log("render", f"{len(images)} images for {len(rows)} rows — spreading them "
                      "across the timeline")
    if len(images) > len(rows):
        # Spreading is a kindness when pictures ran short. Applied to a
        # surplus it is a lie: the extra files get skipped at even intervals
        # and every row after the first skip quietly shows the next row's
        # picture. That is how a round which re-downloaded the previous
        # round's grid produced a full-looking set of 197 images with a drift
        # of +1 from row 67 and +2 from row 133 — nothing was missing, so
        # nothing complained. More pictures than rows means the rounds did not
        # answer the prompts, and that is worth stopping for.
        raise ValueError(
            f"{len(images)} images for {len(rows)} rows — there are more "
            "pictures than shots, so the rounds do not line up with the "
            "prompts. Check downloads/ for a repeated zip; refill the round "
            "that failed rather than spreading these.")

    plan = []
    for i, row in enumerate(rows):
        src = images[min(i * len(images) // len(rows), len(images) - 1)]
        name = f"{slug_ts(row['start'])}_i{row['n']:03d}{os.path.splitext(src)[1]}"
        dst = os.path.join(out_dir, name)
        if not os.path.isfile(dst):
            shutil.copyfile(src, dst)
        plan.append({"n": row["n"], "start": row["start"], "end": row["end"],
                     "dur": round(row["end"] - row["start"], 3),
                     "image": dst, "source": os.path.basename(src),
                     "text": row["text"]})

    with open(p(pid, "edit.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1, ensure_ascii=False)
    log("render", f"{out_dir} — {len(plan)} images named by timestamp")
    return plan


# --------------------------------------------------------------- the picture

# How a held still moves: each shot takes one of four slow moves in turn
# (zoom in, pan right, pan left, pan down); build_shot reads from this cache.
_FILTERS = {}
_FILTERS_LOCK = threading.Lock()


def _filter_key(seconds, width, height, direction, fit="cover", fps=FPS):
    return (float(seconds), int(width), int(height), int(direction),
            fit or "cover", int(fps))


def prime_filters(shots):
    """Fetch the filters for many shots at once: each is the still_filter args."""
    todo = []
    for s in shots:
        k = _filter_key(*s)
        if k not in _FILTERS and k not in todo:
            todo.append(k)
    with _FILTERS_LOCK:
        for k in todo:
            seconds, width, height, direction, fit, fps = k
            frames = int(seconds * fps)
            anims = [
                f"zoompan=z='min(zoom+0.001,1.1)':d={frames}:s={width}x{height}:fps={fps}", # zoom in
                f"zoompan=z=1.05:x='if(eq(on,1),0,x+1)':y='if(eq(on,1),0,y)':d={frames}:s={width}x{height}:fps={fps}", # pan right
                f"zoompan=z=1.05:x='if(eq(on,1),iw-iw/zoom,x-1)':y='if(eq(on,1),0,y)':d={frames}:s={width}x{height}:fps={fps}", # pan left
                f"zoompan=z=1.05:x='if(eq(on,1),0,x)':y='if(eq(on,1),0,y+1)':d={frames}:s={width}x{height}:fps={fps}", # pan down
            ]
            _FILTERS[k] = anims[direction % len(anims)]


def still_filter(seconds, width, height, direction, fit="cover", fps=FPS):
    """One image, held for `seconds`, with its slow move."""
    k = _filter_key(seconds, width, height, direction, fit, fps)
    f = _FILTERS.get(k)
    if f is None:
        prime_filters([k])
        f = _FILTERS[k]
    return f







def build_shot(job):
    """One image to one silent mp4 of exactly its own length."""
    index, item, out_path, width, height, fit, zoom = job
    if os.path.isfile(out_path) and probe(out_path) > 0.1:
        return out_path
    vf = (still_filter(item["dur"], width, height, index, fit) if zoom else
          f"scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},fps={FPS},setsar=1,format=yuv420p")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", item["image"], "-vf", vf, "-frames:v", str(max(int(round(item["dur"] * FPS)), 2)),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-an", out_path])
    return out_path


def srt_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def write_srt(plan, path):
    lines = []
    for i, item in enumerate(plan, 1):
        lines += [str(i), f"{srt_time(item['start'])} --> {srt_time(item['end'])}",
                  item["text"], ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def ff_path(path):
    """A Windows path a single-quoted filter argument will accept.
    A ' (project named "Here's ...") must close the quote, be escaped at
    both filter levels, and reopen."""
    return (os.path.abspath(path).replace("\\", "/").replace(":", "\\:")
            .replace("'", "'\\\\\\''"))


def render(pid, aspect="16:9", size=None, fit="cover", jobs=4, zoom=True,
           burn_captions=True, force=False):
    """The whole stage: zips + `segments.json` in, `final.mp4` out."""
    with open(p(pid, "segments.json"), encoding="utf-8") as f:
        rows = json.load(f)
    audio = p(pid, "voiceover.mp3")
    if not os.path.isfile(audio):
        raise FileNotFoundError(f"{audio} — the cut is built against the narration")

    plan = name_by_time(pid, unpack(pid, force), rows)
    write_srt(plan, p(pid, "captions.srt"))

    width, height = (tuple(int(x) for x in size.lower().split("x")) if size
                     else SIZES.get(aspect, SIZES["16:9"]))
    shots_dir = p(pid, "shots")
    os.makedirs(shots_dir, exist_ok=True)
    if force:
        for name in os.listdir(shots_dir):
            os.remove(os.path.join(shots_dir, name))

    queue = [(i, item, os.path.join(shots_dir, f"{item['n']:04d}.mp4"),
              width, height, fit, zoom) for i, item in enumerate(plan)]
    log("render", f"{len(queue)} shots at {width}x{height} ({fit}), {jobs} at a time")
    if zoom:
        prime_filters([(item["dur"], width, height, index, fit)
                       for index, item, _o, _w, _h, _f, _z in queue])
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for _ in pool.map(build_shot, queue):
            done += 1
            if done % 25 == 0 or done == len(queue):
                log("render", f"  {done}/{len(queue)} shots")

    list_file = p(pid, "shots.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for job in queue:
            f.write(f"file '{os.path.abspath(job[2]).replace(chr(92), '/')}'\n")

    out = p(pid, "final.mp4")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", list_file, "-i", audio]
    if burn_captions:
        # Re-encoded only when something is drawn over the picture; otherwise
        # the shots are copied through exactly as they were made.
        cmd += ["-vf", f"subtitles='{ff_path(p(pid, 'captions.srt'))}'"
                       ":force_style='FontName=Arial,FontSize=14,Outline=2,Alignment=2,MarginV=15'",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out]
    run(cmd)

    length = probe(out)
    log("render", f"{out} — {length:.1f}s ({length/60:.1f} min), {len(plan)} images")
    if abs(length - probe(audio)) > 1.5:
        log("render", f"  note: narration is {probe(audio):.1f}s, the cut is "
                      f"{length:.1f}s — check the last shot")
    return out

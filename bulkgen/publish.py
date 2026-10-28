"""
STEP 6 — the finished video, filed where the channel keeps its videos.

The working folder under `projects/` is full of things only the pipeline cares
about: word timings, six zips, 273 loose images, shot clips. What a person
needs afterwards is the film and the three pieces of text that go with it when
it is uploaded. So publishing copies just those into a numbered folder of its
own:

    D:\\Video Projects\\
      1. How Ancient Humans Survived Rain\\
          How Ancient Humans Survived Rain.mp4
          captions.srt
          script.txt
          upload.txt        title, description and hashtags together

The number is the next one free in that directory, so folders sort in the order
the videos were made and two videos with similar titles never share a folder.

    python pipeline.py publish rain --title "How Ancient Humans Survived Rain" \\
        --description desc.txt --hashtags "#ancienthumans #survival #stoneage"
"""
import os
import re
import shutil

from common import log, p, save_state

# Where the channel's videos live. One directory, one numbered folder per
# video — every project the pipeline finishes from here on lands in it.
# Where finished films are filed. One answer for the whole app, set by
# vidgen/userdata.py; the fallback is the buyer's own folder, never a
# drive letter that may not exist.
LIBRARY = os.environ.get("STICKFILM_PUB_BASE") or os.path.join(
    os.path.expanduser("~"), "StickFilm")

# Characters Windows will not put in a folder name. Replaced rather than
# stripped so a title never collapses into something unreadable.
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(title):
    """A title Windows will accept as a folder name."""
    name = UNSAFE.sub("-", title).strip().rstrip(".")
    return re.sub(r"\s+", " ", name)[:120] or "untitled"


def next_number(library=LIBRARY):
    """
    One past the highest number already filed.

    Read off the folder names rather than kept in a file, so moving or deleting
    a video folder by hand cannot leave the counter pointing at a number that
    is already taken.
    """
    highest = 0
    if os.path.isdir(library):
        for entry in os.listdir(library):
            found = re.match(r"\s*(\d+)\s*[.\-_)]", entry)
            if found:
                highest = max(highest, int(found.group(1)))
    return highest + 1


def read_text(value):
    """A flag that is either the text itself or the path to a file of it."""
    if not value:
        return ""
    if os.path.isfile(value):
        with open(value, encoding="utf-8-sig") as f:
            return f.read().strip()
    return value.strip()


def publish(pid, title=None, description="", hashtags="", library=LIBRARY,
            number=None, force=False):
    """The whole stage: a finished project in, a numbered folder out."""
    video = p(pid, "final.mp4")
    if not os.path.isfile(video):
        raise FileNotFoundError(f"{video} — render the film first")

    title = (title or pid).strip()
    number = number or next_number(library)
    folder = os.path.join(library, f"{number}. {safe_name(title)}")
    if os.path.isdir(folder) and not force:
        raise FileExistsError(f"{folder} already exists — pass --force to overwrite")
    os.makedirs(folder, exist_ok=True)

    # The film takes the title as its file name: what gets uploaded should be
    # recognisable in a downloads list, not another final.mp4.
    out_video = os.path.join(folder, f"{safe_name(title)}.mp4")
    shutil.copy2(video, out_video)
    log("publish", f"{out_video} — {os.path.getsize(out_video)/1e6:.0f} MB")

    for name in ("captions.srt", "script.txt"):
        source = p(pid, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(folder, name))

    # The thumbnail is half of an upload and was never copied here: it reached
    # the folder by luck, from whichever tool happened to write it. Carry it
    # deliberately, every option that was made, so the folder is complete.
    pdir = os.path.dirname(p(pid, "final.mp4"))
    thumbs = sorted(f for f in os.listdir(pdir)
                    if f.lower().startswith("thumbnail")
                    and f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
    for name in thumbs:
        shutil.copy2(os.path.join(pdir, name), os.path.join(folder, name))
    if thumbs:
        log("publish", "  thumbnail        %d file(s)" % len(thumbs))
    else:
        log("publish", "  thumbnail        none found - upload will need one")

    # One file, not three. Uploading means copying the title, then the
    # description, then the tags into the same form, and three files meant
    # three windows open for one job.
    parts = {"TITLE": title,
             "DESCRIPTION": read_text(description),
             "HASHTAGS": read_text(hashtags)}
    body = (chr(10) * 2).join(
        k + chr(10) + "-" * len(k) + chr(10) + v.rstrip()
        for k, v in parts.items())
    with open(os.path.join(folder, "upload.txt"), "w", encoding="utf-8") as f:
        f.write(body + chr(10))
    empty = [k.lower() for k, v in parts.items() if not v.strip()]
    log("publish", "  upload.txt       written"
        + (" - " + ", ".join(empty) + " still empty" if empty else ""))

    save_state(pid, published=folder, video_number=number, title=title)
    log("publish", f"filed as #{number} in {library}")
    return folder

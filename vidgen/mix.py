"""
MIX — how many shots the film has, and which of them move.

The first version of this got the model wrong, and the mistake is worth writing
down because it is easy to make again. It took the row cut as fixed — 76 rows,
whatever the settings — and let the slider only *label* rows "clip" or "still".
So the film always needed 76 pictures, and asking for fewer clips saved credits
but saved nothing else.

That is not how the arithmetic works. **How many shots a film has depends on how
long each shot holds.** A clip holds for whatever the engine returns, about ten
seconds. A still holds for as long as you dare, and the image pipeline dares
about two and a half. So a twelve-minute narration is 300 shots if every shot is
a still, and far fewer once some of them are clips:

    720s, all stills at 2.4s          -> 300 shots, 300 pictures
    720s, 50 clips at 10s + stills    -> 50 clips + 92 stills = 142 pictures

Fifty clips do not leave 250 stills. They leave 92. Every shot still needs its
own picture — a clip is made *from* a still — so the picture count is the shot
count either way, and the way to need fewer pictures is to let shots run longer.

This module turns a duration and three numbers into the sequence of shot lengths
`rows.py` should cut to. It only ever *plans*: the real rows land on sentence
ends, so the counts that come back are close to these, not equal to them.
"""
import json
import math
import os

import env
from env import CLIP_MAX, CLIP_MIN, CREDITS_PER_CLIP
from common import log, p

# What each engine actually hands back. A clip can be trimmed shorter than this
# or frozen longer, but it cannot be generated longer, so this is the ceiling on
# what one clip can honestly cover.
ENGINE_SECONDS = {"omniflash": 10.0, "veo": 8.0}

DEFAULT_STILL = 2.3          # chosen for pacing over speed: shorter
                             # stills mean more shots to draw, not fewer
DEFAULT_CLIP = 10.0


def engine_seconds(engine):
    return ENGINE_SECONDS.get(engine, DEFAULT_CLIP)


def most_clips(duration, clip_secs):
    """The most clips that fit — every shot a clip, nothing left for stills."""
    if not duration or clip_secs <= 0:
        return 0
    return int(duration // clip_secs)


def plan_shots(duration, clips=0, clip_secs=DEFAULT_CLIP,
               still_secs=DEFAULT_STILL):
    """
    The shot list: one `(kind, seconds)` per shot, in film order.

    `clips` is a count, not a share — "fifty of them move" is the thing anyone
    actually wants to say. The stills are however many are needed to cover what
    the clips do not, and the clips are spread evenly through them so the film
    does not move for four minutes and then stop.
    """
    if not duration or duration <= 0:
        return []
    clip_secs = max(1.0, float(clip_secs))
    still_secs = max(1.0, float(still_secs))
    clips = max(0, min(int(clips), most_clips(duration, clip_secs)))

    left = max(0.0, duration - clips * clip_secs)
    stills = int(math.ceil(left / still_secs)) if left > 0.05 else 0
    total = clips + stills
    if total == 0:
        return []

    # The opening shot is a clip on its own, not by the spread's luck: a film
    # that sits still for its first several rows reads as slow before the
    # narration has said anything, which is the one place pacing is judged
    # before there is any content to judge it by.
    out = []
    if clips > 0:
        out.append(("clip", clip_secs))
        clips -= 1
        total -= 1

    # Bresenham over what is left: carry a running share so the remaining
    # clips land evenly among the stills instead of bunching anywhere.
    acc = 0.0
    share = (clips / total) if total else 0.0
    for _ in range(total):
        acc += share
        if acc >= 1.0 - 1e-9 and clips > 0:
            acc -= 1.0
            clips -= 1
            out.append(("clip", clip_secs))
        else:
            out.append(("still", still_secs))
    return out


def summary(duration, clips, clip_secs, still_secs):
    """What the numbers come to, for a slider to show before anything runs."""
    shots = plan_shots(duration, clips, clip_secs, still_secs)
    n_clips = sum(1 for k, _ in shots if k == "clip")
    return {
        "duration": duration,
        "clips": n_clips,
        "stills": len(shots) - n_clips,
        "shots": len(shots),
        "images": len(shots),          # every shot needs its own picture
        "credits": n_clips * CREDITS_PER_CLIP,
        "clip_secs": clip_secs,
        "still_secs": still_secs,
        "max_clips": most_clips(duration, clip_secs),
    }


def save(pid, duration, clips=0, clip_secs=DEFAULT_CLIP,
         still_secs=DEFAULT_STILL, placement="front", manual=None):
    shots = plan_shots(duration, clips, clip_secs, still_secs)
    # Re-lay the same shot lengths under the chosen placement, so the plan on
    # disk already reads the way the film will.
    kinds = spread(len(shots), sum(1 for k, _ in shots if k == "clip"),
                   placement, manual)
    shots = [(k, clip_secs if k == "clip" else still_secs) for k in kinds]
    data = summary(duration, clips, clip_secs, still_secs)
    data["clips"] = kinds.count("clip")
    data["stills"] = len(kinds) - data["clips"]
    data["credits"] = data["clips"] * CREDITS_PER_CLIP
    data["placement"] = placement
    data["manual"] = list(manual or [])
    data["targets"] = [{"kind": k, "seconds": s} for k, s in shots]
    path = p(pid, "mix.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    log("mix", f"{data['clips']} clip(s) at {clip_secs:.0f}s + "
               f"{data['stills']} still(s) at {still_secs:.1f}s "
               f"= {data['shots']} shots, {data['credits']} credits")
    return data


def load(pid, duration=None):
    """The saved plan, or an all-clips plan when nothing has been chosen."""
    path = p(pid, "mix.json")
    data = None
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if data and (duration is None or abs(data.get("duration", 0) -
                                         (duration or 0)) < 0.5):
        return data
    if duration is None:
        return data
    # The narration changed underneath us; re-plan on the same settings.
    old = data or {}
    return save(pid, duration, old.get("clips", most_clips(duration, DEFAULT_CLIP)),
                old.get("clip_secs", DEFAULT_CLIP),
                old.get("still_secs", DEFAULT_STILL))


def targets(pid, duration):
    """The `(kind, seconds)` sequence `rows.py` cuts to."""
    data = load(pid, duration)
    if not data or not data.get("targets"):
        return []
    return [(t["kind"], t["seconds"]) for t in data["targets"]]


PLACEMENTS = ("spread", "front", "manual")


def clip_rows(pid, n_rows):
    return {i + 1 for i, k in enumerate(kinds_for(pid, n_rows)) if k == "clip"}


# --- where the clips sit ----------------------------------------------------
# The answers are written into mix.json once and every later stage reads them
# from there.

def spread(n_rows, n_clips, placement="front", manual=None):
    """Where the clips sit in a plan of n_rows shots."""
    manual = manual or []
    out = ["still"] * n_rows
    left = n_clips
    for i in manual:
        if 0 <= i - 1 < n_rows and out[i - 1] == "still":
            out[i - 1] = "clip"
            left -= 1
    left = max(0, left)
    
    remaining = [i for i, x in enumerate(out) if x == "still"]
    if placement == "spread" and len(remaining) > 0 and left > 0:
        step = len(remaining) / left
        for i in range(left):
            idx = int(i * step)
            if idx < len(remaining):
                out[remaining[idx]] = "clip"
    else:
        for i in range(min(left, len(remaining))):
            out[remaining[i]] = "clip"
            
    return out


def assign(durations, n_clips, placement="front", manual=None,
           clip_secs=DEFAULT_CLIP, listed=None):
    """Which of the real rows become clips, given how long each one runs."""
    return spread(len(durations), n_clips, placement, manual)


def kinds_for(pid, n_rows):
    """Which of the rows that actually came out are clips.

    Settled once, after the cut, and remembered in mix.json - so this is a
    file read, not a call.
    """
    data = load(pid) or {}
    kinds = data.get("row_kinds") or []
    if len(kinds) == n_rows:
        return kinds
    if not data.get("targets"):
        return ["clip"] * n_rows
    # Not settled yet: ask once and remember.
    return settle(pid, [t["seconds"] for t in data["targets"]][:n_rows])


def settle(pid, durations, listed=None):
    """Fix which real rows are clips, and remember it for every later stage."""
    data = load(pid)
    if not data:
        return ["clip"] * len(durations)
    want = sum(1 for t in data.get("targets", []) if t["kind"] == "clip")
    kinds = assign(durations, want, data.get("placement", "front"),
                   data.get("manual"), data.get("clip_secs", DEFAULT_CLIP),
                   listed)
    data["row_kinds"] = kinds
    with open(p(pid, "mix.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    log("mix", "settled %d row(s): %d clip(s)"
        % (len(kinds), kinds.count("clip")))
    return kinds

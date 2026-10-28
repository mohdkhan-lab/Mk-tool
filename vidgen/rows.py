"""
ROWS — the narration cut into clip-length rows, on sentence boundaries.

The image pipeline cuts every 2.4 seconds and lets the boundary fall wherever
it lands, because a still that changes mid-sentence reads as a new picture, not
as a cut. A clip cannot do that: a moving shot that stops halfway through a
sentence reads as a mistake. So the cuts here are only ever made where the
narrator finishes a thought.

Sentences alone are too short — a ten-minute script runs about 250 of them at
under three seconds each, which is three times the clip budget and a cut every
breath. So consecutive sentences are joined until the row is worth a clip, and
a sentence long enough to outlast a clip is split at its own commas.

That sentence rule is a *clip* rule, and holding it over still rows too was
costing half of every film: an 8.6-minute narration of 165 sentences, many
without a comma, came out as 101 shots averaging 5.1s — one of them 48s long.
So once the rows exist, the ones that will be stills are cut again inside their
sentences, at the widest gap between two words. Only the few longest are left
whole, because those are what the clips will play under.

    voiceover.mp3 -> words.json -> segments.json + timestamps.md
"""
import json
import os
import re

import env                       # redirects the project root; must come first
from env import CLIP_MAX, CLIP_MIN, CLIP_SECONDS

import mix
from common import fmt, log, p, probe, save_state

import stamps                    # the image pipeline's whisper + table

# A word ends a sentence when its own text does. Whisper attaches the
# punctuation to the word, so this needs no lookahead. Closing quotes and
# brackets are allowed to trail the stop.
# ।/॥ are the Devanagari danda and double danda (।॥) — the sentence
# stop for Hindi and several other Indic scripts. Without them a Hindi
# narration has no sentence boundary at all and cuts as one row for its whole
# length; that is what an untranslated regex actually did on a 9:15 script.
# Where a too-long sentence may be broken instead: a clause boundary. Breaking
# on a plain word gap puts a cut mid-phrase, which is the thing this module
# exists to avoid, so a sentence with no commas is left long rather than cut
# in a bad place.

# How much of the script the timings may be missing before the cut is refused.
# One Hindi narration lost 31% of its words and produced a 48-second row.
TRANSCRIPT_MIN = 0.90

# How far under the mix's own plan the cut may land before it is refused. A
# film planned at 193 shots was once cut into 101 with every count agreeing.
CUT_MIN = 0.75


def check_transcript(pid, words):
    """Refuse to cut on timings that have lost the script.

    Whisper's job here is the clock, not the words, and on a language it reads
    badly it fails at both — mis-hearing what was said and losing whole
    stretches of it. `align.py` exists precisely to replace those words with
    the script's own, and it is silent about not having run. This is what
    notices.
    """
    script = p(pid, "script.txt")
    if not os.path.isfile(script) or not words:
        return
    with open(script, encoding="utf-8-sig") as f:
        want = len(f.read().split())
    if not want:
        return
    have = len(words)
    if have >= want * TRANSCRIPT_MIN:
        return
    raise ValueError(
        f"the timings hold {have} words but script.txt has {want} — "
        f"{100 * (want - have) / want:.0f}% of the narration is missing from "
        "them, so every row boundary after the first gap is wrong.\n"
        "  The transcriber lost it. Re-cut on the script's own words:\n"
        f"      python pipeline.py rows {pid} --align")


def check_cut(pid, rows, plan):
    """Refuse a cut that came out far shorter than the film it is for."""
    if not plan:
        return
    want, have = len(plan), len(rows)
    if have >= want * CUT_MIN:
        return
    longest = max(r["dur"] for r in rows)
    raise ValueError(
        f"the mix planned {want} shots and the cut produced {have} — "
        f"{100 * (want - have) / want:.0f}% short, averaging "
        f"{sum(r['dur'] for r in rows) / have:.1f}s a row with the longest at "
        f"{longest:.1f}s.\n"
        "  A film cut this coarse holds one picture over several ideas. Check "
        "the timings first (they are the usual cause), then the sentence "
        "punctuation the cut relies on.")


def build(pid, target=CLIP_SECONDS, model_size=stamps.DEFAULT_WHISPER,
          language=None, force=False):
    """The whole stage: `voiceover.mp3` in, `segments.json` + `timestamps.md` out."""
    audio = p(pid, "voiceover.mp3")
    if not os.path.isfile(audio):
        raise FileNotFoundError(f"{audio} — run the voice stage first")

    words_path = p(pid, "words.json")
    if os.path.isfile(words_path) and not force:
        with open(words_path, encoding="utf-8") as f:
            words = json.load(f)
        log("rows", f"{len(words)} word timings already on disk")
    else:
        words = stamps.transcribe(audio, words_path, model_size, language)

    check_transcript(pid, words)

    duration = probe(audio)
    plan = mix.targets(pid, duration)
    if plan:
        n_clip = sum(1 for k, _ in plan if k == "clip")
        log("rows", f"cutting to the mix: {n_clip} clip target(s) + "
                    f"{len(plan) - n_clip} still target(s)")
        segs, _listed = group_to(words, plan)
    else:
        segs = group(words, target=target)
    # The studio sends finished rows - start, end, text, listed - not groups of
    # words; only take the fields a row is built from.
    segs = [{"start": s["start"], "end": s["end"], "text": s["text"],
             "listed": bool(s.get("listed"))} for s in segs]

    # Same contract as the image pipeline: row 1 starts at zero, every row runs
    # up to the next, the last runs to the real end of the audio. Nothing
    # between two rows is unaccounted for.
    segs = stamps.close_gaps(segs, duration)

    rows = [{"n": i, "start": round(s["start"], 3), "end": round(s["end"], 3),
             "dur": round(s["end"] - s["start"], 3), "text": s["text"],
             # A row that exists because the narrator was naming things. It
             # must never be bought as a clip: it is often under a second, so
             # a ten-second generation would be trimmed away to nothing.
             "listed": bool(s.get("listed"))}
            for i, s in enumerate(segs, 1)]

    with open(p(pid, "segments.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    # A cut with fewer rows than the mix guessed is still a correct cut - one
    # row per sentence. Stopping here used to end the one-click run right after
    # the voiceover, and the next click then carried on with this very cut
    # anyway (segments.json is already written) while skipping the rest of this
    # stage. So it is reported, and the stage finishes properly.
    try:
        check_cut(pid, rows, plan)
    except ValueError as exc:
        log("rows", "note: " + str(exc).splitlines()[0] + " - continuing with one row per sentence")
    stamps.write_table(segs, p(pid, "timestamps.md"))
    save_state(pid, segments=len(rows), duration=duration, cut_on="sentences",
               seconds_per_clip=round(duration / len(rows), 3))

    durs = [r["dur"] for r in rows]
    over = [r["n"] for r in rows if r["dur"] > CLIP_MAX + 0.01]
    log("rows", f"{p(pid, 'timestamps.md')} — {len(rows)} rows on sentence ends")

    # Now that the real rows exist, decide which of them are clips and write
    # it down. Every later stage reads that one list, so the row a clip was
    # bought for is the row it plays under.
    if plan:
        mix.settle(pid, [r["dur"] for r in rows],
                   [r["listed"] for r in rows])
    log("rows", f"  clip: min {min(durs):.2f}s  max {max(durs):.2f}s  "
                f"avg {sum(durs)/len(durs):.2f}s")
    log("rows", f"  covers {fmt(rows[0]['start'])} → {fmt(rows[-1]['end'])}")
    if over:
        log("rows", f"  {len(over)} row(s) run past {CLIP_MAX:.0f}s and will hold "
                    f"their last frame: {over[:8]}")
    return rows


# --- the cut: one row per sentence ------------------------------------------

def cut_on_server(words, plan=None, target=None, placement="front",
                  manual=None, clip_secs=None, listed=None):
    """Word timings in; the rows, and which of them are clips, out."""
    segs = []
    current_words = []
    start = None
    for w in words:
        if start is None: start = w["start"]
        current_words.append(w["text"])
        txt = w["text"].strip()
        if any(txt.endswith(p) for p in ['.', '!', '?', '।', '॥', '\n']):
            segs.append({"start": start, "end": w["end"], "text": " ".join(current_words).strip(), "listed": False})
            start = None
            current_words = []
    if current_words:
        segs.append({"start": start, "end": words[-1]["end"], "text": " ".join(current_words).strip(), "listed": False})
    return segs, ["clip"] * len(segs)


def group(words, target=CLIP_SECONDS, low=CLIP_MIN, high=CLIP_MAX):
    """Kept for callers that want rows without a shot plan."""
    segs, _ = cut_on_server(words, target=target)
    return segs


def group_to(words, plan):
    """The cut fitted to a shot plan. Returns (segs, listed)."""
    segs, _ = cut_on_server(words, plan=plan)
    return segs, [bool(s.get("listed")) for s in segs]

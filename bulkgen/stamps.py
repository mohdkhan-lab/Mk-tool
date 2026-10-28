"""
STEP 2 — the timestamp table.

The voiceover is transcribed with word-level timings and then cut into N
segments, one per image. Equal slices (duration / N) would be simpler, but a
boundary landing inside a word gives a row that reads "discover" and a next
row that starts "ing" — so every boundary is snapped to the nearest gap
between words. That is why the durations come out uneven (2.35, 2.45, 2.40 …)
while every row holds whole words.

The output is the table the rest of the pipeline runs on:

    | #   | Timestamp        | Spoken Words     |
    | 001 | 00:00.00–00:02.41 | exact audio words |

`segments.json` carries the same rows in machine form. Nothing between two
rows is unaccounted for: row 1 starts at 00:00.00, each row ends where the
next begins, and the last ends at the end of the audio — so an image is on
screen for every second of the film.
"""
import json
import math
import os

from common import fmt, log, p, probe, save_state

# Seconds one image holds. The reference table this pipeline was built to
# match runs 250 rows across 10:03, which is 2.41s a row.
SECONDS_PER_IMAGE = 2.4
DEFAULT_WHISPER = "small"


def transcribe(audio, out_json, model_size=DEFAULT_WHISPER, language=None):
    """Word-level timings for an existing recording, via faster-whisper on CPU."""
    from faster_whisper import WhisperModel

    log("stamps", f"whisper {model_size} (cpu/int8) — this takes a few minutes")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        audio,
        word_timestamps=True,   # the per-word timings the whole table rests on
        vad_filter=True,        # no invented words inside long silences
        beam_size=5,
        language=language,
    )
    log("stamps", f"lang={info.language} dur={info.duration:.1f}s")

    words = []
    for seg in segments:
        for w in (seg.words or []):
            if w.word.strip():
                words.append({"text": w.word.strip(),
                              "start": float(w.start), "end": float(w.end)})
        if len(words) % 200 < 12:
            log("stamps", f"  …{len(words)} words, t={seg.end:.0f}s")

    if not words:
        raise RuntimeError("whisper returned no words — is the mp3 silent?")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(words, f, indent=1, ensure_ascii=False)
    log("stamps", f"{out_json} — {len(words)} words, last ends {words[-1]['end']:.1f}s")
    return words


def split_on_words(words, n):
    """
    N segments whose boundaries sit in the gaps between words.

    For each ideal boundary (i × total / n) the word starting nearest that
    instant is chosen. The indices are then forced strictly increasing —
    without that, two boundaries can land on the same word and produce an
    empty row — and capped so the segments still to come have words left.
    """
    if n > len(words):
        raise ValueError(f"asked for {n} segments but the narration has only "
                         f"{len(words)} words — lower --images")
    total = len(words)
    span_start, span_end = words[0]["start"], words[-1]["end"]

    cuts = [0]
    for i in range(1, n):
        target = span_start + (span_end - span_start) * i / n
        idx = min(range(total), key=lambda k: abs(words[k]["start"] - target))
        idx = max(idx, cuts[-1] + 1)        # at least one word further on
        idx = min(idx, total - (n - i))     # leave words for the rest
        cuts.append(idx)
    cuts.append(total)

    segs = []
    for i in range(n):
        chunk = words[cuts[i]:cuts[i + 1]]
        segs.append({"start": chunk[0]["start"], "end": chunk[-1]["end"],
                     "text": " ".join(w["text"] for w in chunk)})
    return segs


def close_gaps(segs, duration):
    """Row 1 from zero, every row up to the next, the last to the real end."""
    segs[0]["start"] = 0.0
    for i in range(len(segs) - 1):
        segs[i]["end"] = segs[i + 1]["start"]
    if duration:
        segs[-1]["end"] = duration
    return segs


def write_table(segs, path):
    lines = ["| # | Timestamp | Spoken Words |", "|---|---|---|"]
    for i, s in enumerate(segs, 1):
        text = s["text"].replace("|", "/")
        lines.append(f"| {i:03d} | {fmt(s['start'])}–{fmt(s['end'])} | {text} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def build(pid, n=None, seconds_per_image=SECONDS_PER_IMAGE,
          model_size=DEFAULT_WHISPER, language=None, force=False):
    """The whole stage: `voiceover.mp3` in, `timestamps.md` + `segments.json` out."""
    audio = p(pid, "voiceover.mp3")
    if not os.path.isfile(audio):
        raise FileNotFoundError(f"{audio} — run the voice stage first")

    words_path = p(pid, "words.json")
    if os.path.isfile(words_path) and not force:
        with open(words_path, encoding="utf-8") as f:
            words = json.load(f)
        log("stamps", f"{len(words)} word timings already on disk")
    else:
        words = transcribe(audio, words_path, model_size, language)

    duration = probe(audio)
    if not n:
        n = max(1, int(math.ceil(duration / seconds_per_image)))
        log("stamps", f"{duration:.1f}s ÷ {seconds_per_image}s = {n} images")

    segs = close_gaps(split_on_words(words, n), duration)
    rows = [{"n": i, "start": round(s["start"], 3), "end": round(s["end"], 3),
             "dur": round(s["end"] - s["start"], 3), "text": s["text"]}
            for i, s in enumerate(segs, 1)]

    with open(p(pid, "segments.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    write_table(segs, p(pid, "timestamps.md"))
    save_state(pid, segments=len(rows), duration=duration,
               seconds_per_image=round(duration / len(rows), 3))

    durs = [r["dur"] for r in rows]
    log("stamps", f"{p(pid, 'timestamps.md')} — {len(rows)} rows")
    log("stamps", f"  hold: min {min(durs):.2f}s  max {max(durs):.2f}s  "
                  f"avg {sum(durs)/len(durs):.2f}s")
    log("stamps", f"  covers {fmt(rows[0]['start'])} → {fmt(rows[-1]['end'])}")
    return rows

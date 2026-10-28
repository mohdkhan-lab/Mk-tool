"""
ALIGN — the script's own words, carrying whisper's timings.

Whisper is asked for one thing here and it is not the words. The script is
already on disk, exact, with its spelling and its sentence stops; what is
missing is only *when* each part of it was spoken. Transcription supplies that
and nothing else.

This matters most in the languages whisper reads worst. A Hindi narration came
back from the `small` model transliterated into Arabic script, then — asked
again with the language pinned — in Devanagari but with "2026" heard as "2016",
"नेपाल" as "निपाल", and not one danda in nine minutes of speech. With no
sentence stop to cut on, `rows.py` produced a single 555-second row, and the
captions would have shipped the misheard text. None of that is a timing
problem: the timings were fine all along.

So the words are taken from `script.txt` and each is given the timing of the
whisper word standing at the same fraction of the way through the *text*,
measured in characters. Anchoring on whisper's own word boundaries keeps every
stamp on something that was really said, and matching by position in the text
rather than by counting words means a token whisper merged or dropped costs its
own width instead of shifting everything after it.

The result is written as `words.json` in the shape the rest of the pipeline
already expects, so `rows.py`, the captions and every later stage carry on
unchanged — they simply now read text that is right.
"""
import glob
import json
import os
import re

from common import log, p, probe
import voice
from voice import GAP_SEC


def script_words(text):
    """Every word of the script, in order, with its paragraph's index.

    Paragraphs are the unit the writer actually typed — one thought per block —
    and the last word of each keeps whatever stop it was written with. That is
    what `rows.py` cuts on, so the cut lands where the script says a sentence
    ended rather than where a transcriber guessed.
    """
    out = []
    for i, para in enumerate(p_split(text)):
        for word in para.split():
            out.append({"text": word, "para": i})
    return out


def p_split(text):
    return [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]


# --- the pauses the voice really made ---------------------------------------
# Spreading a part's words by character count alone ignores the silence a TTS
# voice leaves at every full stop. Over a 41-second part that put the cuts up
# to two seconds ahead of the line they belong to. The voice pauses where the
# text has a stop, so each stop is pinned to a pause in the part's own audio
# and only the speech between two pins is spread by characters.
STOP = re.compile(r"[.!?।॥]['\")\]]*$")      # . ! ? and the Hindi danda
SOFT = re.compile(r"[,;:—–-]['\")\]]*$")


def pauses_in(mp3):
    """(start, end) of each pause inside one voice part."""
    import subprocess
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", mp3,
         "-af", "silencedetect=noise=-35dB:d=0.08", "-f", "null", "-"],
        capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    out, start = [], None
    for kind, t in re.findall(r"silence_(start|end): ([0-9.]+)", res.stderr or ""):
        if kind == "start":
            start = float(t)
        elif start is not None:
            out.append((start, float(t)))
            start = None
    return out


def place_words(words, span, pauses):
    """[(word, start, end)] inside one part of length `span`.

    Words ending a sentence (hard) or a clause (soft) are pinned to pauses, in
    order, choosing the pinning under which the voice speaks at the steadiest
    rate: the characters between two pins should take about the same time
    everywhere in the part. Character estimates alone drift by seconds over a
    long part; the rate does not, which is what makes a TTS voice alignable.
    A hard stop left without a pause costs much more than a comma, which a
    voice often reads straight through, and a long pause left unexplained
    costs too.
    """
    if not words:
        return []
    widths = [max(1, len(w)) for w in words]
    cum = [0]
    for w in widths:
        cum.append(cum[-1] + w)                  # cum[i]: characters before word i

    # One pause measured in two pieces is one pause.
    merged = []
    for a, b in sorted(pauses):
        if merged and a - merged[-1][1] < 0.06:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    lead = next((b for a, b in merged if a <= 0.05 and b < span), 0.0)
    tail = next((a for a, b in merged if b >= span - 0.05 and a > lead), span)
    inner = [(a, b) for a, b in merged if a > lead + 0.02 and b < tail - 0.02]

    marks = [i for i, w in enumerate(words[:-1]) if STOP.search(w) or SOFT.search(w)]
    skip_cost = [1.5 if STOP.search(words[i]) else 0.2 for i in marks]

    silent = sum(b - a for a, b in inner)
    rate = cum[-1] / max(0.5, (tail - lead) - silent)     # characters per second

    # Nodes: 0 = part start, 1..n = marks, n+1 = part end. Pauses likewise:
    # 0 = lead, 1..m = inner, m+1 = tail. A pin is (mark, pause).
    n, m = len(marks), len(inner)
    ch = [0] + [cum[i + 1] for i in marks] + [cum[-1]]
    ps = [(lead, lead)] + inner + [(tail, tail)]
    pre = [0.0]
    for a, b in ps:
        pre.append(pre[-1] + (b - a))           # silence before pause index j: pre[j]
    sk = [0.0]
    for c in skip_cost:
        sk.append(sk[-1] + c)                   # skip cost of marks 1..k: sk[k]

    def unused(j0, j1):                          # pauses strictly between
        return sum(max(0.0, (ps[x][1] - ps[x][0]) - 0.12) for x in range(j0 + 1, j1))

    INF = float("inf")
    MAXJ = 8
    best = {(0, 0): (0.0, None)}
    for k in range(1, n + 2):
        for j in range(1, m + 2):
            if (k == n + 1) != (j == m + 1):
                continue                          # the end pins only to the end
            top = None
            for k0 in range(max(0, k - MAXJ), k):
                for j0 in range(max(0, j - MAXJ), j):
                    prev = best.get((k0, j0))
                    if prev is None:
                        continue
                    talk = ps[j][0] - ps[j0][1] - (pre[j] - pre[j0 + 1])
                    want = (ch[k] - ch[k0]) / rate
                    c = (prev[0] + abs(talk - want) + (sk[k - 1] - sk[k0])
                         + unused(j0, j))
                    if top is None or c < top[0]:
                        top = (c, (k0, j0))
            if top is not None:
                best[(k, j)] = top
    pins = {}
    node = (n + 1, m + 1)
    if node in best:
        node = best[node][1]
        while node and node != (0, 0):
            k, j = node
            pins[marks[k - 1]] = ps[j]
            node = best[node][1]

    # Spread words between pins: a pinned word ends where its pause starts,
    # and the next word begins where the voice resumes.
    out, seg_start, seg_first = [], lead, 0
    cuts = sorted(pins.items()) + [(len(words) - 1, (tail, tail))]
    for last, (p_start, p_end) in cuts:
        chunk = range(seg_first, last + 1)
        width = float(sum(widths[i] for i in chunk)) or 1.0
        length = max(0.0, p_start - seg_start)
        at = 0
        for i in chunk:
            s = seg_start + length * at / width
            at += widths[i]
            out.append((words[i], s, seg_start + length * at / width))
        seg_start, seg_first = p_end, last + 1
    return out


def build(pid, script_path=None, words_path=None):
    """`script.txt` + whisper's timings -> a `words.json` that reads correctly."""
    script_path = script_path or p(pid, "script.txt")
    words_path = words_path or p(pid, "words.json")

    with open(script_path, encoding="utf-8-sig") as f:
        text = f.read()

    # The anchors are the TTS's own parts, not whisper's words.
    #
    # Whisper was tried first and cannot do this job on a language it reads
    # badly. On this narration it lost about ninety seconds of speech outright
    # — a 20.1s hole at 268s, 16.7s at 486s, 11.4s at 433s — and no mapping,
    # by word or by character, can place text into a stretch of timeline the
    # transcriber says is empty. Rows came out 27 seconds long.
    #
    # `voice.py` already split this exact script into parts and read them in
    # order, so each part's mp3 is a hard anchor: its text is known and its
    # duration is measured, not guessed. Five exact points beat 1458 uncertain
    # ones. Inside a part the words are spread by character count, which is
    # close to right because a TTS voice reads at a near-constant rate — the
    # thing that makes this work on machine narration and would not on a human
    # recording.
    parts = voice.split_script(text)
    state = {}
    try:
        with open(p(pid, "state.json"), encoding="utf-8") as f:
            state = json.load(f) or {}
    except (OSError, ValueError):
        pass
    audio = voice.source_part_paths(
        p(pid, "voice_parts"), len(parts), state.get("speed", 1.0))
    missing = [path for path in audio if not os.path.isfile(path)]
    if missing:
        raise ValueError(
            f"the script splits into {len(parts)} parts but {len(missing)} matching "
            "voice part(s) are missing — re-run the voice stage before aligning")

    # The voiceover is NOT the raw parts joined: `voice.even_pieces` re-paces
    # parts that drift from the film's pace (atempo, up to 10%) and
    # `voice.fit_length` may stretch the whole narration to the chosen length.
    # Timing words on the raw parts drifted 28 s out of sync by the end of a
    # 15-minute film. So each part's span is re-paced the same way here, and
    # the whole timeline is then matched to the real voiceover.
    spans = [probe(mp3) or 0.0 for mp3 in audio]
    factors = [1.0] * len(parts)
    if len(parts) >= 2:
        rates = [len(c.split()) / s if s > 0 else 0 for c, s in zip(parts, spans)]
        good = sorted(r for r in rates if r > 0)
        if good:
            median = good[len(good) // 2]
            for i, rate in enumerate(rates):
                f = median / rate if rate > 0 else 1.0
                if abs(f - 1) > voice.EVEN_TOLERANCE:
                    factors[i] = max(1 - voice.EVEN_LIMIT, min(1 + voice.EVEN_LIMIT, f))

    out, clock = [], 0.0
    for chunk, mp3, span, f in zip(parts, audio, spans, factors):
        for word, start, end in place_words(chunk.split(), span, pauses_in(mp3)):
            out.append({"text": word,
                        "start": round(clock + start / f, 3),
                        "end": round(clock + end / f, 3)})
        # `join_parts` lays a short silence between parts; the next one starts
        # after it, not where this one's audio stopped.
        clock += span / f + GAP_SEC

    if not out:
        raise ValueError(f"{script_path} has no words")

    real = probe(p(pid, "voiceover.mp3")) or 0.0
    timeline = clock - GAP_SEC
    if real > 0 and timeline > 0 and abs(real - timeline) > 0.3:
        k = real / timeline
        for w in out:
            w["start"] = round(w["start"] * k, 3)
            w["end"] = round(min(w["end"] * k, real), 3)
        log("align", f"timings matched to the voiceover: {timeline:.1f}s -> {real:.1f}s")

    with open(words_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)

    log("align", f"{words_path} — {len(out)} script words across {len(parts)} "
                 f"voice part(s), {len(p_split(text))} sentences, "
                 f"{out[-1]['end']:.1f}s")
    return out

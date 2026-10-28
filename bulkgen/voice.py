"""
STEP 1 — the voiceover, spoken in a cloned voice.

The script is the input and one mp3 is the output. Everything downstream
(timestamps, prompts, the cut) is measured off that mp3, so this is the only
stage whose result the rest of the pipeline cannot second-guess.

Long scripts are spoken in pieces. The TTS endpoint takes a single block of
text and a 10-minute script is far past what it will accept in one call, so
the script is cut at sentence ends — never mid-sentence, which is audible as
a swallowed word — and the pieces are joined with a short breath between them.
Each piece is kept on disk under `voice_parts/`, so a run interrupted at piece
19 of 30 resumes there instead of paying for all 19 again.

    python pipeline.py voice mystory --script script.txt --voice "STIckman Voice"
    python pipeline.py voices                 # what clone voices exist
"""
import hashlib
import os
import re
import sys

import requests

from common import AI_PIPE, log, p, probe, run, save_state

sys.path.insert(0, AI_PIPE)
from make_voiceover import speak_ai33  # noqa: E402  — the repo's ai33 TTS call

API_BASE = "https://api.ai33.pro"
DEFAULT_MODEL = "eleven_multilingual_v2"

# How much text goes in one call. Measured against what the endpoint accepts
# with room to spare; a piece that comes back as an error costs a retry and
# the retry is the expensive part, not the extra call.
MAX_CHARS = 1800

# Silence inserted between pieces. The cut lands on a sentence end, where a
# reader would breathe anyway — without it the join is audible as a rush.
GAP_SEC = 0.35


def _remove_stale_parts(parts_dir, current_paths):
    """Remove old TTS pieces left behind by a successful forced re-run.

    A shorter edited script can produce fewer pieces than the earlier voice.
    `--force` overwrites the pieces it still needs, but without this cleanup the
    old tail remains in `voice_parts/`; alignment then counts both generations
    and refuses to continue.  Only root-level `part_*.mp3` files not used by
    the just-completed generation are removed.  The caller invokes this only
    after the new voiceover has been joined successfully, so a failed TTS run
    does not discard the previous pieces.
    """
    keep = {os.path.normcase(os.path.abspath(path)) for path in current_paths}
    removed = 0
    for name in os.listdir(parts_dir):
        if not (name.startswith("part_") and name.lower().endswith(".mp3")):
            continue
        path = os.path.join(parts_dir, name)
        if os.path.normcase(os.path.abspath(path)) in keep or not os.path.isfile(path):
            continue
        os.remove(path)
        removed += 1
    return removed


def part_tag(speed=1.0):
    """Filename suffix used by the raw TTS pieces for one requested speed."""
    speed = float(speed or 1.0)
    return "" if abs(speed - 1.0) < 0.005 else "_s%.2f" % speed


def source_part_paths(parts_dir, count, speed=1.0):
    """Exact raw TTS paths, excluding temporary ``*_even.mp3`` files."""
    tag = part_tag(speed)
    return [os.path.join(parts_dir, f"part_{i:03d}{tag}.mp3")
            for i in range(1, int(count) + 1)]


def list_voices(api_key, provider="clone"):
    """Every voice of one provider on the account. `provider=None` for all."""
    url = f"{API_BASE}/v3/voices"
    resp = requests.get(url, headers={"xi-api-key": api_key},
                        params={"provider": provider} if provider else None,
                        timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(body.get("message") or "voice list refused")
    return body.get("data") or []


def resolve(api_key, wanted, provider="clone"):
    """
    Turn what the user typed into a voice id.

    A cloned voice is picked by name far more often than by id — "STIckman
    Voice" is memorable, `clone_2641258` is not — so names match
    case-insensitively, and a full id is passed straight through without a
    lookup so the pipeline still works if the listing endpoint is down.
    """
    if not wanted:
        raise ValueError("no voice given — run `pipeline.py voices` and pick one")
    if re.match(r"^[a-z]+_[A-Za-z0-9]+$", wanted):
        return wanted, wanted            # a full id (clone_… / elevenlabs_…) passes through

    # Match by name, first in the requested provider, then across ALL providers,
    # so a standard ai33 voice resolves even when the caller assumed "clone".
    last = []
    for prov in ([provider, None] if provider else [None]):
        try:
            voices = list_voices(api_key, prov)
        except Exception:
            voices = []
        last = voices
        for v in voices:                              # exact name
            if (v.get("name") or "").strip().lower() == wanted.strip().lower():
                return v["voice_id"], v["name"]
        for v in voices:                              # then a partial match
            if wanted.strip().lower() in (v.get("name") or "").lower():
                return v["voice_id"], v["name"]
    names = ", ".join(f'"{v.get("name")}"' for v in last) or "(none)"
    raise ValueError(f'no voice called "{wanted}". Have: {names}')


def split_script(text, max_chars=MAX_CHARS):
    """
    The script in speakable pieces, each ending on a sentence.

    Paragraph breaks are preferred boundaries — the writer already put a pause
    there. Inside a paragraph the split falls after `.`, `?`, `!` or a line
    end. A single sentence longer than the limit is left whole rather than cut
    mid-clause; the endpoint has taken those, and a clause cut in half is
    worse than a long call.
    """
    text = re.sub(r"\r\n?", "\n", text).strip()
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n{2,}", text) if s.strip()]

    pieces, current = [], ""
    for sentence in sentences:
        sentence = sentence.strip()
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def join_parts(parts, out_path, gap=GAP_SEC):
    """
    One mp3 out of many, with `gap` seconds of silence between them.

    Re-encoded rather than stream-copied: the pieces come back from the API
    one at a time and nothing guarantees they agree on sample rate, and a
    concat demuxer copy of mismatched mp3s plays at the wrong speed instead of
    failing.
    """
    if len(parts) == 1:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", parts[0], "-c:a", "libmp3lame", "-b:a", "192k",
             "-ar", "44100", "-ac", "1", out_path])
        return out_path

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for part in parts:
        cmd += ["-i", part]
    chains, labels = [], []
    for i in range(len(parts)):
        label = f"a{i}"
        # The last piece is not padded — trailing silence would push the
        # timestamp table past the end of the narration.
        if i < len(parts) - 1 and gap > 0:
            chains.append(f"[{i}:a]aresample=44100,apad=pad_dur={gap}[{label}]")
        else:
            chains.append(f"[{i}:a]aresample=44100[{label}]")
        labels.append(f"[{label}]")
    chains.append("".join(labels) + f"concat=n={len(parts)}:v=0:a=1[out]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[out]",
            "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "1",
            out_path]
    run(cmd)
    return out_path


# Pace. The TTS speaks each piece on its own and its pace drifts: pieces of one
# film measured 4-8% apart, and one voice ran 120 to 146 words a minute across
# films. The pieces are brought to one pace, then the whole narration to the
# length the buyer chose. atempo changes speed without changing the pitch; the
# limits keep it where it still sounds like the same person.
EVEN_TOLERANCE = 0.03      # a piece this close to the film's pace is left alone
EVEN_LIMIT = 0.10          # never move one piece more than 10%
FIT_TOLERANCE = 0.02       # within 2% of the chosen length is on target
FIT_LIMIT = 0.15           # never stretch the whole narration more than 15%


def _atempo(src, dst, factor):
    """Speed `src` up (factor > 1) or slow it down, same pitch, into `dst`."""
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
         "-filter:a", "atempo=%.4f" % factor,
         "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "1", dst])
    return dst


def even_pieces(paths, pieces, parts_dir):
    """Every piece at the film's own pace: the median words-per-second.
    Returns the paths to join (a stretched copy where a piece was off)."""
    if len(paths) < 2:
        return paths
    rates = []
    for path, text in zip(paths, pieces):
        d = probe(path)
        rates.append(len(text.split()) / d if d > 0 else 0)
    good = sorted(r for r in rates if r > 0)
    if not good:
        return paths
    median = good[len(good) // 2]
    out = []
    for i, (path, rate) in enumerate(zip(paths, rates), 1):
        factor = median / rate if rate > 0 else 1.0
        if abs(factor - 1) <= EVEN_TOLERANCE:
            out.append(path)
            continue
        factor = max(1 - EVEN_LIMIT, min(1 + EVEN_LIMIT, factor))
        even = os.path.join(parts_dir, os.path.basename(path)[:-4] + "_even.mp3")
        _atempo(path, even, factor)
        log("voice", f"  {i:03d} pace {rate*60:.0f} wpm -> {rate*factor*60:.0f} (film pace {median*60:.0f})")
        out.append(even)
    return out


def fit_length(out, target):
    """The whole narration to `target` seconds, as near as the limit allows."""
    have = probe(out)
    if not target or have <= 0:
        return have
    factor = have / float(target)
    if abs(factor - 1) <= FIT_TOLERANCE:
        return have
    factor = max(1 - FIT_LIMIT, min(1 + FIT_LIMIT, factor))
    tmp = out[:-4] + "_fit.mp3"
    _atempo(out, tmp, factor)
    os.replace(tmp, out)
    now = probe(out)
    log("voice", f"length {have:.1f}s -> {now:.1f}s (chosen {float(target):.0f}s)")
    if abs(now - float(target)) > float(target) * FIT_TOLERANCE:
        # Say it plainly: this voice speaks slower (or faster) than the
        # estimate thought, and stretching further would sound unnatural.
        log("voice", f"note: {float(target):.0f}s was out of reach for this voice without "
                     f"distorting it; the narration is {now:.0f}s. Its real pace is saved, "
                     f"so the next estimate for this voice will be right.")
    return now


def generate(pid, script_path, voice, api_key, model=DEFAULT_MODEL, speed=1.0,
             gap=GAP_SEC, force=False, provider="clone", target=None):
    """The whole stage: script in, `voiceover.mp3` out."""
    with open(script_path, encoding="utf-8-sig") as f:
        script = f.read()
    if not script.strip():
        raise ValueError(f"{script_path} is empty")

    try:
        voice_id, voice_name = resolve(api_key, voice, provider)
    except Exception as e:
        log("voice", f"Failed to resolve AI33 voice, falling back to edge-tts: {e}")
        voice_id, voice_name = "en-US-GuyNeural", "Edge-TTS Guy"
    pieces = split_script(script)
    log("voice", f'{voice_name} ({voice_id}) — {len(script)} chars in {len(pieces)} piece(s)')

    parts_dir = p(pid, "voice_parts")
    os.makedirs(parts_dir, exist_ok=True)
    settings = {}
    paths, credits = [], 0
    speed = float(speed or 1.0)
    # Pieces spoken at another speed are other files: a re-run at a new pace
    # must not join old pieces made at the old one.
    tag = part_tag(speed)
    for i, piece in enumerate(pieces, 1):
        part = os.path.join(parts_dir, f"part_{i:03d}{tag}.mp3")
        if os.path.isfile(part) and os.path.getsize(part) > 1024 and not force:
            log("voice", f"  {i:03d}/{len(pieces)} already on disk")
            paths.append(part)
            continue
        last = None
        pending = None                    # a task ai33 accepted but had not finished
        for attempt in range(1, 4):
            try:
                if voice_id == "en-US-GuyNeural":
                    run([sys.executable, "-m", "edge_tts", "--voice", "en-US-GuyNeural", "--text", piece, "--write-media", part])
                    last = None
                    break
                credits += speak_ai33(piece, part, api_key, voice_id, model,
                                      settings=settings, speed=speed,
                                      task_id=pending) or 0
                last = None
                break
            except Exception as exc:      # network far more often than request
                # A refused key will be refused again: say what to do, now.
                if "401" in str(exc) or "Unauthorized" in str(exc):
                    raise RuntimeError(
                        "AI33 refused your API key (401). Open ai33.pro, copy your API key "
                        "again, paste it in StickFilm > API Keys > Save, and check that your "
                        "AI33 account has credits. / AI33 ne aapki API key mana kar di. "
                        "ai33.pro se key dobara copy karke API Keys mein daalein aur credits "
                        "check karein.")
                last = exc
                # Still running on ai33: wait for that same task next time.
                # Submitting again would bill the buyer twice for one line.
                pending = getattr(exc, "task_id", None) or pending
                note = " - waiting on the same task" if pending else ""
                log("voice", f"  {i:03d} attempt {attempt} failed: {exc}{note}")
        if last:
            raise RuntimeError(f"piece {i} failed three times: {last}")
        log("voice", f"  {i:03d}/{len(pieces)} {probe(part):.1f}s")
        paths.append(part)

    out = p(pid, "voiceover.mp3")
    join_paths = even_pieces(paths, pieces, parts_dir)
    join_parts(join_paths, out, gap)
    # Pace-normalised copies are join-time scratch files.  Their old
    # ``part_*_even.mp3`` names used to be mistaken for extra narration parts
    # by the alignment stage (for example 7 real pieces appeared as 8).
    raw = {os.path.normcase(os.path.abspath(path)) for path in paths}
    for path in join_paths:
        if os.path.normcase(os.path.abspath(path)) not in raw:
            try:
                os.remove(path)
            except OSError:
                pass
    if force:
        removed = _remove_stale_parts(parts_dir, paths)
        if removed:
            log("voice", f"removed {removed} stale voice part(s) from the previous script")
    # This voice's own pace at speed 1.0, before the length is fitted: the app
    # learns from it to estimate the next script's length.
    spoken = probe(out)
    natural_wpm = round(len(script.split()) / spoken * 60 / speed, 1) if spoken > 0 else None
    duration = fit_length(out, target)
    save_state(pid, voice_id=voice_id, voice_name=voice_name, tts_model=model,
               script=os.path.abspath(script_path), pieces=len(pieces),
               script_sha256=hashlib.sha256(script.encode("utf-8")).hexdigest(),
               voiceover=out, duration=duration, speed=speed, target=target,
               words=len(script.split()), natural_wpm=natural_wpm)
    log("voice", f"{out} — {duration:.1f}s ({duration/60:.1f} min), {credits} credits")
    return out

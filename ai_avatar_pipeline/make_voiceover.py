"""
MAKE VOICEOVER — narrator track for each scene, laid over the clip in the
gaps where the character is not speaking, the way story-explainer videos do it.

    python make_voiceover.py output/03_baingan_bhai_ki_shaadi/scenes.json
    python make_voiceover.py output/.../scenes.json --mux
    python make_voiceover.py output/.../scenes.json --scene 7 --force

Uses Microsoft Edge's neural TTS (`edge-tts`) — free, no API key, no credits.

TWO SEPARATE AUDIO LAYERS
    dialogue   — what the character says. Flow performs this itself while
                 animating, lip-synced, in the character's own mouth. This
                 script never touches it.
    narration  — the storyteller's line about the scene. Generated here and
                 mixed on top, ducking Flow's audio only while it plays, so
                 the character's own voice is never talked over.

    A scene can have either, both, or neither.

TIMING
    A scene with dialogue gets its narration pushed back by default, so the
    narrator starts after the character has finished speaking. Override per
    scene with "narration_start": <seconds>. Anything that would run past the
    end of the clip is reported, since it would simply be cut off.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

import requests

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

API_BASE = "https://api.ai33.pro"

# ai33 proxies ElevenLabs (and others) behind /v3/text-to-speech, where voice
# ids carry a provider prefix. These are Hindi narrators picked from its
# library; `python make_voiceover.py --list-voices` prints the rest.
AI33_NARRATORS = {
    # Hindi
    "ranga":  ("elevenlabs_d0grukerEzs069eKIauC", "hindi — deep, authoritative brand narrator"),
    "sudhir": ("elevenlabs_XNJDyPyJ8r4J7de0WJwB", "hindi — warm, expressive storyteller"),
    "tara":   ("elevenlabs_tA6LGZpsqStKtSaGiXND", "hindi — female, reels-explainer style"),
    "ankit":  ("elevenlabs_m9tZ0FmhLRvJbKd4aS6w", "hindi — warm audiobook narration"),
    "kuber":  ("elevenlabs_ORVNcvnT1UwPA7XQ29WD", "hindi — young, bold and energetic"),
    "sanjay": ("elevenlabs_PboFIQ6kckfADmG3UQYF", "hindi — older, mature storyteller"),
    # English
    "jerry":  ("elevenlabs_QuzlkGvsGTSxjYyGssV4", "english — warm, friendly storyteller"),
    "micah":  ("elevenlabs_O27BicDGHNmYVJgbXOIj", "english — positive and engaging narrator"),
    "kate":   ("elevenlabs_poylg979ASDig8oTbLuu", "english — british female, social-media style"),
    "philip": ("elevenlabs_2styzLg7OSeuhPP6uQ26", "english — clear, measured, professional"),
    "rian":   ("elevenlabs_dtb2DcRUiZX01F9bvBi0", "english — young, high-energy explainer"),
    # English — gaming / YouTube commentary style
    "ethan":  ("elevenlabs_uB5dGadiIOtRLnqbQl9M", "english — fast, upbeat YouTube narrator (gaming)"),
    "aimoun": ("elevenlabs_WTKyEUnBEQlkDLD0tCBZ", "english — young YouTuber, friendly and energetic"),
    "jason":  ("elevenlabs_gwO2lpz0k1C1Hy0dytGO", "english — modern, fast-paced and direct"),
    "adam":   ("elevenlabs_N7ZPtMSz30kJ869b58WE", "english — calm, steady young male narrator"),
}
DEFAULT_AI33_VOICE = "sudhir"

# How many times one line is attempted before it is given up on. Failures here
# are almost always the connection rather than the request, and a single
# dropped one used to leave a silent gap that only showed up by reading the log
# to the end.
LINE_ATTEMPTS = 3
DEFAULT_AI33_MODEL = "eleven_multilingual_v2"

# edge-tts is the free fallback: no key, no credits, but only two Hindi voices.
DEFAULT_NARRATOR = {"voice": "hi-IN-MadhurNeural", "pitch": "-4Hz", "rate": "-4%", "volume": "+0%"}

# How long the character is assumed to speak before the narrator comes in, on
# scenes that have dialogue. Flow decides the real timing and does not report
# it, so this is a deliberate cushion rather than a measurement.
DIALOGUE_CUSHION_SEC = 5.0

# sidechaincompress ratio (valid range 1-20): how hard Flow's audio is pushed
# down while the narrator speaks. 1 is no ducking at all, 8 sits the narrator
# clearly in front while ambience stays audible underneath.
DUCK_RATIO = 8.0


def log(msg):
    print(f"[voice] {msg}", flush=True)


def have(tool):
    return shutil.which(tool) is not None


def duration_of(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
    )
    return float(out.stdout.strip())


async def speak_edge(text, out_path, cfg):
    try:
        import edge_tts
    except ImportError:
        sys.exit("edge-tts missing. Install it:  python -m pip install edge-tts\n"
                 "Or use the ai33 engine:  --engine ai33 --api-key <key>")
    tts = edge_tts.Communicate(
        text, cfg.get("voice", DEFAULT_NARRATOR["voice"]),
        rate=cfg.get("rate", "+0%"),
        pitch=cfg.get("pitch", "+0Hz"),
        volume=cfg.get("volume", "+0%"),
    )
    await tts.save(out_path)



def _voice_settings(settings, speed):
    """
    Build the voice_settings block, including pace.

    The API takes `speed` inside voice_settings. ElevenLabs documents 0.7-1.2,
    but this endpoint accepts higher — 1.27 was verified working — and gaming
    commentary wants the faster end. Doing it at generation time keeps the voice
    natural; speeding the finished mp3 up with ffmpeg raises the pitch and makes
    the narrator sound thin.
    """
    cfg = dict(settings or {})
    if speed and abs(float(speed) - 1.0) > 0.01:
        cfg["speed"] = max(0.5, min(1.5, float(speed)))
    return {"voice_settings": cfg} if cfg else {}


# The model clips the tail of a line intermittently — the same sentence came
# back at 7.7s with its closing word and at 7.1s without it, from identical
# input, so a line whose meaning rests on its final word is a coin flip.
#
# Padding the text was tried and does not fix it. An ellipsis renders as a
# pause, and a pause is still the last thing, so the real word stays exposed;
# padded and unpadded takes had near-identical silence maps. Padding with a
# throwaway sentence and cutting it back off is worse — the cut is placed by
# silence detection, which cannot tell the pad's leading gap from a pause
# inside the line, and it removed the real closing word instead.
#
# What works is writing the line so its last word is not load-bearing: end on
# a short closing phrase that can be lost without costing the meaning. That is
# a scripting rule, not something to paper over at synthesis time.


def _audio_url(task):
    """The finished audio's link in an ai33 task reply, wherever it is put."""
    meta = task.get("metadata") or {}
    for src in (meta, task):
        url = src.get("audio_url") or src.get("audioUrl")
        if url:
            return url
        for key in ("result_audios", "result_files", "results"):
            arr = src.get(key)
            if arr and isinstance(arr, list) and isinstance(arr[0], dict):
                url = arr[0].get("audioUrl") or arr[0].get("audio_url") or arr[0].get("url")
                if url:
                    return url
    return None


class TTSStillRunning(TimeoutError):
    """ai33 took the task but has not finished it. Carries the task id, so a
    retry waits for the same task instead of submitting - and paying for - a
    second one."""

    def __init__(self, task_id, timeout):
        super().__init__(f"TTS task {task_id} did not finish in {timeout}s")
        self.task_id = task_id


def speak_ai33(text, out_path, api_key, voice_id, model_id, settings=None, timeout=600,
               speed=None, task_id=None):
    """
    Generate one line through ai33's TTS.

    Same async shape as image generation — submit, poll, download — so the
    progress and failure behaviour stays familiar rather than inventing a
    second pattern for audio. With `task_id`, an earlier submission is waited
    on again rather than a new one sent.
    """
    if not task_id:
        resp = requests.post(
            f"{API_BASE}/v3/text-to-speech",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={"text": text, "voice_id": voice_id, "model_id": model_id,
                  **_voice_settings(settings, speed)},
            timeout=120,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(body.get("message") or json.dumps(body)[:200])
        task_id = body["task_id"]

    deadline = time.time() + timeout
    task = {}
    # A v3 task reports on /v3/task. The old /v1/task route sometimes never
    # moved past "running" for one, while ai33's own site showed the audio
    # finished - so the app waited out its timeout on a line already made.
    routes = [f"{API_BASE}/v3/task/{task_id}", f"{API_BASE}/v1/task/{task_id}"]
    url = None
    while time.time() < deadline:
        for route in list(routes):
            try:
                r = requests.get(route, headers={"xi-api-key": api_key}, timeout=30)
                if r.status_code == 404:
                    routes.remove(route)          # this route does not know the task
                    continue
                task = r.json() or {}
            except Exception:
                continue
            status = str(task.get("status") or "").lower()
            if status in ("error", "failed", "failure"):
                raise RuntimeError(task.get("error_message") or f"task {status}")
            url = _audio_url(task)
            if url or status in ("done", "completed", "complete", "success", "succeeded", "finished"):
                break
        if url or str(task.get("status") or "").lower() in (
                "done", "completed", "complete", "success", "succeeded", "finished"):
            break
        if not routes:
            routes = [f"{API_BASE}/v3/task/{task_id}", f"{API_BASE}/v1/task/{task_id}"]
        time.sleep(3)
    else:
        raise TTSStillRunning(task_id, timeout)

    url = url or _audio_url(task)
    if not url:
        raise RuntimeError("task finished but returned no audio url (keys: %s)"
                           % list((task.get("metadata") or task).keys()))

    # ai33's CDN refuses a download with no User-Agent (403).
    audio = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0 StickFilmStudio"})
    audio.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(audio.content)
    return task.get("credit_cost", 0)


def mux_narration(clip, narration, out_path, start_sec, duck=DUCK_RATIO):
    """
    Lay the narrator over the clip, ducking the original only while it speaks.

    sidechaincompress does the ducking: the narration drives a compressor on
    Flow's track, so the character's own voice stays at full level whenever the
    narrator is silent, and dips underneath only when they overlap. A flat
    volume cut would have muffled the whole clip instead.

    `duck` is that compressor's ratio, which ffmpeg only accepts between 1 and
    20 — it is not a volume multiplier, and passing one silently fails.
    """
    duck = min(20.0, max(1.0, float(duck)))
    delay_ms = int(max(0.0, start_sec) * 1000)
    # asplit is not optional: the narration feeds two places — the compressor's
    # sidechain and the final mix — and a filter output can only be consumed
    # once. Naming it twice without splitting produced a file that looked right
    # and measured right overall, while the narration itself was missing from
    # the very seconds it was meant to fill.
    filt = (
        f"[1:a]adelay={delay_ms}|{delay_ms},apad,asplit=2[nar_key][nar_mix];"
        f"[0:a][nar_key]sidechaincompress=threshold=0.05:ratio={duck}:"
        f"attack=20:release=400[duckedbg];"
        f"[duckedbg][nar_mix]amix=inputs=2:duration=first:normalize=0[a]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", clip, "-i", narration, "-filter_complex", filt,
         "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", out_path],
        check=True, creationflags=_NO_WINDOW,
    )


def main():
    ap = argparse.ArgumentParser(description="Generate and lay a narrator track over the scene clips")
    ap.add_argument("scenes_file")
    ap.add_argument("--scene", type=int, help="Only this scene_number")
    ap.add_argument("--mux", action="store_true", help="Also write clips with the narration applied")
    ap.add_argument("--duck", type=float, default=DUCK_RATIO,
                    help="Ducking strength while the narrator speaks: 1 = none, 20 = maximum (default 8)")
    ap.add_argument("--cushion", type=float, default=DIALOGUE_CUSHION_SEC,
                    help="Seconds to wait before narrating on scenes that have dialogue")
    ap.add_argument("--force", action="store_true", help="Regenerate audio that already exists")
    ap.add_argument("--engine", choices=["ai33", "edge"], default="ai33",
                    help="ai33 = ElevenLabs Hindi narrators (~73 credits/line). edge = free, 2 voices.")
    ap.add_argument("--api-key", default=os.environ.get("AI33_API_KEY"))
    ap.add_argument("--voice", default=None,
                    help=f"ai33 narrator: {', '.join(AI33_NARRATORS)} — or a full provider-prefixed voice_id")
    ap.add_argument("--tts-model", default=DEFAULT_AI33_MODEL)
    ap.add_argument("--speed", type=float, default=1.0,
                    help="Narration pace. 1.0 normal, 1.27 gaming-commentary fast.")
    ap.add_argument("--stability", type=float, default=None,
                    help="Lower = more variation and energy. 0.3 for gaming narration.")
    ap.add_argument("--style", type=float, default=None,
                    help="Higher = more emotion. 0.65 for gaming narration.")
    ap.add_argument("--list-voices", action="store_true", help="Print the ai33 narrator shortcuts and exit")
    args = ap.parse_args()

    if args.list_voices:
        for name, (vid, desc) in AI33_NARRATORS.items():
            print(f"  {name:<8} {desc:<38} {vid}")
        return

    if args.mux and not have("ffmpeg"):
        sys.exit("ffmpeg not found on PATH — needed for --mux.")

    out_dir = os.path.dirname(os.path.abspath(args.scenes_file))
    with open(args.scenes_file, encoding="utf-8-sig") as f:
        data = json.load(f)

    narrator = data.get("narrator") or DEFAULT_NARRATOR
    voice_dir = os.path.join(out_dir, "narration")
    os.makedirs(voice_dir, exist_ok=True)

    ai33_voice = None
    if args.engine == "ai33":
        if not args.api_key:
            sys.exit("ai33 engine needs a key: --api-key <key> (or set AI33_API_KEY), "
                     "or switch to the free engine with --engine edge")
        picked = args.voice or narrator.get("ai33_voice") or DEFAULT_AI33_VOICE
        ai33_voice = AI33_NARRATORS.get(picked, (picked, "custom"))[0]

    scenes = sorted(data["scenes"], key=lambda s: s["scene_number"])
    if args.scene is not None:
        scenes = [s for s in scenes if s["scene_number"] == args.scene]
        if not scenes:
            sys.exit(f"No scene {args.scene}.")

    if args.engine == "ai33":
        log(f"engine: ai33 ({args.tts_model}), voice {ai33_voice}")
    else:
        log(f"engine: edge-tts, voice {narrator.get('voice')} "
            f"{narrator.get('pitch','')} {narrator.get('rate','')}")

    made, muxed, overruns, credits = 0, 0, [], 0
    failed = []
    for scene in scenes:
        num = scene["scene_number"]
        text = (scene.get("narration") or "").strip()
        if not text:
            continue

        mp3 = os.path.join(voice_dir, f"scene_{num:02d}.mp3")
        if os.path.exists(mp3) and not args.force:
            log(f"scene {num}: narration already exists — skipping (use --force to redo)")
        else:
            # One dropped connection used to cost the whole line. A 22-line run
            # came back with 21 because a single ConnectTimeout landed on scene
            # 20, and the only way to notice was reading the log to the end —
            # everything downstream just carried on with a gap in it. Retried
            # here instead, since the failure is almost always the network
            # rather than the request.
            ok = False
            for attempt in range(1, LINE_ATTEMPTS + 1):
                try:
                    if args.engine == "ai33":
                        cfg = {}
                        if args.stability is not None:
                            cfg["stability"] = args.stability
                        if args.style is not None:
                            cfg["style"] = args.style
                            cfg.setdefault("similarity_boost", 0.75)
                        credits += speak_ai33(text, mp3, args.api_key, ai33_voice,
                                              args.tts_model, settings=cfg or None,
                                              speed=args.speed) or 0
                    else:
                        asyncio.run(speak_edge(text, mp3, narrator))
                    ok = True
                    break
                except Exception as e:
                    if attempt == LINE_ATTEMPTS:
                        log(f"scene {num}: FAILED — {type(e).__name__}: {e}")
                    else:
                        log(f"scene {num}: {type(e).__name__} — dobara koshish "
                            f"({attempt + 1}/{LINE_ATTEMPTS})")
                        time.sleep(4 * attempt)
            if not ok:
                failed.append(num)
                continue
            made += 1

        # Narration waits for the character when the scene has spoken dialogue.
        start = scene.get("narration_start")
        if start is None:
            start = args.cushion if (scene.get("dialogue") or "").strip() else 0.0

        secs = duration_of(mp3) if have("ffprobe") else 0
        budget = scene.get("duration_sec", 10)
        flag = ""
        if secs and start + secs > budget:
            overruns.append((num, start, secs, budget))
            flag = "  <-- clip se bahar nikal rahi hai"
        log(f"scene {num}: narration {secs:.1f}s starting at {start:.1f}s of {budget}s{flag}")

        if args.mux:
            clip_rel = scene.get("video_path")
            if not clip_rel:
                log(f"scene {num}: no video yet — muxing skipped")
                continue
            clip = os.path.join(out_dir, clip_rel)
            if not os.path.isfile(clip):
                log(f"scene {num}: clip missing on disk — muxing skipped")
                continue
            voiced_dir = os.path.join(out_dir, "clips_voiced")
            os.makedirs(voiced_dir, exist_ok=True)
            out_path = os.path.join(voiced_dir, f"scene_{num:02d}.mp4")
            mux_narration(clip, mp3, out_path, start, args.duck)
            muxed += 1

    log(f"Done — {made} narration file(s) in {voice_dir}"
        + (f", {muxed} clip(s) voiced" if args.mux else "")
        + (f", {credits} credits" if credits else ""))
    if overruns:
        log(f"WARNING: {len(overruns)} narration(s) run past the end of their clip and will be cut off:")
        for num, start, secs, budget in overruns:
            log(f"  scene {num}: starts {start:.1f}s + {secs:.1f}s speech = {start + secs:.1f}s vs {budget}s clip")
        log("  Narration chhoti karo, ya us scene ka \"narration_start\" kam karo.")


if __name__ == "__main__":
    main()

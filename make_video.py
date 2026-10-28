"""
STICKFILM 1-CLICK END-TO-END AUTOMATED PRODUCER
==============================================
Give it a script, and it produces a complete 1080p animated documentary video with:
- Authentic "meri eng" Minimax Voiceover
- WhisperX Timestamp Alignment (1-to-1 sentence rows)
- Story-Driven Action-First Image Prompts
- Automatic "NO [THING]" Red-Cross (❌) Stamping
- Attached Stickman Character Consistency
- Bulk Image Generation on Gemini Canvas (with Auto-Profile Rotation)
- 1080p Master Video Render
- 3 High-CTR Viral Thumbnails
- Ready-to-Upload upload.txt with Title options, Description, Chapters & Hashtags!

USAGE:
    python make_video.py "scripts/my_new_script.txt"
    python make_video.py "scripts/my_new_script.txt" --name "13_ORIGIN_OF_CHOCOLATE"
"""

import os
import sys
import json
import hashlib
import re
import time
import shutil
import zipfile
import subprocess
import argparse
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
VIDGEN_DIR = os.path.join(HERE, "vidgen")
BULKGEN_DIR = os.path.join(HERE, "bulkgen")
REFS_DIR = os.environ.get("STICKFILM_REFS") or os.path.join(VIDGEN_DIR, "refs")
PROJECTS_DIR = os.environ.get("STICKFILM_PROJECTS") or os.path.join(VIDGEN_DIR, "projects")
SCRIPTS_DIR = os.environ.get("STICKFILM_SCRIPTS") or os.path.join(HERE, "scripts")
_DEFAULT_PUB = r"D:\Video Projects"
if not os.path.isdir(os.path.splitdrive(_DEFAULT_PUB)[0] + os.sep):
    _DEFAULT_PUB = os.path.join(os.path.expanduser("~"), "Video Projects")
PUB_BASE = os.environ.get("STICKFILM_PUB_BASE") or _DEFAULT_PUB
local_profiles = os.path.join(HERE, "ai_avatar_pipeline", "browser_profiles")
if "STICKFILM_PROFILES" not in os.environ or not os.path.exists(os.environ.get("STICKFILM_PROFILES", "")):
    if os.path.exists(r"G:\auto_shorts_pipeline\ai_avatar_pipeline\browser_profiles"):
        os.environ["STICKFILM_PROFILES"] = r"G:\auto_shorts_pipeline\ai_avatar_pipeline\browser_profiles"
    else:
        os.environ["STICKFILM_PROFILES"] = local_profiles

for p_dir in [
    r"C:\Users\sting\AppData\Local\ffmpeg\bin",
    os.path.join(HERE, "tools"),
    os.path.dirname(sys.executable),
    r"D:\AI Autometa\bin",
]:
    if os.path.isdir(p_dir) and p_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p_dir + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, HERE)
sys.path.insert(0, VIDGEN_DIR)
sys.path.insert(0, BULKGEN_DIR)

from apply_negation_red_crosses import stamp_project_negations, draw_bold_cartoon_red_cross

def slugify(text):
    text = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"[-\s]+", "_", text).upper()

def relay(cmd, cwd=None):
    """Run a stage and pass every line it prints on to our own output.

    The dashboard starts this script with pythonw, which has no console, and a
    pythonw child left to inherit that gets nowhere to write: every stage ran
    silently and a failure showed only as "returned non-zero exit status 1",
    never the reason. Reading the child's output and printing it here puts it
    in the dashboard's console like any other line.
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    for line in proc.stdout:
        print(line.rstrip("\n"), flush=True)
    return proc.wait()


def run_cmd(cmd, cwd=HERE):
    print(f"\n[RUN] {' '.join(cmd)}", flush=True)
    code = relay(cmd, cwd=cwd)
    if code != 0:
        raise subprocess.CalledProcessError(code, cmd)

def generate_viral_thumbnails(pid, title_hook, out_dir):
    """Generates 3 high-CTR pro viral thumbnails with 3D cartoon typography and dynamic frames."""
    try:
        from pro_thumbnail_engine import compose_pro_thumbnail, save_thumbnail_pack
    except Exception as e:
        print(f"[THUMBNAIL] Could not import pro_thumbnail_engine: {e}")
        return

    pdir = os.path.join(PROJECTS_DIR, pid)
    img_dir = os.path.join(pdir, "images")
    if not os.path.isdir(img_dir):
        return

    images = sorted([f for f in os.listdir(img_dir) if f.endswith(".png")])
    if not images:
        return

    # 1. Smart candidate frame selection via prompts.json
    prompts_file = os.path.join(pdir, "prompts.json")
    chosen_indices = []
    
    if os.path.exists(prompts_file):
        try:
            with open(prompts_file, "r", encoding="utf-8", errors="ignore") as f:
                prompts_data = json.load(f)
            drama_kws = ["danger", "fire", "dead", "corpse", "fight", "escape", "blood", "poison", 
                         "secret", "shock", "mystery", "weird", "attack", "kill", "forbidden", "monster", "chaos"]
            scored = []
            for i, p_item in enumerate(prompts_data):
                txt = p_item.get("prompt", "").lower()
                sc = sum(1 for kw in drama_kws if kw in txt)
                if sc > 0 and i < len(images):
                    scored.append((sc, i))
            scored.sort(key=lambda x: -x[0])
            for _, idx in scored:
                if len(chosen_indices) >= 3:
                    break
                if all(abs(idx - ci) > 15 for ci in chosen_indices):
                    chosen_indices.append(idx)
        except Exception:
            pass

    # Fallback to distributed action frames (25%, 52%, 78%, avoiding peaceful intro shot)
    n_imgs = len(images)
    fallback_candidates = [
        int(n_imgs * 0.25),
        int(n_imgs * 0.52),
        int(n_imgs * 0.78),
    ]
    for fb in fallback_candidates:
        if len(chosen_indices) >= 3:
            break
        idx = max(1, min(fb, n_imgs - 1))
        if all(abs(idx - ci) > 10 for ci in chosen_indices):
            chosen_indices.append(idx)

    while len(chosen_indices) < 3 and len(chosen_indices) < n_imgs:
        idx = (len(chosen_indices) + 1) * (n_imgs // 4)
        chosen_indices.append(min(idx, n_imgs - 1))

    candidates = [os.path.join(img_dir, images[i]) for i in chosen_indices]

    # 2. Generate 3 distinct short, viral psychology hooks (2-4 words max)
    clean_title = pid.replace("_", " ").strip()
    words = clean_title.split()
    subject = words[-1].upper() if words else "THIS"
    
    hooks_and_themes = [
        ("THE SECRET TRUTH", "yellow_gold"),
        ("CAN YOU SURVIVE?", "danger_red"),
        (f"WHY {subject}?!", "electric_cyan")
    ]
    
    title_upper = clean_title.upper()
    if "ROME" in title_upper or "ROMAN" in title_upper:
        hooks_and_themes[0] = ("24 HOURS IN ROME", "yellow_gold")
        hooks_and_themes[1] = ("CAN YOU SURVIVE?", "danger_red")
        hooks_and_themes[2] = ("THE DIRTY TRUTH", "electric_cyan")
    elif "PLAGUE" in title_upper or "POISON" in title_upper or "DEATH" in title_upper:
        hooks_and_themes[0] = ("PATIENT ZERO", "danger_red")
        hooks_and_themes[1] = ("DO NOT ENTER!", "danger_red")
        hooks_and_themes[2] = ("FATAL MISTAKE?!", "yellow_gold")
    elif "MYST" in title_upper or "SECRET" in title_upper:
        hooks_and_themes[0] = ("FORBIDDEN SECRET", "danger_red")
        hooks_and_themes[1] = ("WHAT HAPPENED?!", "yellow_gold")
        hooks_and_themes[2] = ("HIDDEN TRUTH", "electric_cyan")

    out_dirs = [out_dir, pdir]
    os.makedirs(out_dir, exist_ok=True)

    for i, (cand_path, (hook, theme)) in enumerate(zip(candidates, hooks_and_themes), 1):
        try:
            raw_img = Image.open(cand_path)
            pro_thumb = compose_pro_thumbnail(
                base_img=raw_img,
                text=hook,
                font_size=155,
                tilt=-3.0,
                theme=theme,
                y_offset=30
            )
            save_thumbnail_pack(
                final_img=pro_thumb,
                out_dirs=out_dirs,
                option_num=i,
                is_primary=(i == 1)
            )
            print(f"[THUMBNAIL] Generated Pro Option {i}: '{hook}' ({theme})")
        except Exception as e:
            print(f"[THUMBNAIL ERROR] Option {i}: {e}")

def create_upload_txt(pid, script_text, out_dir):
    """Generates ready-to-publish upload.txt."""
    lines = [l.strip() for l in script_text.splitlines() if l.strip()]
    first_few = " ".join(lines[:4])
    clean_title = pid.replace("_", " ").title()

    upload_content = f"""TITLE OPTIONS:
1. {clean_title} (The Untold Animated Story)
2. Who Actually Made The First One?
3. How Humanity Discovered {clean_title.split()[-1]}

DESCRIPTION:
{first_few}

In this animated documentary, we explore the incredible, hilarious, and mysterious history behind one of humanity's greatest everyday wonders.

CHAPTERS:
00:00 - Introduction & The Mystery
02:15 - Ancient Origins
05:30 - Accidental Discoveries
09:10 - How It Spread Across The World
14:20 - Modern Evolution

#History #AnimatedDocumentary #Stickman #Documentary #Storytelling #{pid.replace('_', '')}
"""
    with open(os.path.join(out_dir, "upload.txt"), "w", encoding="utf-8") as f:
        f.write(upload_content)
    print(f"[METADATA] Wrote {os.path.join(out_dir, 'upload.txt')}")

def resolve_project_name(name):
    if not name:
        return name
    candidates = [name, name.replace("_", " "), name.replace(" ", "_"), slugify(name)]
    for c in candidates:
        if os.path.isdir(os.path.join(PROJECTS_DIR, c)):
            return c
    return name

def _publish_copy(src, dest):
    """Copy the finished film into Deliverables whole: to a side name, then
    one rename, so the page never lists a copy that is still being written."""
    part = dest + ".part"
    shutil.copyfile(src, part)
    os.replace(part, dest)


def _voice_refresh_reason(pdir, script_path, voice, speed):
    """Return why an existing voiceover cannot be reused, or ``""`` if safe."""
    v_file = os.path.join(pdir, "voiceover.mp3")
    if not os.path.isfile(v_file) or os.path.getsize(v_file) <= 100 * 1024:
        return "voiceover is missing"
    try:
        with open(script_path, "r", encoding="utf-8-sig") as f:
            script = f.read()
        with open(os.path.join(pdir, "state.json"), encoding="utf-8") as f:
            state = json.load(f) or {}
    except (OSError, ValueError):
        return "voice metadata is missing"

    saved_hash = state.get("script_sha256")
    current_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if saved_hash and saved_hash != current_hash:
        return "the script changed after the voiceover was made"

    # Legacy projects have no hash yet; their saved piece count still catches
    # the common edited-script case without regenerating every old film.
    try:
        import voice as voice_stage
        expected = len(voice_stage.split_script(script))
        saved_pieces = state.get("pieces")
        parts_dir = os.path.join(pdir, "voice_parts")
        matching = voice_stage.source_part_paths(
            parts_dir, expected, state.get("speed", 1.0))
        if saved_pieces is not None and int(saved_pieces) != expected:
            return "the script has a different number of voice parts"
        if any(not os.path.isfile(path) for path in matching):
            return "the voice-part files do not match the script"
    except OSError:
        return "the voice-part folder is missing"
    except (ValueError, TypeError, ImportError):
        pass

    requested = (voice or "").strip()
    saved_voice = (state.get("voice_id") or "").strip()
    if saved_voice and requested.startswith(("clone_", "elevenlabs_", "minimax_")) \
            and requested != saved_voice:
        return "the selected voice changed"
    saved_speed = state.get("speed")
    if saved_speed is not None and abs(float(saved_speed) - float(speed or 1.0)) > 0.005:
        return "the voice speed changed"
    return ""


def _invalidate_after_voice_change(pdir):
    """Make every downstream stage follow the newly regenerated narration."""
    for name in ("words.json", "segments.json", "timestamps.md", "prompts.txt", "final.mp4"):
        path = os.path.join(pdir, name)
        if os.path.isfile(path):
            os.remove(path)
    for name in ("images", "images_raw", "downloads", "clips"):
        path = os.path.join(pdir, name)
        if not os.path.isdir(path):
            continue
        backup = os.path.join(pdir, name + "_script_changed_backup")
        shutil.rmtree(backup, ignore_errors=True)
        shutil.move(path, backup)
        os.makedirs(path, exist_ok=True)
    print("[AUTO REFRESH] Voiceover changed; cleared stale timestamps, prompts, images and clips.")


def produce_video(script_path, pid=None, profile="acc1", batch_size=35, clips=0, ref=None, voice="meri eng", look_mode=None, placement="front", aspect="16:9", speed=1.0, target=None):
    if not os.path.isfile(script_path):
        sys.exit(f"Error: Script file '{script_path}' not found!")

    with open(script_path, "r", encoding="utf-8") as f:
        script_text = f.read().strip()

    if not pid:
        base_name = os.path.splitext(os.path.basename(script_path))[0]
        pid = resolve_project_name(base_name)

    else:
        pid = resolve_project_name(pid)

    print(f"\n=======================================================")
    print(f"🎬 PRODUCTION PIPELINE FOR: {pid}")
    print(f"=======================================================\n")

    pdir = os.path.join(PROJECTS_DIR, pid)
    os.makedirs(pdir, exist_ok=True)

    # Check if this video is ALREADY COMPLETELY FINISHED!
    final_mp4 = os.path.join(pdir, "final.mp4")
    img_dir = os.path.join(pdir, "images")
    has_imgs = os.path.isdir(img_dir) and len([f for f in os.listdir(img_dir) if f.lower().endswith(('.png', '.jpg', '.webp'))]) > 20
    if os.path.isfile(final_mp4) and os.path.getsize(final_mp4) > 1024 * 1024 and has_imgs:
        print(f"\n=======================================================")
        print(f"✅ PROJECT ALREADY COMPLETE: {pid}")
        print(f"🎬 1080p Master Video '{final_mp4}' already exists ({os.path.getsize(final_mp4)/(1024*1024):.1f} MB)!")
        print(f"Skipping re-generation to protect completed work!")
        print(f"=======================================================\n")
        pub_dir = os.path.join(PUB_BASE, pid)
        os.makedirs(pub_dir, exist_ok=True)
        dest_mp4 = os.path.join(pub_dir, f"{pid}.mp4")
        if not os.path.isfile(dest_mp4):
            _publish_copy(final_mp4, dest_mp4)
        generate_viral_thumbnails(pid, pid.replace("_", " "), pub_dir)
        create_upload_txt(pid, script_text, pub_dir)
        return
    
    # 1. Save script into project
    with open(os.path.join(pdir, "script.txt"), "w", encoding="utf-8") as f:
        f.write(script_text)

    # 2. Setup Look & Character Reference
    lm = (look_mode or "").lower()
    realistic = any(k in lm for k in ("real", "document", "cinema", "photo"))

    # The reference the buyer actually chose. A CLEARED reference stays cleared —
    # it is never silently replaced with the built-in stickman (that swap is the
    # bug where a documentary run still pulled a stickman sheet in).
    char_ref = ""
    if ref:
        if os.path.isabs(ref):
            char_ref = ref
        elif os.path.isfile(os.path.join(REFS_DIR, ref)):
            char_ref = os.path.join(REFS_DIR, ref)
        else:
            char_ref = os.path.abspath(ref)

    # A category the buyer made from a prompt carries its own words and its own
    # motion (Vox board or slow zoom). Read it first: it decides the look, and
    # it must never fall into the stickman branch below just because its name
    # says neither "3d" nor "realistic".
    custom = {}
    if look_mode:
        looks_dir = os.environ.get("STICKFILM_LOOKS") or os.path.join(VIDGEN_DIR, "looks")
        cfile = os.path.join(looks_dir, f"{look_mode}.json")
        try:
            with open(cfile, encoding="utf-8") as f:
                cand = json.load(f) or {}
            if cand.get("custom"):
                custom = cand
        except (OSError, ValueError):
            pass

    ref_name = os.path.basename(char_ref).lower() if char_ref else ""
    is_3d = (not realistic) and (("3d" in lm) or any(
        k in ref_name for k in ["3d", "chatgpt", "pixar", "cgi", "nanni", "neanderthal", "first series"]))
    modern = (not realistic) and (not is_3d) and ("modern" in lm)
    stickman = (not realistic) and (not is_3d) and (not modern)

    # Only a stickman look falls back to the built-in stickman sheet when no
    # reference was given. Realistic (documentary), modern and 3D never pull a
    # stickman in — a cleared reference means "no character sheet".
    if not char_ref and stickman and not custom:
        cand = os.path.join(REFS_DIR, "clean_stickman_ref.png")
        if os.path.isfile(cand):
            char_ref = cand
    if char_ref and not os.path.isfile(char_ref):
        char_ref = ""

    if realistic:
        style_prompt = "photorealistic cinematic documentary film still, natural realistic lighting, real-world textures, shallow depth of field, filmic color grade, no text, no letters, no watermark, no split panels, no comic grid"
        char_prompt = ""
        neg_prompt = "no cartoon, no stickman, no 2D, no flat vector art, no drawing, no illustration, no anime, no text, no letters, no watermark, no split panels, no comic grid"
    elif is_3d:
        style_prompt = "3D animated film still, stylised 3D CGI render in the look of a modern animated feature, Pixar 3D animation style, soft global illumination, gentle depth of field, rounded shapes, warm cinematic lighting, clean uncluttered composition, high detail"
        char_prompt = "Stylized 3D CGI cartoon character matching the uploaded 3D reference sheet, consistent 3D model, Pixar animated feature film quality, expressive features, rich textures"
        neg_prompt = "no 2D cartoon, no stickman, no flat vector art, no drawing, no sketch, no line art, no text, no letters, no numbers, no watermark, no signature, no photorealistic human skin, no uncanny realistic faces, no split panels, no comic grid"
    elif modern:
        style_prompt = "clean modern 2D cartoon illustration, contemporary flat vector style, bold outlines, bright flat colours, simple uncluttered composition, no text, no letters, no watermark, no split panels, no comic grid"
        char_prompt = "consistent modern 2D cartoon character matching the uploaded reference" if char_ref else ""
        neg_prompt = "no stickman, no text, no letters, no watermark, no realistic human faces, no split panels, no comic grid"
    else:
        style_prompt = "flat 2D cartoon stickman illustration in the Moronomy style, thick bold black outlines, muted earthy historical colors, clean vector art, no text, no letters, no watermark, no split panels, no comic grid."
        if "rome" in pid.lower() or "roman" in pid.lower():
            style_prompt = "flat 2D cartoon stickman illustration in the Moronomy style, thick bold black outlines, ancient Roman historical setting, muted earthy warm parchment aesthetic, no text, no letters, no watermark, no split panels, no comic grid."
        char_prompt = ""
        neg_prompt = "no text, no letters, no watermark, no realistic human faces, no split panels, no comic grid"

    # The built-in names above keep their hand-tuned wording; a custom category
    # overrides the style only. Its "character" is never used: that was the
    # subject of the prompt it was made from ("an elephant in a hoodie"), and
    # carrying it over put the same elephant into every later film. Who is in
    # a film comes from that film's script and its own reference, nothing else.
    if custom:
        style_prompt = custom.get("style") or style_prompt
        char_prompt = ("the recurring character matches the uploaded reference image, "
                       "drawn in this style") if char_ref else ""
        neg_prompt = custom.get("negative") or neg_prompt
        print(f"[look] custom category {custom.get('label', look_mode)!r} "
              f"(motion: {custom.get('motion', 'kenburns')})", flush=True)

    look_data = {
        "style": style_prompt,
        "character": char_prompt,
        "character_ref": os.path.abspath(char_ref) if char_ref else "",
        "style_ref": "",
        "negative": neg_prompt,
        "aspect": aspect
    }
    if custom.get("motion") == "board":
        look_data["motion"] = "board"
    with open(os.path.join(pdir, "look.json"), "w", encoding="utf-8") as f:
        json.dump(look_data, f, indent=2)

    # Save settings aspect
    s_file = os.path.join(pdir, "settings.json")
    cur_s = {}
    if os.path.isfile(s_file):
        try:
            with open(s_file, "r", encoding="utf-8") as f:
                cur_s = json.load(f)
        except Exception:
            pass
    cur_s["aspect"] = aspect
    with open(s_file, "w", encoding="utf-8") as f:
        json.dump(cur_s, f, indent=2)


    # 3. Stage: Voiceover. Re-run automatically when the script/voice no longer
    # matches the saved narration, so Launch Full Video is enough after an edit.
    v_file = os.path.join(pdir, "voiceover.mp3")
    voice_reason = _voice_refresh_reason(pdir, os.path.join(pdir, "script.txt"), voice, speed)
    voice_regenerated = bool(voice_reason)
    if not voice_reason:
        print(f"\n[SKIP STEP 1] Voiceover '{v_file}' already exists ({os.path.getsize(v_file)/(1024*1024):.1f} MB). Skipping TTS.")
    else:
        detail = f" ({voice_reason}; regenerating automatically)" if os.path.isfile(v_file) else ""
        print(f"\n--- STEP 1: GENERATING VOICEOVER ({voice}){detail} ---")
        vcmd = [sys.executable, os.path.join(VIDGEN_DIR, "pipeline.py"), "voice", pid, "--voice", voice]
        # The length the buyer chose: spoken at the pace that gets near it,
        # then fitted exactly.
        if speed and abs(float(speed) - 1.0) > 0.005:
            vcmd += ["--speed", "%.3f" % float(speed)]
        if target:
            vcmd += ["--target", "%.1f" % float(target)]
        if os.path.isfile(v_file):
            vcmd += ["--force"]
        run_cmd(vcmd)
        if voice_regenerated:
            _invalidate_after_voice_change(pdir)

    # 4. Stage: Align Timestamps (Skip if already generated!)
    seg_file = os.path.join(pdir, "segments.json")
    if os.path.isfile(seg_file) and os.path.getsize(seg_file) > 100:
        print(f"\n[SKIP STEP 2] Timestamps '{seg_file}' already exist. Skipping Whisper alignment.")
    else:
        print(f"\n--- STEP 2: ALIGNING TIMESTAMPS (Placement: {placement}, Clips: {clips}) ---")
        run_cmd([sys.executable, os.path.join(VIDGEN_DIR, "pipeline.py"), "rows", pid, "--clips", str(clips), "--placement", placement])

    # 5. Stage: Generate 1-to-1 Story Prompts. Regenerate whenever the chosen
    # style differs from what the existing prompts were written in — otherwise a
    # style switch silently reuses the old look (e.g. picking Realistic or 3D but
    # keeping the old stickman prompts, which is the "still 2D" bug).
    p_file = os.path.join(pdir, "prompts.txt")
    target_family = "realistic" if realistic else ("3d" if is_3d else "stickman")
    recreate_prompts = False
    if os.path.isfile(p_file) and os.path.getsize(p_file) > 100:
        with open(p_file, "r", encoding="utf-8", errors="ignore") as pf:
            first_l = pf.readline().lower()
        # A 3D prompt ends "no 2D cartoon, no stickman, ... no photorealistic
        # human skin". Matching words anywhere read that as a stickman prompt,
        # so every 3D run "changed style", cleared finished images and drew
        # them again. Drop the "no ..." clauses, then the style named first
        # is the one the prompt was written in.
        said = re.sub(r"\bno\s+[^,.;]*", " ", first_l)
        found = []
        for family, words in (("stickman", ("moronomy", "stickman")),
                              ("3d", ("3d animated", "cgi", "pixar")),
                              ("realistic", ("photorealistic", "documentary", "cinematic"))):
            hits = [said.find(w) for w in words if w in said]
            if hits:
                found.append((min(hits), family))
        existing_family = min(found)[1] if found else "unknown"
        if existing_family != "unknown" and existing_family != target_family:
            print(f"\n[PROMPTS UPDATE] Style changed ({existing_family} -> {target_family}). Regenerating prompts...")
            recreate_prompts = True
        else:
            # Prompts from the old offline fallback never described the
            # narration (four rotating canned poses). Write real ones.
            with open(p_file, "r", encoding="utf-8", errors="ignore") as pf:
                body = pf.read()
            canned = ("stick figure explaining with open hands gesture",
                      "thoughtful stick figure looking toward camera",
                      "stick figure in dynamic stance pointing to key concept",
                      "stick figure standing confidently with slight tilt")
            if sum(body.count(c) for c in canned) >= 3:
                print("\n[PROMPTS UPDATE] Existing prompts are generic placeholder poses "
                      "that ignore the narration. Rewriting them scene by scene...")
                recreate_prompts = True
    else:
        recreate_prompts = True

    if recreate_prompts:
        print("\n--- STEP 3: CREATING ACTION-FIRST PROMPTS ---")
        run_cmd([sys.executable, os.path.join(VIDGEN_DIR, "pipeline.py"), "prompts", pid, "--mode", "template", "--look", os.path.join(pdir, "look.json")])
        # When prompts are recreated for a style switch (e.g. 2D to 3D), old images belong to previous style!
        for folder_name in ("images", "images_raw", "downloads"):
            old_f = os.path.join(pdir, folder_name)
            if os.path.isdir(old_f) and os.listdir(old_f):
                bk = os.path.join(pdir, f"{folder_name}_2d_backup")
                shutil.rmtree(bk, ignore_errors=True)
                shutil.copytree(old_f, bk)
                shutil.rmtree(old_f, ignore_errors=True)
                os.makedirs(old_f, exist_ok=True)
                print(f"[STYLE SYNC] Backed up and cleared stale {folder_name}/ for fresh 3D generation.")
    else:
        print(f"\n[SKIP STEP 3] Prompts '{p_file}' already exist. Skipping prompt generation.")


    # 6. Stage: Generate Images on Gemini Canvas (Skip if all images exist!)
    row_count = 0
    if os.path.isfile(seg_file):
        try:
            with open(seg_file, "r", encoding="utf-8") as f:
                row_count = len(json.load(f))
        except Exception:
            pass

    existing_imgs = [f for f in os.listdir(img_dir)] if os.path.isdir(img_dir) else []
    valid_existing = [f for f in existing_imgs if f.lower().endswith(('.png', '.jpg', '.webp')) and os.path.getsize(os.path.join(img_dir, f)) > 30000]

    if row_count > 0 and len(valid_existing) >= row_count:
        print(f"\n[SKIP STEP 4] All {len(valid_existing)}/{row_count} images already rendered! Skipping Canvas generation.")
    else:
        print(f"\n--- STEP 4: GENERATING IMAGES ON GEMINI CANVAS (Profile: {profile}, Batch: {batch_size}) ---")
        run_cmd([sys.executable, os.path.join(VIDGEN_DIR, "pipeline.py"), "images", pid, "--profile", profile, "--aspect", aspect, "--batch", str(batch_size), "--resume"])

    # 7. Stage: Stamp Automatic Negations (❌)
    print("\n--- STEP 5: APPLYING AUTOMATIC NEGATION RED CROSS (❌) STAMPS ---")
    stamp_project_negations(pid)

    # 8. STEP 6: MANDATORY IMAGE VERIFICATION (Zero-Blank Policy)
    print("\n--- STEP 6: VERIFYING ALL IMAGES (No Blank/Corrupt Frames Allowed) ---")
    img_dir = os.path.join(pdir, "images")
    if not os.path.isdir(img_dir):
        sys.exit(f"❌ Verification failed: images directory '{img_dir}' does not exist!")

    all_imgs = [f for f in os.listdir(img_dir) if f.lower().endswith((".png", ".jpg", ".webp"))]
    if not all_imgs:
        sys.exit(f"❌ Verification failed: No images found in '{img_dir}'!")

    blank_imgs = []
    suspicious_imgs = []
    for f in all_imgs:
        fpath = os.path.join(img_dir, f)
        sz = os.path.getsize(fpath)
        if sz == 39370 or sz < 15000:
            blank_imgs.append((f, sz))
        elif sz < 50000:
            suspicious_imgs.append((f, sz))

    print(f"[VERIFY] Total images: {len(all_imgs)}")
    print(f"[VERIFY] Blank (transparent/empty): {len(blank_imgs)}")
    print(f"[VERIFY] Suspiciously small (<50KB): {len(suspicious_imgs)}")

    if blank_imgs:
        print("\n" + "="*60)
        print("❌ CRITICAL ERROR: BLANK IMAGES DETECTED! STOPPING RENDER.")
        print("="*60)
        for name, sz in blank_imgs[:10]:
            print(f"  - {name}: {sz} bytes (blank canvas)")
        if len(blank_imgs) > 10:
            print(f"  ... and {len(blank_imgs) - 10} more blank images.")
        print("\nPipeline stopped to avoid producing a broken video.")
        print("Run images stage again with clean profile to re-fetch images.")
        sys.exit(1)

    print("\n✅ VERIFICATION PASSED: All images are valid. Proceeding to render!")

    # 8B. Stage: Generate Animated Video Clips for Opening Hook Shots (OmniFlows)
    if clips > 0:
        print(f"\n--- STEP 6B: GENERATING {clips} ANIMATED HOOK CLIPS VIA OMNIFLOWS (Placement: {placement}) ---")
        clips_dir = os.path.join(pdir, "clips")
        existing_clips = [f for f in os.listdir(clips_dir) if f.endswith(".mp4")] if os.path.isdir(clips_dir) else []
        if len(existing_clips) >= clips:
            print(f"[SKIP STEP 6B] All {len(existing_clips)}/{clips} video clips already exist! Skipping OmniFlows generation.")
        else:
            try:
                # pipeline.py's `clips` stage forwards straight to omni_videos.py,
                # whose --aspect only accepts "landscape"/"portrait" — not the
                # "16:9"/"9:16" ratio this script and the UI use everywhere else.
                clip_aspect = {"16:9": "landscape", "9:16": "portrait"}.get(aspect, "landscape")
                run_cmd([
                    sys.executable, os.path.join(VIDGEN_DIR, "pipeline.py"), "clips", pid,
                    "--aspect", clip_aspect, "--model", "omniflash", "--limit", str(clips)
                ])
                print(f"✅ Finished generating opening animated video clips via OmniFlows!")
            except Exception as e:
                print(f"\n[CLIPS NOTICE] OmniFlows clip generation note: {e}")
                print("Rendering will proceed seamlessly using cinematic pan/zoom still animation where clips are pending.\n")

    # 9. Stage: Render Final 1080p Video
    print(f"\n--- STEP 7: RENDERING MASTER VIDEO (Aspect: {aspect}) ---")
    run_cmd([sys.executable, os.path.join(VIDGEN_DIR, "pipeline.py"), "render", pid, "--aspect", aspect, "--jobs", "8"])

    # 10. Copy final video to Deliverables Directory
    pub_dir = os.path.join(PUB_BASE, pid)
    os.makedirs(pub_dir, exist_ok=True)

    src_mp4 = os.path.join(pdir, "final.mp4")
    dest_mp4 = os.path.join(pub_dir, f"{pid}.mp4")
    _publish_copy(src_mp4, dest_mp4)
    print(f"\n[MASTER VIDEO] Published to: {dest_mp4} ({os.path.getsize(dest_mp4)/1024/1024:.1f} MB)")

    # 11. Generate Thumbnails & Metadata
    print("\n--- STEP 8: GENERATING VIRAL THUMBNAILS & UPLOAD METADATA ---")
    generate_viral_thumbnails(pid, pid.replace("_", " "), pub_dir)
    create_upload_txt(pid, script_text, pub_dir)

    print("\n=======================================================")
    print(f"🎉 PRODUCTION COMPLETE! DELIVERABLES READY IN:")
    print(f"👉 {pub_dir}")
    print("=======================================================\n")

def main():
    parser = argparse.ArgumentParser(description="Stickfilm 1-Click Automated Video Producer")
    parser.add_argument("script", help="Path to narration script txt file")
    parser.add_argument("--name", "-n", default=None, help="Custom project ID name (e.g. 13_FIRST_CHOCOLATE)")
    parser.add_argument("--profile", "-p", default="acc1", help="Browser profile to start with (acc1-acc15)")
    parser.add_argument("--batch", "-b", type=int, default=35, help="Batch size for Canvas generation")
    parser.add_argument("--clips", "-c", type=int, default=0, help="Number of animated clips to mix into film (default: 0 = pure stills)")
    parser.add_argument("--placement", default="front", choices=["front", "spread", "manual"], help="Placement of animated clips (default: front for opening hook scenes)")
    parser.add_argument("--ref", "-r", "--character-ref", dest="ref", default=None, help="Custom character reference image path or filename in refs/")
    parser.add_argument("--voice", "-v", default="meri eng", help="Voice model/clone name (default: meri eng)")
    parser.add_argument("--look", "-l", "--style", dest="look", default=None, help="Visual style preset: '3d' or '2d' (auto-detected from ref if omitted)")
    parser.add_argument("--aspect", "-a", default="16:9", choices=["16:9", "9:16"], help="Video aspect ratio: 16:9 (Landscape) or 9:16 (Vertical Shorts/Reels)")
    parser.add_argument("--speed", type=float, default=1.0, help="Voice pace (1.0 = the voice's own)")
    parser.add_argument("--target", type=float, default=None, help="Seconds the narration should last")
    args = parser.parse_args()

    produce_video(args.script, pid=args.name, profile=args.profile, batch_size=args.batch, clips=args.clips, ref=args.ref, voice=args.voice, look_mode=args.look, placement=args.placement, aspect=args.aspect, speed=args.speed, target=args.target)


if __name__ == "__main__":
    main()

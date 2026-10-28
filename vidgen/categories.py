"""New categories from a prompt.

The user describes a look; Gemini writes the category (style, what to avoid,
and whether stills move on the Vox board) from the instructions in `_brief`,
and `_finish` checks and completes the answer. Before anything is saved, three
sample pictures are drawn with the same engine a film uses - the images stage
in the browser - so what the user approves is what their films will get.
Saving puts the category in the user's looks folder, where the style menu and
make_video.py find it.
"""
import base64
import binascii
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def looks_dir():
    return os.environ.get("STICKFILM_LOOKS") or os.path.join(HERE, "looks")


def projects_dir():
    return os.environ.get("STICKFILM_PROJECTS") or os.path.join(HERE, "projects")


def drafts_dir():
    d = os.path.join(looks_dir(), "_drafts")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_slug(slug):
    slug = re.sub(r"[^a-z0-9_]", "", (slug or "").lower())
    if not slug.startswith("custom_"):
        raise ValueError("not a category made here")
    return slug


def preview_pid(slug):
    return "_preview_" + slug


def _read(path, fallback=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class GeminiBusy(Exception):
    """Gemini answered, but with an error instead of a category."""


BUSY_MESSAGE = ("Google's Gemini is busy right now (it answered with an error on the API and "
                "in your Chrome profile). Please try again in a few minutes.")


LOCAL_MODELS = ("gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3.6-flash",
                "gemini-3.5-flash", "gemini-flash-latest")

_BASE_NEGATIVE = ["no text", "no letters", "no watermark", "no split panels", "no comic grid"]


def _brief(prompt):
    """The instructions Gemini gets to turn the user's sentence into a category."""
    return (
        "You design reusable visual styles for an app that turns a narrated script into "
        "an illustrated video (one picture per spoken line, animated with slow camera moves).\n"
        "The user describes the look they want:\n\n"
        f"\"{(prompt or '').strip()}\"\n\n"
        "Write ONE category as a JSON object with exactly these keys:\n"
        "- label: a short name for the style, 2-4 words, Title Case.\n"
        "- style: 45-90 words of comma-separated image-generation style phrases. Start with "
        "the medium (e.g. 'flat 2D vector illustration', '3D animated film still', "
        "'photorealistic documentary photograph', 'watercolor painting'), then rendering, "
        "line work, palette, lighting, texture and composition. Describe HOW pictures look, "
        "never WHAT they show (no people, places or story).\n"
        "- negative: 6-12 comma-separated 'no ...' phrases of things this style must avoid "
        "(always include no text, no letters, no watermark, no split panels, no comic grid).\n"
        "- motion: 'board' if the style is a paper / cut-out / collage / Vox-explainer look "
        "where pictures should slide in as cards, otherwise 'kenburns'.\n"
        "- samples: an array of exactly 3 short scene descriptions (8-20 words each) about "
        "the user's topic, used to draw 3 preview pictures in this style: one wide "
        "establishing scene, one person doing something, one close-up detail.\n"
        "Reply with the JSON object only.")


def _finish(prompt, answer):
    """Check and complete Gemini's answer into a saved-category shape."""
    text = str(answer or "")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0) if m else text)
    except ValueError:
        raise GeminiBusy("Gemini's answer was not a readable category")
    if not isinstance(data, dict):
        raise GeminiBusy("Gemini's answer was not a readable category")
    style = " ".join(str(data.get("style") or "").split())
    if len(style) < 20:
        raise GeminiBusy("Gemini did not describe a style")
    label = " ".join(str(data.get("label") or "").split())[:60] or "Custom Look"
    neg = [x.strip() for x in re.split(r"[,;]", str(data.get("negative") or "")) if x.strip()]
    for base in _BASE_NEGATIVE:
        if not any(base in n.lower() for n in neg):
            neg.append(base)
    samples = [" ".join(str(s).split()) for s in (data.get("samples") or []) if str(s).strip()][:3]
    while len(samples) < 3:
        samples.append(["wide establishing scene", "a person in a moment",
                        "close-up of an important detail"][len(samples)]
                       + " about " + (" ".join((prompt or "everyday life").split())[:80]))
    motion = "board" if str(data.get("motion") or "").strip().lower() == "board" else "kenburns"
    slug = "custom_" + (re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "look")
    return {"label": label, "slug": slug, "custom": True, "needs_ref": False,
            "motion": motion, "style": style, "character": "",
            "negative": ", ".join(neg), "samples": samples}


def _keys():
    """Every saved Gemini key, in order (the API Keys window's list)."""
    try:
        from common import gemini_keys
        return list(gemini_keys())
    except Exception:
        try:
            from common import gemini_key
            k = gemini_key()
            return [k] if k else []
        except Exception:
            return []


def _reference_parts(images):
    """Validated Gemini inline-image parts from the dashboard upload."""
    out = []
    for image in (images or [])[:4]:
        if not isinstance(image, dict):
            continue
        mime = str(image.get("mime") or "").lower()
        data = str(image.get("data") or "")
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise ValueError("Reference images must be JPG, PNG or WebP.")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("One reference image could not be read. Upload it again.")
        if not raw or len(raw) > 2 * 1024 * 1024:
            raise ValueError("Each reference image must be under 2 MB after resizing.")
        out.append({"inline_data": {"mime_type": mime,
                                     "data": base64.b64encode(raw).decode("ascii")}})
    return out


def _draft_from_this_pc(prompt, key, reference_images=None):
    """The category written by the Gemini API. Every saved key is tried in
    turn, so one key's used-up quota does not stop it."""
    import time
    import requests
    brief = _brief(prompt)
    image_parts = _reference_parts(reference_images)
    if image_parts:
        brief += ("\n\nStudy the attached reference images together. Extract only their reusable "
                  "visual language: medium, rendering, palette, lighting, texture, line work "
                  "and composition. Do not copy their people, characters, clothing, objects, "
                  "locations, text or story into the category style. Keep all three sample "
                  "descriptions about the buyer's requested topic, not the reference subjects.")
    body = {"contents": [{"role": "user", "parts": [{"text": brief}] + image_parts}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 16384,
                                 "responseMimeType": "application/json"}}
    keys = [k for k in dict.fromkeys([key] + _keys()) if k]
    tried, refused = [], 0
    for rnd in range(2):
        if rnd:
            time.sleep(6)          # Google's 503s come in bursts
        for n, k in enumerate(keys, 1):
            for model in LOCAL_MODELS:
                try:
                    r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/"
                                      "%s:generateContent?key=%s" % (model, k), json=body, timeout=90)
                except Exception as exc:
                    tried.append("key %d %s: %s" % (n, model, str(exc)[:60]))
                    continue
                if r.status_code == 200:
                    try:
                        parts = r.json()["candidates"][0]["content"]["parts"]
                        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                        return _finish(prompt, text)
                    except GeminiBusy as exc:
                        tried.append("key %d %s: %s" % (n, model, exc))
                        continue
                    except Exception:
                        tried.append("key %d %s: empty answer" % (n, model))
                        continue
                if r.status_code in (401, 403) or (r.status_code == 400 and "API" in r.text[:400]):
                    refused += 1
                    tried.append("key %d refused (%d)" % (n, r.status_code))
                    break                  # next key
                tried.append("key %d %s: %d" % (n, model, r.status_code))
                if r.status_code == 429:
                    continue               # this model's quota on this key; next model / key
        if refused >= len(keys):
            raise ValueError("Gemini refused your API key(s). Check them under API Keys.")
    raise GeminiBusy("; ".join(tried[-6:]))


def _chat_profiles(first, busy=()):
    """Signed-in profiles to ask, the chosen one first. A profile a running
    video is using is never touched: opening it closes its Chrome, which
    would stop that video's images."""
    busy = {b for b in (busy or ()) if b}
    names = [first or "acc1"]
    try:
        import studio_api
        for p in studio_api.get_browser_profiles():
            if p.get("login") == "yes" and p["name"] not in names:
                names.append(p["name"])
            if p.get("login") == "in use":
                busy.add(p["name"])
    except Exception:
        pass
    return [n for n in names if n not in busy]


def _draft_in_browser(prompt, profile, busy=()):
    """The same category, written by Gemini in the user's own signed-in
    Chrome profile - for when the API is busy (503) or no key is set. When
    one account's Gemini chat answers with an error, the next signed-in
    profile is asked."""
    brief = _brief(prompt)
    import profile_chat
    profiles = _chat_profiles(profile, busy)
    if not profiles:
        raise ValueError("Every signed-in Chrome profile is busy making a video right now. "
                         "Make the category when it is done.")
    said = []
    for name in profiles[:3]:
        try:
            answer = profile_chat.ask_gemini(brief, profile_name=name, timeout_sec=180)
        except Exception as exc:
            said.append("%s: %s" % (name, str(exc)[:80]))
            continue
        finally:
            try:
                profile_chat.close_session()
            except Exception:
                pass
        if "{" not in (answer or ""):
            # Gemini's own "I encountered an error" - try the next account.
            said.append("%s: %s" % (name, (answer or "no answer")[:80]))
            continue
        try:
            return _finish(prompt, answer)
        except GeminiBusy as exc:
            said.append("%s: %s" % (name, exc))
            continue
    raise GeminiBusy("; ".join(said))


# Words that say how a picture is drawn. When no Gemini will write the
# category, the style is built from these words of the buyer's own sentence;
# the rest of the sentence is the topic, which only the samples use.
_DRAW_WORDS = [
    "2d", "3d", "flat", "vector", "cartoon", "anime", "manga", "comic", "watercolor",
    "watercolour", "oil", "painting", "painted", "sketch", "pencil", "ink", "charcoal",
    "collage", "vox", "cut-out", "cutout", "paper", "realistic", "photorealistic",
    "cinematic", "photo", "documentary", "explainer", "infographic", "minimal",
    "minimalist", "pastel", "neon", "retro", "vintage", "dark", "bright", "noir", "pixel",
    "clay", "claymation", "isometric", "low-poly", "lowpoly", "miniature", "mughal",
    "ukiyo-e", "pop-art", "doodle", "chalk", "blueprint", "storybook", "pixar", "cgi",
    "stickman", "stick", "whiteboard", "line-art", "lineart", "hand-drawn", "gritty",
    "moody", "colorful", "colourful", "monochrome", "sepia", "grainy", "glossy", "matte",
]
_FILLER = {"in", "a", "an", "the", "of", "and", "with", "style", "category", "look",
           "like", "type", "wala", "wali", "jaisa", "jaisi", "mujhe", "chahiye", "ki",
           "ka", "ke", "me", "mein", "hai", "for", "videos", "video", "films", "film"}


def _draft_plain(prompt):
    """A category made without any AI writer: the drawing words of the
    buyer's sentence become the style, the rest the samples' topic. Always
    works; the samples drawn in Canvas then show what it looks like."""
    import re
    words = re.findall(r"[a-z0-9][a-z0-9'-]*", (prompt or "").lower())
    draw = [w for w in words if w in _DRAW_WORDS]
    topic = " ".join(w for w in words if w not in _DRAW_WORDS and w not in _FILLER) or "everyday life"
    base = " ".join(dict.fromkeys(draw)) or "clean 2d"
    board = any(w in draw for w in ("vox", "collage", "cutout", "cut-out", "paper"))
    light = ("dramatic low-key lighting, deep shadows"
             if any(w in draw for w in ("dark", "noir", "moody", "gritty")) else "soft even lighting")
    medium = "" if any(w in draw for w in ("painting", "painted", "photo", "realistic",
                                           "photorealistic", "cinematic", "collage")) else " illustration"
    style = ("%s%s, one consistent colour palette, clear simple shapes, clean uncluttered "
             "composition, %s, every frame in the same look" % (base, medium, light))
    draft = {
        "label": (" ".join(w.upper() if w in ("2d", "3d") else w.capitalize()
                           for w in dict.fromkeys(draw)) or "Clean 2D") + " Look",
        "style": style,
        "character": "",
        "subjects": [w for w in topic.split() if len(w) > 3],
        "negative": "no text, no letters, no watermark, no blurry faces",
        "motion": "board" if board else "kenburns",
        "samples": ["wide establishing scene about %s" % topic,
                    "a person in a moment about %s" % topic,
                    "close-up of an important detail about %s" % topic],
    }
    return draft


def draft(prompt, profile="acc1", reference_images=None):
    """Write the category; keep it as a draft until saved.

    With a Gemini key the API writes it (every saved key is tried in turn).
    When no key works or none is set, it is built from the user's own words."""
    reference_images = list(reference_images or [])[:4]
    if not (prompt or "").strip() and reference_images:
        prompt = "Create a reusable visual category from the uploaded reference images."
    if not (prompt or "").strip():
        raise ValueError("Describe the look first.")
    keys = _keys()
    key = keys[0] if keys else None
    look, api_error = None, ""
    if reference_images and not key:
        raise ValueError("A Gemini API key is required to analyse reference images. Add it under API Keys.")
    if key:
        try:
            look = _draft_from_this_pc(prompt, key, reference_images)
        except ValueError:
            raise
        except Exception as exc:
            api_error = str(exc)
    if look is None and reference_images:
        raise ValueError("Gemini could not analyse the reference images: %s" %
                         (api_error or "no usable response"))
    if look is None:
        # No Gemini would write it (busy, out of quota, or no key): build it
        # from the user's own words. The samples drawn in Canvas then show
        # what it looks like.
        look = _finish(prompt, json.dumps(_draft_plain(prompt)))
        look["made_without_ai"] = True
    look["slug"] = _safe_slug(look.get("slug"))
    if reference_images:
        look["reference_count"] = len(reference_images)
    # Never overwrite a saved category, or another draft still being looked
    # at, by drafting one with the same name.
    base, n = look["slug"], 2
    while (os.path.isfile(os.path.join(looks_dir(), look["slug"] + ".json"))
           or os.path.isfile(os.path.join(drafts_dir(), look["slug"] + ".json"))):
        look["slug"] = "%s_%d" % (base, n)
        n += 1
    _write(os.path.join(drafts_dir(), look["slug"] + ".json"), look)
    return look


def start_samples(slug, job, python, pipeline_script, profile="acc1", aspect="16:9"):
    """Draw the draft's three samples through the real images stage."""
    slug = _safe_slug(slug)
    look = _read(os.path.join(drafts_dir(), slug + ".json"))
    if not look:
        raise ValueError("that draft is gone - describe the look again")
    pid = preview_pid(slug)
    proj = os.path.join(projects_dir(), pid)
    shutil.rmtree(proj, ignore_errors=True)
    os.makedirs(proj, exist_ok=True)
    # Samples show the style only; a character here would be kept by the
    # category and drawn into every later film.
    project_look = {"style": look["style"], "character": "",
                    "character_ref": "", "style_ref": "", "negative": look["negative"],
                    "aspect": aspect}
    if look.get("motion") == "board":
        project_look["motion"] = "board"
    _write(os.path.join(proj, "look.json"), project_look)
    # The pipeline's own prompt shape: the shot, then the look.
    with open(os.path.join(proj, "prompts.txt"), "w", encoding="utf-8") as f:
        for shot in look.get("samples") or []:
            f.write("%s. %s. %s\n" % (shot, look["style"], look["negative"]))
    argv = [python, "-u", pipeline_script, "images", pid,
            "--profile", profile, "--aspect", aspect]
    return job.start("category samples", argv)


def _board_previews(proj, pics):
    """Each sample as it will sit on the Vox board, card at rest."""
    from PIL import Image
    import board
    out_dir = os.path.join(proj, "previews")
    os.makedirs(out_dir, exist_ok=True)
    W, H = 1280, 720
    b = board.style(W)
    bg_path = board.board_for(proj, W, H, b)
    names = []
    for i, pic in enumerate(pics):
        dst = os.path.join(out_dir, "board_%d.png" % (i + 1))
        if not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(pic):
            card, (cw, ch) = board.card_for(proj, pic, W, H, b, i)
            with Image.open(bg_path) as bg, Image.open(card) as c:
                canvas = bg.convert("RGBA").resize((W, H))
                canvas.alpha_composite(c.convert("RGBA"),
                                       (int((W - cw) / 2), int((H - ch) / 2)))
                canvas.convert("RGB").save(dst)
        names.append(os.path.basename(dst))
    return names


def status(slug, job):
    """Where the samples are: the job's last lines and the pictures so far."""
    slug = _safe_slug(slug)
    look = _read(os.path.join(drafts_dir(), slug + ".json")) or \
        _read(os.path.join(looks_dir(), slug + ".json"))
    pid = preview_pid(slug)
    proj = os.path.join(projects_dir(), pid)
    raw = os.path.join(proj, "images_raw")
    pics = sorted(f for f in os.listdir(raw)
                  if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))) \
        if os.path.isdir(raw) else []
    running = job.running and job.stage == "category samples"
    out = {"ok": True, "slug": slug, "look": look, "running": running,
           "images": ["/image_raw/%s/%s" % (pid, f) for f in pics],
           "board": [], "lines": list(job.lines)[-6:] if running or job.stage == "category samples" else [],
           "exit": job.code if job.stage == "category samples" else None}
    if pics and look and look.get("motion") == "board" and not running:
        try:
            names = _board_previews(proj, [os.path.join(raw, f) for f in pics])
            out["board"] = ["/category_preview/%s/%s" % (pid, n) for n in names]
        except Exception as exc:
            out["board_error"] = str(exc)[:160]
    return out


def save(slug, label=None):
    """Make the draft a category: it appears in the style menu from now on."""
    slug = _safe_slug(slug)
    src = os.path.join(drafts_dir(), slug + ".json")
    look = _read(src)
    if not look:
        raise ValueError("that draft is gone - describe the look again")
    if label and label.strip():
        look["label"] = " ".join(label.split())[:60]
    look["character"] = ""      # a category is a style; the cast comes from each film
    # Keep the approved samples beside the category so the menu can show them.
    raw = os.path.join(projects_dir(), preview_pid(slug), "images_raw")
    keep = os.path.join(looks_dir(), slug + "_samples")
    if os.path.isdir(raw):
        shutil.rmtree(keep, ignore_errors=True)
        shutil.copytree(raw, keep)
        look["sample_images"] = sorted(os.listdir(keep))
    _write(os.path.join(looks_dir(), slug + ".json"), look)
    os.remove(src)
    return look


def trash_dir():
    return os.path.join(looks_dir(), "_trash")


def delete(slug, in_use=()):
    """Take a category the buyer made out of the style menu.

    Only the buyer's own ("custom_...") categories, never a built-in one. The
    files go to looks/_trash rather than being erased, so a mistake can be
    undone by hand, and an update never copies a deleted look back (the
    dashboard skips anything in the trash). Films already made keep their
    pictures; a video waiting in the queue on this category blocks it."""
    slug = _safe_slug(slug)
    if not slug.startswith("custom_"):
        raise ValueError("only categories you made can be deleted")
    src = os.path.join(looks_dir(), slug + ".json")
    if not os.path.isfile(src):
        raise ValueError("that category is already gone")
    if slug in set(in_use or ()):
        raise ValueError("a video in the queue uses this category - "
                         "let it finish or remove it from the queue first")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(trash_dir(), exist_ok=True)
    shutil.move(src, os.path.join(trash_dir(), "%s_%s.json" % (slug, stamp)))
    samples = os.path.join(looks_dir(), slug + "_samples")
    if os.path.isdir(samples):
        shutil.move(samples, os.path.join(trash_dir(), "%s_%s_samples" % (slug, stamp)))
    return True


def listing():
    """Every category made here, newest first."""
    out = []
    d = looks_dir()
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if not name.endswith(".json"):
            continue
        data = _read(os.path.join(d, name), {}) or {}
        if data.get("custom"):
            out.append({"id": name[:-5], "label": data.get("label", name[:-5]),
                        "motion": data.get("motion", "kenburns"),
                        "mtime": os.path.getmtime(os.path.join(d, name))})
    return sorted(out, key=lambda x: -x["mtime"])

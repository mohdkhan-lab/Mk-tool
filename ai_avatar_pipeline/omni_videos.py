"""
OMNI VIDEOS — scene clips through OmniFlows' Bulk Image to Video tool.

    python omni_videos.py output/07_story/scenes.json --aspect portrait
    python omni_videos.py output/07_story/scenes.json --limit 2 --dry-run

The tool pairs uploaded images with prompts BY ORDER, so the pairing is the
thing that has to be right. Two guards, both before anything is spent:

  * only scenes whose image is actually on disk are included, so a missing
    file cannot shift every later scene onto the wrong prompt;
  * the submit button counts the pairs itself ("Generate N Videos"), and that
    number is compared against the batch before the click.

Flow's lesson carries over: a prompt that asks for "gestures, expressions" in
a picture with no people makes the model invent people. Prompts here animate
only what is already in the frame unless the scene says otherwise.
"""

import argparse
import os
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from automate_scenes import load_scenes, log, save_scenes
from omni import (OMNI_URL, attach, ensure_login, history_items, keep_session,
                  match_prompt, open_tool, safe)

# The panel accepts 50 pairs, but it renders them ONE AT A TIME — a 50-pair
# batch ran past an hour and had produced 17 clips when the wait gave up. The
# work is not lost when that happens (it keeps rendering on their side and
# lands in history), but a run that cannot see its own results cannot file
# them against scenes. Smaller batches finish inside a wait that is sane, and
# each one is saved before the next is submitted.
BATCH_MAX = 16


def one_line(text):
    return " ".join(str(text).split())


ASPECT_FROM_RATIO = {"16:9": "landscape", "9:16": "portrait", "1:1": "square"}


def project_aspect(data, given, fallback="landscape"):
    """
    The shape this project is in.

    An --aspect that disagrees with the project is not a harmless default: a
    collect run that fell back to portrait cropped six finished landscape
    images down to a 432px slice of themselves. scenes.json already records
    aspect_ratio, so that is the answer unless it is deliberately overridden.
    """
    if given:
        return given
    return ASPECT_FROM_RATIO.get(str(data.get("aspect_ratio", "")).strip(), fallback)


def fetch_bytes(page, url, tries=4):
    """
    Pull one finished result down.

    The generation is the expensive half and it is already paid for by the
    time this runs, so a slow download must not be allowed to throw it away.
    Playwright's default 30s was not enough for a full-size result on a busy
    account: one timeout killed a batch of six that had all rendered fine.

    Retries with a rising deadline, and returns None rather than raising, so a
    single unreachable file costs its own scene and nothing else. Whatever is
    missed is still in the site's history and comes back with --collect.
    """
    for attempt in range(1, tries + 1):
        try:
            return page.request.get(url, timeout=60_000 * attempt).body()
        except Exception as exc:
            log(f"  download attempt {attempt}/{tries} failed ({safe(exc)[:60]})")
            page.wait_for_timeout(3000 * attempt)
    return None


def clip_seconds(path):
    """How long the clip really is, or None when ffprobe is not around."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
        return float(out.stdout.strip())
    except Exception:
        return None


# What a clip is asked to do: movement only, built from the scene's own fields.
# The rules keep the safety filter from refusing it and keep the picture (its
# characters, style and framing) exactly as drawn.
_MOTION_RULES = ("Keep the characters, their faces, clothing, the art style, colours and "
                 "framing exactly as in the image. No new objects, no text, no weather "
                 "effects, no scene change, no camera cut. Smooth, natural, gentle motion.")


def prime_motion(scenes, speech=True):
    """Kept for callers: motion prompts are built locally, nothing to fetch."""
    return None


def motion_prompt(scene, speech=True):
    """What this clip should do - unique to this scene, about movement only."""
    what = one_line(scene.get("video_instruction") or "")
    if not what:
        about = one_line(scene.get("scene") or scene.get("image_prompt") or "")[:220]
        what = ("Bring this moment to life with subtle natural movement: %s. Characters "
                "breathe and make small natural gestures; slow gentle camera push-in."
                % (about or "the scene in the image"))
    line = one_line(scene.get("dialogue") or "")
    talk = (" The speaking character's mouth moves naturally as if saying: \"%s\"." % line[:200]
            if speech and line else "")
    return "%s%s %s" % (what.rstrip(". ") + ".", talk, _MOTION_RULES)









def pending(data, redo=False):
    """Scenes with an image on disk and no clip yet, in scene order."""
    proj = data.get("_proj", "")
    out = []
    for s in data.get("scenes", []):
        img = s.get("image_path")
        if not img:
            continue
        if not os.path.isfile(os.path.join(proj, img)):
            continue
        if s.get("video_path") and not redo:
            continue
        out.append(s)
    return out


def panel_failures(page):
    """
    Refusals, read off the panel while it still exists.

    Veo declines to animate a photorealistic human face — "Content Blocked —
    Realistic Person Safety Filter" — and a refused card never becomes a
    video and never reaches the history. The panel is the only place that ever
    says so, which is why it is still worth reading even though it is not
    where the results are collected from.
    """
    try:
        return page.evaluate(r"""() => {
            const panel = document.querySelector('#tool-bulk-i2v');
            if (!panel) return [];
            // Whole cards, not leaves. The failure notice and the [Snn] tag
            // live in different elements of the same card — "Scene 7 failed"
            // on one line, "Prompt 7: [S10] Animate only..." on another — so
            // reading leaves gives a failure with no scene attached to it and
            // the wait sits on that clip until it times out. The card's own
            // number is its position in the batch, not the scene, so the tag
            // is the only thing that says which row was refused.
            const out = [];
            panel.querySelectorAll('*').forEach(el => {
                const t = (el.innerText || '').trim();
                if (t.length > 600) return;
                if (!/failed|content blocked|safety filter|refused/i.test(t)) return;
                if (!/\[S\d+\]/.test(t)) return;      // no tag: not a whole card
                out.push(t.replace(/\s+/g, ' '));
            });
            // Keep the shortest text carrying each tag — the tightest element
            // that still holds both halves, rather than every ancestor of it.
            const best = new Map();
            for (const t of out) {
                const m = t.match(/\[S(\d+)\]/);
                if (!m) continue;
                const k = m[1];
                if (!best.has(k) || t.length < best.get(k).length) best.set(k, t);
            }
            return [...best.values()];
        }""")
    except Exception:
        return []


def harvest(ctx, page, batch, out_dir, timeout=7200, before=None):
    """
    Collect a submitted batch, by tag, out of the account's history.

    The panel used to be the source: it lists results as they land, in an
    order of its own. Two things wrecked that. Its order is not the submission
    order, so clips filed one scene off and a whole batch was shifted. And it
    is destroyed by anything that reloads the page — a dropped session sent
    the app back to its home screen mid-batch and the wait then watched an
    empty panel for an hour while the clips sat finished on the server.

    History has neither problem. It survives the reload, and each card names
    the prompt that made it, so the [Snn] tag says which scene a clip belongs
    to instead of leaving it to be guessed from position.

    Returns {scene_number: path} for whatever arrived.
    """
    want = {s["scene_number"]: s for s in batch}
    got, refused = {}, set()
    deadline = time.time() + timeout
    while time.time() < deadline and len(got) + len(refused) < len(want):
        page.wait_for_timeout(45_000)

        for line in panel_failures(page):
            m = re.search(r"\[S(\d+)\]", line)
            num = int(m.group(1)) if m else None
            if num in want and num not in refused:
                refused.add(num)
                log(f"  scene {num:02d} REFUSED: {safe(line)[:100]}")
            elif not m and line not in refused:
                log(f"  refusal on the panel: {safe(line)[:100]}")

        items = history_items(ctx)
        # Cards that were already there when this batch was submitted are not
        # this batch's results, however well they match. Re-animating the same
        # scene produces the identical prompt and the identical [Snn] tag, so
        # the run before this one sits in history as a perfect match that is
        # already finished — while the clip actually being made is not done yet
        # and gets skipped. A film redrawn with a different character then
        # rendered with the old character's clips, and nothing in the log
        # looked wrong: three clips "arrived", three unique files on disk.
        if before:
            items = [it for it in items if it.get("url") not in before]
        prime_motion(list(want.values()))
        for num, scene in want.items():
            if num in got or num in refused:
                continue
            hit = match_prompt(items, motion_prompt(scene), "video")
            if not hit:
                continue
            body = fetch_bytes(page, hit["url"])
            if body is None:
                continue
            final = os.path.join(out_dir, f"scene_{num:02d}.mp4")
            with open(final, "wb") as f:
                f.write(body)
            got[num] = final
            length = clip_seconds(final) or 0
            log(f"  scene {num:02d} arrived ({length:.1f}s) "
                f"[{len(got)}/{len(want)}]")

    missing = [n for n in want if n not in got and n not in refused]
    if missing:
        log(f"  still nothing for {missing}")
    return got, refused


def main():
    ap = argparse.ArgumentParser(description="Scene clips via OmniFlows Bulk Image to Video")
    ap.add_argument("scenes_file")
    ap.add_argument("--aspect", default="", choices=["", "portrait", "landscape"],
                    help="Default: whatever the project's aspect_ratio says")
    ap.add_argument("--model", help="Substring of the model radio's label (default: leave as-is)")
    ap.add_argument("--scenes", default="", metavar="2,4,12",
                    help="Only these scene numbers. For sampling a look across "
                         "the film before committing the whole batch — the "
                         "first N scenes are all one sequence and prove little.")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Upload and fill, report the pair count, do not submit")
    ap.add_argument("--collect", action="store_true",
                    help="Do not generate: pull already-finished clips out of "
                         "the site's history and file them against the scenes")
    args = ap.parse_args()

    proj = os.path.dirname(os.path.abspath(args.scenes_file))
    data = load_scenes(args.scenes_file)
    args.aspect = project_aspect(data, args.aspect)
    data["_proj"] = proj
    todo = pending(data, args.redo)
    data.pop("_proj", None)
    if args.scenes:
        want = {int(x) for x in args.scenes.replace(" ", "").split(",") if x}
        todo = [s for s in todo if s["scene_number"] in want]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        log("nothing to animate — no scene has an image without a clip")
        return
    clip_dir = os.path.join(proj, "clips")
    os.makedirs(clip_dir, exist_ok=True)
    log(f"{len(todo)} scenes to animate")

    with sync_playwright() as p:
        # Share the served browser when there is one; a run of its own would
        # kill it and sign the account out.
        ctx, attached = attach(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(OMNI_URL, wait_until="domcontentloaded", timeout=120_000)
        if not ensure_login(page):
            sys.exit(1)          # window stays open for a hand sign-in
        if args.collect:
            items = history_items(ctx)
            log(f"history has {len(items)} items")
            got = 0
            prime_motion(todo)
            for scene in todo:
                hit = match_prompt(items, motion_prompt(scene), "video")
                if not hit:
                    log(f"  scene {scene['scene_number']:02d}: nothing finished for it yet")
                    continue
                body = fetch_bytes(page, hit["url"])
                if body is None:
                    continue
                final = os.path.join(clip_dir, f"scene_{scene['scene_number']:02d}.mp4")
                with open(final, "wb") as f:
                    f.write(body)
                scene["video_path"] = os.path.relpath(final, proj)
                got += 1
                log(f"  scene {scene['scene_number']:02d} -> {scene['video_path']}")
            save_scenes(args.scenes_file, data)
            log(f"collected {got}/{len(todo)}")
            if not attached:
                ctx.close()
            return

        open_tool(page, "Bulk Image to Video")

        if args.model:
            radios = page.locator("input[name='biv-model']")
            for i in range(radios.count()):
                label = radios.nth(i).evaluate(
                    "r => (r.closest('label') || r.parentElement).innerText")
                if args.model.lower() in label.lower():
                    radios.nth(i).check(force=True)
                    log(f"model: {safe(one_line(label))[:60]}")
                    break

        for start in range(0, len(todo), BATCH_MAX):
            batch = todo[start:start + BATCH_MAX]
            if start:
                # The panel keeps the previous batch's uploads. Left alone, the
                # second batch of 18 images still reported "Generate 50 Videos"
                # — the old files plus the new ones, pairing every clip against
                # the wrong prompt. Reloading is the only reliable reset.
                page.goto(OMNI_URL, wait_until="domcontentloaded", timeout=120_000)
                ensure_login(page)
                open_tool(page, "Bulk Image to Video")
            files = [os.path.join(proj, s["image_path"]) for s in batch]
            page.select_option("#bi2vAspect", args.aspect)
            page.set_input_files("#biv-files-input", files)
            page.wait_for_timeout(3000 + 200 * len(files))

            prime_motion(batch)
            blocks = [f"Prompt {i + 1}:\n{motion_prompt(s)}"
                      for i, s in enumerate(batch)]  # the [Snn] tag is inside
            page.fill("#biv-prompts-ta", "\n\n".join(blocks))
            page.wait_for_timeout(2000)

            # The button labels itself with the number of pairs it sees. That
            # is the site's own count, so it catches an upload that silently
            # dropped a file or a prompt block it parsed differently.
            label = page.inner_text("#bi2vSubmitBtn")
            m = re.search(r"(\d+)", label)
            counted = int(m.group(1)) if m else -1
            log(f"batch {start // BATCH_MAX + 1}: {len(batch)} pairs, "
                f"button says {safe(one_line(label))}")
            if counted != len(batch):
                log("pair count mismatch — not submitting; window left open")
                sys.exit(1)
            if args.dry_run:
                log("dry run — nothing submitted")
                continue

            # Whatever history already holds, remembered before the submit so
            # the collector can tell this batch's clips from an earlier run's.
            before = {it.get("url") for it in history_items(ctx) if it.get("url")}
            page.click("#bi2vSubmitBtn")
            log(f"  submitted, waiting for clips ({len(before)} already in history)")
            got, refused = harvest(ctx, page, batch, clip_dir, before=before)
            for scene in batch:
                num = scene["scene_number"]
                if num not in got:
                    continue
                final = got[num]
                scene["video_path"] = os.path.relpath(final, proj)
                # The tool returns 8s clips, not the 10s these scenes were
                # written for. Recording what actually came back keeps the
                # stitcher from stretching a 10s narration line over an 8s
                # picture, which reads as slow motion.
                length = clip_seconds(final)
                if length:
                    scene["duration_sec"] = round(length)
            save_scenes(args.scenes_file, data)
        if not attached:
            ctx.close()


if __name__ == "__main__":
    main()

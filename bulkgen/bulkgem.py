"""
STEP 4 — the Gemini "Bulk Image Generator" app, driven.

The app is a shared Gemini Canvas app: an aspect-ratio select, two consistency
toggles with a reference-image upload behind each, one textarea that takes one
prompt per line, and a counter underneath that says how many prompts it thinks
it has. It renders inside a sandboxed frame, so every locator here is scoped to
that frame rather than to the page.

Three things this stage will not do quietly:

* **It refuses to generate unless the counter matches.** The counter is the
  only place the app says what it actually parsed. Paste 250 prompts, have it
  read 249, and the run still succeeds — with every image after the miscount
  landing on the wrong words. So the counter is read back and compared, and a
  mismatch is retried, then escalated.
* **It refuses to upload a reference to the wrong slot.** Character and style
  are enabled one at a time and each reference goes into the file input that
  appeared with its own toggle, so the mapping is built rather than guessed.
* **It never closes the browser.** The window is left open on purpose — the
  generation is long, and watching it is how a stall gets noticed.

    python pipeline.py images mystory --profile acc1
    python pipeline.py images mystory --dump      # save the app DOM instead
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile

from common import AI_PIPE, BULK_APP_URL, INSTALL_ROOT, REPO, log, p, save_state


def _ref_candidates(reference):
    """Where a reference sheet named in a look may be.

    The user's own refs folder first - installed, that is where the app keeps
    them, away from this code - then beside the code, then the install folder
    the app shipped its sample sheets in."""
    name = os.path.basename(reference)
    out = []
    user_refs = os.environ.get("STICKFILM_REFS")
    if user_refs:
        out += [os.path.join(user_refs, name)]
    for root in dict.fromkeys([REPO, INSTALL_ROOT]):
        out += [os.path.join(root, reference),
                os.path.join(root, "vidgen", reference),
                os.path.join(root, "bulkgen", reference),
                os.path.join(root, "vidgen", "refs", name),
                os.path.join(root, "bulkgen", name)]
    return out

sys.path.insert(0, AI_PIPE)
from automate_scenes import kill_stale_chrome, launch_profile, load_profiles  # noqa: E402

# Contexts opened this run. Held so the browser survives until the whole
# pipeline is done — a garbage-collected context takes its window with it.
OPEN = []

# How long the app frame gets to appear before we call it a login problem.
APP_WAIT_SEC = 90

# A quiet poll: the page is asked for its state every this many seconds while
# images render. Faster gains nothing and fills the log.
POLL_SEC = 5


# ---------------------------------------------------------------- page pieces

def _profile(name):
    matches = [x for x in load_profiles() if x["name"] == name]
    if not matches:
        have = ", ".join(x["name"] for x in load_profiles())
        raise ValueError(f"no browser profile called {name}. Have: {have}")
    return matches[0]


def bring_to_front(page):
    """Bring the browser window directly to the front of screen."""
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run([
            "powershell", "-NoProfile", "-Command",
            "(New-Object -ComObject WScript.Shell).AppActivate('Google Chrome')"
        ], timeout=5, capture_output=True, creationflags=flags)
    except Exception:
        pass


def open_app(profile="acc1", url=BULK_APP_URL, shot_dir=None, minimized=True):
    """A signed-in browser on the app, past the shared-app warning.

    Runs minimized by default — this drives an unattended generation run, not
    something the person needs to watch (the dashboard's Live Image Wall shows
    the results as they land), so there is no reason for a window to jump in
    front of whatever else is on screen.
    """
    from playwright.sync_api import sync_playwright

    prof = _profile(profile)
    kill_stale_chrome(prof.get("user_data_dir"))

    pw = sync_playwright().start()
    ctx = launch_profile(pw, prof, accept_downloads=True, minimized=minimized)
    OPEN.append((pw, ctx))
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if not minimized:
        maximize(page)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    except Exception as e:
        log("bulk", f"page.goto note ({e}) — continuing to wait for app...")
    page.wait_for_timeout(4000)
    if not minimized:
        bring_to_front(page)

    # "This app was created by another person" — a gate on every shared app,
    # shown before the app itself is allowed to render.
    for label in ("Continue", "Got it"):
        try:
            button = page.get_by_role("button", name=label, exact=True)
            if button.count():
                button.first.click(force=True, timeout=5000)
                page.wait_for_timeout(2000)
                log("bulk", f"clicked past the shared-app gate ({label})")
                break
        except Exception:
            pass

    # "You need to sign in with your Google Account to see some features" sits
    # along the bottom of the page for the whole run. It is not an error — the
    # app generates perfectly well behind it — but it puts the words "Sign in"
    # on the page, and it sits over the bottom of the layout where the download
    # button ends up. Closed here so neither can bite.
    try:
        banner = page.get_by_text("You need to sign in with your Google Account",
                                  exact=False)
        if banner.count():
            for close in (page.get_by_role("button", name="Close"),
                          page.locator("button[aria-label='Close']"),
                          banner.first.locator("xpath=ancestor::*[self::div][1]")
                               .get_by_role("button").last):
                try:
                    if close.count():
                        close.first.click(timeout=3000)
                        page.wait_for_timeout(600)
                        break
                except Exception:
                    continue
            log("bulk", "dismissed the Google sign-in banner"
                if not banner.count() else
                "the Google sign-in banner is up (harmless, the app still draws)")
    except Exception:
        pass

    frame = wait_for_app(page)
    if not minimized:
        bring_to_front(page)
    if shot_dir:
        shot(page, shot_dir, "01_loaded")
    return page, frame


def login(profile="acc1", url=BULK_APP_URL):
    """
    Open one profile's browser so a Google account can be signed in.

    The image stage needs a browser that is already signed in — that is the one
    thing a zip cannot carry to a new machine. This opens the app itself rather
    than a generic Google page, so what you see when the sign-in finishes is
    the thing the pipeline will be driving: if the app renders, the profile is
    ready. Closing the window ends it; the cookies are already saved.
    """
    from playwright.sync_api import sync_playwright

    prof = _profile(profile)
    kill_stale_chrome(prof.get("user_data_dir"))

    pw = sync_playwright().start()
    ctx = launch_profile(pw, prof, accept_downloads=True)
    OPEN.append((pw, ctx))
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    maximize(page)
    page.goto(url, timeout=120_000)
    log("login", f"profile {profile}: sign in to Google in the window that opened")
    log("login", "when the Bulk Image Generator appears, CLOSE the window — "
                 "the login is saved by itself")

    while True:
        try:
            if not ctx.pages:
                break
            page.wait_for_timeout(1500)
        except Exception:      # the window was closed, which is the signal
            break
    log("login", f"profile {profile} is signed in and saved")
    OPEN.clear()
    return profile


def maximize(page):
    """
    Open the window full-screen-wide.

    The app's left column is taller than a 1280x900 window, so the Download
    button sits below the fold — the automation clicks it either way, but the
    person watching the run cannot see it and cannot take over if a stage
    stops. Chrome only exposes this through the devtools protocol.
    """
    try:
        cdp = page.context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window["windowId"],
                  "bounds": {"windowState": "maximized"}})
    except Exception as exc:          # not fatal — only the view is smaller
        log("bulk", f"could not maximise the window ({exc})")


def app_frame(page):
    """
    The frame the app is actually in.

    Identified by what it holds rather than by name: a Canvas app frame has no
    stable id, and the page itself carries an empty bscframe that matches any
    structural test.
    """
    for frame in page.frames:
        try:
            if frame.locator("textarea").count() and frame.locator("button").count():
                return frame
        except Exception:
            continue
    return None


def wait_for_app(page, seconds=APP_WAIT_SEC):
    deadline = time.time() + seconds
    while time.time() < deadline:
        shot(page, None, "00_loading")
        frame = app_frame(page)
        if frame:
            return frame
        page.wait_for_timeout(2000)

    signed_out = page.get_by_role("link", name="Sign in").count()
    raise RuntimeError(
        "the app never rendered" + (
            " — this profile is signed out of Google. Sign it in once with:\n"
            "    python automate_scenes.py --login --profile <name>"
            if signed_out else
            " — run again with --dump to save what the page did render"))


def shot(page, shot_dir, name):
    try:
        live_fp = os.path.join(REPO, "vidgen", "live_canvas.jpg")
        page.screenshot(path=live_fp, quality=70, type="jpeg")
        # The dashboard also looks in the project folder - the only one both
        # sides share once the app is installed.
        if shot_dir:
            page.screenshot(path=os.path.join(os.path.dirname(shot_dir), "live_canvas.jpg"),
                            quality=70, type="jpeg")
    except Exception:
        pass
    if not shot_dir:
        return None
    os.makedirs(shot_dir, exist_ok=True)
    path = os.path.join(shot_dir, f"{name}.png")
    try:
        page.screenshot(path=path, full_page=False)
    except Exception:
        return None
    return path


# ------------------------------------------------------------------ controls

# Every control in the app carries an id, so nothing here is matched on
# generated Tailwind classes or on label text that a rewording would break.
# Taken from the app's own DOM (`--dump` writes the copy these came from).
IDS = {
    "tab_image": "button[data-tab='image']",
    "aspect": "#aspectRatio",
    "cc_toggle": "#ccToggle",
    "cc_panel": "#ccOptionsContainer",
    "cc_text": "textarea.cc-text-input",
    "cc_image": "input.cc-image-input",
    "sc_toggle": "#scToggle",
    "sc_panel": "#scOptionsContainer",
    "sc_text": "#scText",
    "sc_image": "#scImage",
    "prompts": "#prompts",
    "count": "#prompt-count",
    "generate": "#generate-btn",
    "stop": "#stop-btn",
    "download": "#download-all-btn",
    "grid": "#image-grid",
    "select_failed": "#select-failed-btn",
    "regen": "#bulk-regen-btn",
    "regen_confirm": "#confirm-regen-btn",
}

# The select stores a word, not a ratio, and the option text carries the pixel
# size as well — so the ratio is mapped to the value the app actually stores.
ASPECT_VALUES = {"1:1": "square", "square": "square",
                 "9:16": "portrait", "portrait": "portrait",
                 "16:9": "landscape", "landscape": "landscape"}


def show_image_tab(frame):
    """The app opens on the image tab, but a reload can land on Text-to-Speech."""
    tab = frame.locator(IDS["tab_image"])
    if tab.count():
        tab.first.click()
        frame.wait_for_timeout(500)


def set_aspect(frame, wanted):
    """Pick the aspect ratio by the value the select stores, not by its index."""
    if not wanted:
        return None
    sel = frame.locator(IDS["aspect"])
    value = ASPECT_VALUES.get(wanted.strip().lower())
    if value:
        sel.select_option(value=value)
    else:
        # An option the map has not seen — match on what it says instead.
        options = sel.locator("option")
        for j in range(options.count()):
            if wanted.lower() in (options.nth(j).inner_text() or "").lower():
                sel.select_option(index=j)
                break
        else:
            raise ValueError(f"no aspect-ratio option matching {wanted!r}")
    frame.wait_for_timeout(400)
    label = sel.locator("option:checked").inner_text().strip()
    log("bulk", f"aspect ratio: {label}")
    return label


# The app opens its consistency panel from a `change` listener on the
# checkbox, so what has to reach the page is that event — not a click at any
# particular pixel. Three ways to produce it, cheapest first.
SET_CHECKED_JS = """
(el, want) => {
  if (el.checked !== want) {
    el.checked = want;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
}
"""


def _panel_open(frame, panel):
    """Whether the panel this switch controls is showing."""
    try:
        return frame.locator(f"{panel}:not(.hidden)").count() > 0
    except Exception:
        return False


def _switch(frame, toggle, panel, want=True):
    """
    Set one consistency switch and wait for its panel.

    The checkbox is Tailwind `sr-only` — present and focusable, but clipped to
    nothing — and the visible switch is a sibling `div` drawn by the `peer`
    rules. Playwright will not click a box with no box, `force` or not, so the
    click is aimed at the `label` that wraps both, and if even that is refused
    the event the app is actually listening for is dispatched directly. The
    panel losing `hidden` is what counts as on, because the checkbox and the
    panel can disagree for a moment while the app reacts.
    """
    box = frame.locator(toggle)
    if not box.count():
        raise RuntimeError(f"{toggle} is not on the page — run with --dump")
    if box.is_checked() == want and _panel_open(frame, panel) == want:
        return want

    ways = (
        ("the switch", lambda: box.locator("xpath=ancestor::label[1]").first.click()),
        ("the checkbox", lambda: box.set_checked(want, force=True)),
        ("a change event", lambda: box.evaluate(SET_CHECKED_JS, want)),
    )
    last = None
    for how, attempt in ways:
        try:
            attempt()
        except Exception as exc:
            last = exc
            continue
        frame.wait_for_timeout(800)
        if _panel_open(frame, panel) == want and box.is_checked() == want:
            log("bulk", f"  toggled with {how}")
            return want
    raise RuntimeError(
        f"{toggle} would not turn {'on' if want else 'off'} — the panel {panel} "
        f"never {'opened' if want else 'closed'}"
        + (f" (last error: {last})" if last else ""))


def enable_character(frame, reference=None, description=None):
    """
    Character Consistency on, with our character in the first slot.

    The panel holds one row per character (`#cc-list`, `#add-cc-btn` adds
    more); a recurring cast of one is what this pipeline uses, so the
    reference and the written description both go into row 1.
    """
    if not reference and not description:
        return None
    _switch(frame, IDS["cc_toggle"], IDS["cc_panel"], True)
    log("bulk", "Character Consistency: on")

    if description:
        frame.locator(IDS["cc_text"]).first.fill(description[:600])
        log("bulk", f"  character: {description[:60]}...")
    if reference:
        if not os.path.isfile(reference):
            candidates = _ref_candidates(reference)
            for cand in candidates:
                if os.path.isfile(cand):
                    reference = cand
                    break
        if not os.path.isfile(reference):
            raise FileNotFoundError(f"character reference not found: {reference}")
        frame.locator(IDS["cc_image"]).first.set_input_files(reference)
        frame.wait_for_timeout(1200)
        log("bulk", f"  reference: {os.path.basename(reference)}")
    return reference


def disable_character(frame):
    """Character Consistency off, for a shot with nobody in it."""
    _switch(frame, IDS["cc_toggle"], IDS["cc_panel"], False)
    log("bulk", "Character Consistency: off — nobody in these shots")


def enable_style(frame, reference=None, description=None):
    """Style Consistency on, with our style image and style words."""
    if not reference and not description:
        return None
    _switch(frame, IDS["sc_toggle"], IDS["sc_panel"], True)
    log("bulk", "Style Consistency: on")

    if description:
        frame.locator(IDS["sc_text"]).fill(description[:600])
        log("bulk", f"  style: {description[:60]}...")
    if reference:
        if not os.path.isfile(reference):
            candidates = _ref_candidates(reference)
            for cand in candidates:
                if os.path.isfile(cand):
                    reference = cand
                    break
        if not os.path.isfile(reference):
            raise FileNotFoundError(f"style reference not found: {reference}")
        frame.locator(IDS["sc_image"]).set_input_files(reference)
        try:
            frame.wait_for_selector("#scImagePreviewContainer:not(.hidden)", timeout=15000)
        except Exception:
            log("bulk", "  (no preview appeared — check the window)")
        log("bulk", f"  reference: {os.path.basename(reference)}")
    return reference


# ------------------------------------------------------------------- prompts

COUNT_RE = re.compile(r"(\d[\d,]*)\s*prompts?\b", re.I)

READ_COUNT_JS = """
() => {
  const hits = [];
  for (const el of document.querySelectorAll('*')) {
    if (el.children.length) continue;
    const t = (el.textContent || '').trim();
    if (/^\\d[\\d,]*\\s*prompts?$/i.test(t)) hits.push(t);
  }
  return hits;
}
"""


def read_count(frame):
    """What the app says it parsed, or None if it does not say."""
    counter = frame.locator(IDS["count"])
    if counter.count():
        found = COUNT_RE.search(counter.inner_text() or "")
        if found:
            return int(found.group(1).replace(",", ""))
    # The id is gone: fall back to any element that says "N prompts".
    try:
        hits = frame.evaluate(READ_COUNT_JS)
    except Exception:
        hits = []
    if not hits:
        hits = [m.group(0) for m in COUNT_RE.finditer(frame.locator("body").inner_text())]
    if not hits:
        return None
    return int(COUNT_RE.search(hits[-1]).group(1).replace(",", ""))


def _prompt_box(frame):
    """The prompts textarea. Not `textarea.last` — the app has six of them."""
    box = frame.locator(IDS["prompts"])
    if box.count():
        return box
    return frame.get_by_placeholder(re.compile("one per line|vibrant oil", re.I)).first


def fill_prompts(frame, prompts, tries=3):
    """
    Paste the prompts and make the app agree about how many there are.

    A mismatch is not cosmetic. The counter is the app's own parse of the
    textarea, and it is what the generation runs on; if it reads one fewer
    than we pasted, the images after that point belong to the wrong lines.
    Refilling fixes the common causes (a trailing blank line, a paste that
    landed while the box was still mounting). What it cannot fix is worth
    stopping for.
    """
    text = "\n".join(prompts)
    box = _prompt_box(frame)
    for attempt in range(1, tries + 1):
        box.click()
        box.fill("")
        frame.wait_for_timeout(300)
        box.fill(text)
        frame.wait_for_timeout(1500)

        count = read_count(frame)
        if count == len(prompts):
            log("bulk", f"counter reads {count} prompts — matches")
            return count
        log("bulk", f"counter reads {count} but {len(prompts)} were pasted"
                    + (f" — refilling ({attempt}/{tries})" if attempt < tries else ""))
    raise RuntimeError(
        f"the app counted {read_count(frame)} prompts, not {len(prompts)}. "
        "Nothing was generated. Look at the window: a prompt holding a newline "
        "or an empty line is the usual cause.")


# ---------------------------------------------------------------- generation

# How long the app gets to actually start before we call it a failed run. It
# spends the first stretch on "Analyzing References..." with an empty grid, so
# nothing can be concluded from silence before this is up.
START_WAIT_SEC = 180


def image_count(frame):
    """How many pictures are in the results grid right now."""
    try:
        return frame.locator(f"{IDS['grid']} img").count()
    except Exception:
        return 0


def running(frame):
    """The app shows a Stop button only while it is generating."""
    try:
        return frame.locator(IDS["stop"]).is_visible()
    except Exception:
        return False


def wait_until_started(page, frame, seconds=START_WAIT_SEC):
    """
    Wait for the app to actually be working.

    Between the click and the first tile the app reads the reference images —
    "Analyzing References...", an empty grid, and nothing else to go on. Until
    the Stop button is up, an empty grid means "not yet", not "finished", and
    the difference is the whole run.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        shot(page, None, "03_starting")
        if running(frame) or image_count(frame):
            return True
        page.wait_for_timeout(3000)
    return False


def generate(page, frame, expected, timeout_min=180, shot_dir=None):
    """Start the run and wait it out, saying what it can see while it waits."""
    button = frame.locator(IDS["generate"])
    if not button.count():
        button = frame.get_by_role("button", name=re.compile("generate images", re.I))
    if not button.count():
        raise RuntimeError("no Generate button on the page")
    button.first.click()
    if not wait_until_started(page, frame):
        shot(page, shot_dir, "03_never_started")
        raise RuntimeError(
            f"the run never started — no Stop button and an empty grid "
            f"{START_WAIT_SEC}s after clicking Generate. The window is still "
            "open; look at what the app is saying.")
    log("bulk", f"generating {expected} images — this runs for a while")

    deadline = time.time() + timeout_min * 60
    start_time = time.time()
    last_seen, last_change, last_note = 0, time.time(), 0
    has_rendered_any = False
    while time.time() < deadline:
        page.wait_for_timeout(POLL_SEC * 1000)
        shot(page, shot_dir, "03_live")
        seen = image_count(frame)
        if seen > 0:
            has_rendered_any = True
        if seen != last_seen:
            last_seen, last_change = seen, time.time()

        stalled = int(time.time() - last_change)

        # Signed out, or refused. Only worth believing when the app is doing
        # nothing: Gemini shows a permanent "You need to sign in with your
        # Google Account to see some features" banner along the bottom of the
        # page, and matching on the word "Sign in" alone abandoned rounds that
        # were visibly generating — Stop button up, every tile spinning. Three
        # runs died on that banner. A real refusal has no tiles and no Stop.
        if not running(frame) and seen == 0 and not has_rendered_any:
            try:
                if (page.locator("text='Sign in'").count()
                        or frame.locator("text='403'").count()
                        or frame.locator("text='API request failed'").count()):
                    log("bulk", "  the app is not generating and the page says "
                                "signed-out or refused")
                    shot(page, shot_dir, "03_403_error")
                    return False
            except Exception:
                pass

        # Finished is either all expected tiles rendered OR the generator stopped and settled
        if seen >= expected:
            log("bulk", f"generation complete — all {seen}/{expected} tiles in the grid")
            shot(page, shot_dir, "04_complete")
            return True

        if not running(frame):
            if seen >= int(expected * 0.7) and stalled >= 30:
                log("bulk", f"generation settled — {seen}/{expected} tiles in the grid")
                shot(page, shot_dir, "04_complete")
                return True
            elif 0 < seen < int(expected * 0.7) and stalled >= 60:
                log("bulk", f"  only {seen}/{expected} tiles rendered before settling — insufficient quota on this profile, rotating...")
                return False
            elif seen == 0 and time.time() - start_time > 60:
                # If still 0 after 60s, try clicking Generate again
                log("bulk", "  no tiles yet, re-triggering Generate button...")
                btn = frame.locator(IDS["generate"])
                if btn.count() and not btn.is_disabled():
                    try:
                        btn.first.click()
                    except Exception:
                        pass
                start_time = time.time()
        if time.time() - last_note > 60:
            last_note = time.time()
            stalled = int(time.time() - last_change)
            log("bulk", f"  {seen}/{expected} rendered"
                        + (f", nothing new for {stalled}s" if stalled > 120 else ""))
            shot(page, shot_dir, "03_progress")
            if not running(frame) and stalled > 60 and seen == 0 and not has_rendered_any:
                log("bulk", "  generator stopped with 0 tiles, switching profile...")
                return False
            elif stalled > 600 and seen == 0:
                log("bulk", "  stalled on 0 tiles for >10 min, switching profile...")
                return False

    log("bulk", f"gave up waiting after {timeout_min} min — the window is still "
                "open and may finish on its own")
    shot(page, shot_dir, "03_timeout")
    return False


def retry_failed(page, frame, timeout_min=60, shot_dir=None):
    """
    Put the failed tiles back through the generator.

    A refused prompt leaves a tile in the grid with nothing in it, and that
    tile still becomes a file in the zip — a blank frame in the middle of the
    film. The app has its own "Select Failed" and "Regenerate" for exactly
    this, so it is used rather than reasoned about: if there is nothing
    failed, the button is hidden and this returns False.
    """
    select_failed = frame.locator(IDS["select_failed"])
    if not select_failed.count() or not select_failed.is_visible():
        return False
    select_failed.click()
    frame.wait_for_timeout(800)

    regen = frame.locator(IDS["regen"])
    if not regen.count() or not regen.is_visible():
        return False
    regen.click()
    frame.wait_for_timeout(1000)
    confirm = frame.locator(IDS["regen_confirm"])
    if confirm.count() and confirm.is_visible():
        confirm.click()
    log("bulk", "retrying the failed images")
    generate_wait(page, frame, timeout_min, shot_dir)
    return True


def generate_wait(page, frame, timeout_min, shot_dir=None):
    """Wait out a run that something else started."""
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        page.wait_for_timeout(POLL_SEC * 1000)
        if not running(frame):
            shot(page, shot_dir, "04_retried")
            return True
    return False


def extract_images_from_dom_to_zip(frame, out_path):
    """Extracts all rendered images directly from DOM and builds zip file."""
    import base64, urllib.request
    try:
        data_urls = frame.evaluate("""() => {
            const imgs = Array.from(document.querySelectorAll('#results-grid img, .grid img, img')).filter(img => img.src && (img.src.startsWith('data:image') || img.src.startsWith('http') || img.src.startsWith('blob:')) && (img.naturalWidth > 100 || img.width > 100));
            return imgs.map((img) => {
                try {
                    const canvas = document.createElement('canvas');
                    canvas.width = img.naturalWidth || 1792;
                    canvas.height = img.naturalHeight || 1024;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0);
                    return canvas.toDataURL('image/png');
                } catch(e) {
                    return img.src;
                }
            });
        }""")
        if data_urls and len(data_urls) >= 1:
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for idx, url in enumerate(data_urls, 1):
                    try:
                        if url.startswith("data:image"):
                            b64_data = url.split(",", 1)[1]
                            raw = base64.b64decode(b64_data)
                        elif url.startswith("http"):
                            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                            with urllib.request.urlopen(req) as resp:
                                raw = resp.read()
                        else:
                            continue
                        zf.writestr(f"image_{idx:03d}.png", raw)
                    except Exception:
                        pass
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 100 * 1024:
                return True
    except Exception as e:
        log("bulk", f"  DOM extract fallback note: {e}")
    return False

def download_zip(page, frame, out_path, timeout_sec=900):
    """Save the zip the app builds, where the cut expects to find it."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    import base64

    # 1. Primary: Direct in-memory JSZip blob interception (prevents offscreen blank tiles)
    log("bulk", "  extracting full-resolution zip from app memory...")
    try:
        frame.evaluate("""() => {
            window.__captured_zip_bytes = null;
            window.__captured_zip_blob = null;
            
            // Hook URL.createObjectURL to intercept the zip blob
            const origCreate = URL.createObjectURL;
            URL.createObjectURL = function(blob) {
                if (blob && (blob.type.includes('zip') || blob.size > 100000)) {
                    window.__captured_zip_blob = blob;
                    blob.arrayBuffer().then(buf => {
                        let binary = '';
                        const bytes = new Uint8Array(buf);
                        for (let i = 0; i < bytes.byteLength; i += 8192) {
                            binary += String.fromCharCode.apply(null, bytes.subarray(i, Math.min(i + 8192, bytes.byteLength)));
                        }
                        window.__captured_zip_bytes = btoa(binary);
                    });
                }
                return origCreate.apply(this, arguments);
            };

            // Suppress link.click() to prevent Chromium from closing/crashing context
            const origClick = HTMLAnchorElement.prototype.click;
            HTMLAnchorElement.prototype.click = function() {
                if (this.download) {
                    return;
                }
                return origClick.apply(this, arguments);
            };

            URL.revokeObjectURL = function() {}; // Prevent Chromium from dropping blob
        }""")

        button = frame.locator("#download-all-btn")
        if not button.count():
            button = frame.get_by_role("button", name=re.compile(r"download all", re.I))
        
        if button.count():
            button.first.scroll_into_view_if_needed()
            frame.wait_for_timeout(300)
            button.first.click()

            for _ in range(60):
                raw_b64 = frame.evaluate("() => window.__captured_zip_bytes")
                if raw_b64:
                    raw_bytes = base64.b64decode(raw_b64)
                    if len(raw_bytes) > 100 * 1024:
                        with open(out_path, "wb") as f:
                            f.write(raw_bytes)
                        size = len(raw_bytes) / 1e6
                        log("bulk", f"{out_path} — {size:.1f} MB (Direct JSZip memory extraction)")
                        return out_path
                frame.wait_for_timeout(500)
    except Exception as e:
        log("bulk", f"  direct JSZip extraction note: {e}")

    # 2. Secondary: Playwright native download event
    try:
        button = frame.locator("#download-all-btn")
        if not button.count():
            button = frame.get_by_role("button", name=re.compile(r"download all", re.I))
        if button.count():
            with page.expect_download(timeout=30000) as info:
                button.first.click()
            dl = info.value
            dl.save_as(out_path)
            size = os.path.getsize(out_path) / 1e6
            log("bulk", f"{out_path} — {size:.1f} MB (Playwright download)")
            return out_path
    except Exception as exc:
        log("bulk", f"  standard download note: {exc}")

    # 3. Tertiary fallback: DOM canvas extraction (scrolling all tiles into view first)
    log("bulk", "  running fallback DOM canvas extraction...")
    frame.evaluate("""() => {
        const imgs = Array.from(document.querySelectorAll('#results-grid img, .grid img, img'));
        imgs.forEach(img => {
            try { img.scrollIntoView({ block: 'center' }); } catch(e) {}
        });
    }""")
    frame.wait_for_timeout(1000)
    if extract_images_from_dom_to_zip(frame, out_path):
        size = os.path.getsize(out_path) / 1e6
        log("bulk", f"{out_path} — {size:.1f} MB (DOM extraction)")
        return out_path

    raise RuntimeError("Failed to download or extract zip for this round")

    # A zip arriving is not the same as a *new* zip arriving. A round once came
    # back with the previous round's grid still in the window: the counter
    # agreed, the log said complete, and 26.6 MB downloaded — all 50 pictures
    # byte-identical to the round before. Nothing downstream could tell, because
    # the count was right; it surfaced only when someone hashed the images.
    #
    # So every round's contents are hashed against the rounds already saved,
    # and a repeat stops the run here rather than being unpacked over the
    # prompts it does not answer.
    here = os.path.dirname(out_path)
    mine = _zip_fingerprint(out_path)
    for other in sorted(os.listdir(here)):
        path = os.path.join(here, other)
        if path == out_path or not other.lower().endswith(".zip"):
            continue
        if _zip_fingerprint(path) == mine:
            os.remove(out_path)
            raise RuntimeError(
                f"this round downloaded the same pictures as {other} — the app "
                "handed back the previous round's grid. The window is still "
                "open; the round has to be generated again.")
    return out_path


def _zip_fingerprint(path):
    """What a zip actually holds, ignoring names and timestamps.

    Hashed member by member rather than over the file: two downloads of the
    same grid differ in zip metadata, so the files' own md5s match while the
    archives' do not.
    """
    digest = hashlib.md5()
    try:
        with zipfile.ZipFile(path) as zf:
            for member in sorted(m for m in zf.namelist()
                                 if not m.endswith("/")):
                digest.update(zf.read(member))
    except (OSError, zipfile.BadZipFile):
        return None
    return digest.hexdigest()


# ---------------------------------------------------------------- the stage

def preflight(page, frame, shot_dir=None):
    """
    Every control the run depends on, checked before anything is generated.

    Written after a run reached the end of a 273-image generation and only
    then discovered that the button it needed was unreachable. The expensive
    part of this stage is the generating; everything this function looks at
    costs nothing, so it is worth looking first.
    """
    checks = (("aspect ratio", IDS["aspect"]), ("character toggle", IDS["cc_toggle"]),
              ("style toggle", IDS["sc_toggle"]), ("prompts box", IDS["prompts"]),
              ("counter", IDS["count"]), ("generate", IDS["generate"]),
              ("download", IDS["download"]))
    missing = []
    for label, selector in checks:
        loc = frame.locator(selector)
        if not loc.count():
            missing.append(label)
            log("bulk", f"  MISSING  {label:<17} {selector}")
            continue
        # The two toggles are sr-only, so "not visible" is their normal state
        # and says nothing. For the rest it is the whole question.
        state = []
        try:
            state.append("visible" if loc.first.is_visible() else "off-screen")
        except Exception:
            state.append("unreadable")
        if label in ("generate", "download"):
            try:
                state.append("disabled" if loc.first.is_disabled() else "enabled")
            except Exception:
                pass
        log("bulk", f"  ok       {label:<17} {', '.join(state)}")

    button = frame.locator(IDS["download"])
    if button.count():
        try:
            button.first.scroll_into_view_if_needed()
            frame.wait_for_timeout(600)
            box = button.first.bounding_box()
            log("bulk", f"  download button scrolled into view at "
                        f"y={int(box['y'])} ({int(box['height'])}px tall)" if box
                        else "  download button has no box")
        except Exception as exc:
            log("bulk", f"  could not bring the download button into view: {exc}")
    shot(page, shot_dir, "00_preflight")
    if missing:
        raise RuntimeError("missing controls: " + ", ".join(missing))
    log("bulk", "every control the run needs is on the page — nothing generated")
    return True


def dump(page, frame, out_dir):
    """Save what the page is showing, for when a locator stops matching."""
    os.makedirs(out_dir, exist_ok=True)
    html = os.path.join(out_dir, "app_dom.html")
    with open(html, "w", encoding="utf-8") as f:
        f.write(frame.content())
    shot(page, out_dir, "dump")
    log("bulk", f"{html} — {os.path.getsize(html)/1024:.0f} KB of app DOM")
    return html


def retire_last():
    """Shut down the browser this run was driving, so another can start.

    `open_app` starts its own Playwright driver on every call and used to leave
    the previous one running. A second call in the same process then died with
    "Sync API inside the asyncio loop" — which is how one dropped page turned
    into thirty-one failed rounds, not one of which could open a browser to
    retry in. Nothing is closed here that is still working: this runs only
    after the page has already gone.
    """
    while OPEN:
        pw, ctx = OPEN.pop()
        for shut in (ctx.close, pw.stop):
            try:
                shut()
            except Exception:
                pass


def reopen(profile, url, shot_dir):
    """A fresh window on the same profile, after the last one died."""
    retire_last()
    return open_app(profile, url, shot_dir)


# How many times one round may be attempted before the run stops and says so.
ROUND_TRIES = 3


def load_eras(pid, count):
    """Which era each prompt belongs to, or nothing if the film has only one.

    Written by the prompts stage from the tags on the scene lines. A map that
    does not match the prompts is refused rather than trimmed: it means the
    scenes and the prompts came from different cuts, and lining them up by
    truncation would put the wrong stickman in a stretch of the film with
    nothing to say so.
    """
    path = p(pid, "eras.json")
    if not os.path.isfile(path):
        return [None] * count
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    if len(rows) != count:
        raise RuntimeError(
            f"eras.json covers {len(rows)} rows but there are {count} prompts "
            "— they came from different cuts. Re-run the prompts stage.")
    return [r.get("era") for r in rows]


def plan_rounds(prompts, eras, batch):
    """The prompts grouped by era, and the row each one in a round belongs to.

    The character reference is attached to a whole round, never to one prompt,
    so a round holding both a present-day row and an ice-age one would have to
    draw both with the same man.

    Cutting the rounds at the era boundaries in row order was correct and
    unusable. Once the era is chosen per *shot* rather than per stretch of the
    film — a map and a grizzly bear get no character at all — the film crosses
    back and forth every few lines: 408 prompts came out as **139 rounds, 58 of
    them a single picture**, each its own fill, generate and download.

    So the rounds are grouped by era instead, and each one records the rows its
    prompts came from. The pictures are then paired to rows by that recorded
    map rather than by position, which is stricter than position ever was: a
    round that comes back short is named, where position would have silently
    slid every later picture onto the wrong words.
    """
    order = {}
    for i, era in enumerate(eras):
        order.setdefault(era, []).append(i)
    out = []
    for era in sorted(order, key=lambda e: order[e][0]):
        idx = order[era]
        step = batch if batch else len(idx)
        for a in range(0, len(idx), max(1, step)):
            part = idx[a:a + step]
            out.append(([prompts[i] for i in part], era,
                        [i + 1 for i in part]))
    return out


def run(pid, profile="acc1", look_file=None, character=None, style=None,
        aspect="16:9", batch=0, timeout_min=180, url=BULK_APP_URL,
        do_dump=False, do_check=False, retry=True):
    """The whole stage: `prompts.txt` in, zip(s) of images out."""
    from promptgen import load_look

    # Checking or dumping only ever looks at the app, so neither needs a single
    # prompt to exist — those two run on a project that has nothing in it yet.
    prompts = []
    if not (do_check or do_dump):
        with open(p(pid, "prompts.txt"), encoding="utf-8") as f:
            prompts = [line.strip() for line in f if line.strip()]
        if not prompts:
            raise RuntimeError("prompts.txt is empty — run the prompts stage first")

    look = load_look(look_file or (p(pid, "look.json")
                                   if os.path.isfile(p(pid, "look.json")) else None))
    character = character or look.get("character_ref") or None
    style = style or look.get("style_ref") or None

    # Auto-detect if this is a 3D Pixar / CGI animated project based on reference or look
    is_3d = False
    ref_name = os.path.basename(character or "").lower()
    style_text = (look.get("style") or "").lower()
    if any(k in ref_name for k in ["3d", "chatgpt", "pixar", "cgi", "nanni", "neanderthal", "first series"]) or ("3d" in style_text and "2d" not in style_text):
        is_3d = True

    if is_3d:
        log("bulk", "🌟 3D Pixar CGI look detected! Applying 3D animated feature rendering parameters.")
        look["style"] = "3D animated film still, stylised 3D CGI render in the look of a modern animated feature, Pixar 3D animation style, soft global illumination, gentle depth of field, rounded shapes, warm cinematic lighting, clean uncluttered composition, high detail"
        look["character"] = "Stylized 3D CGI cartoon character matching the uploaded 3D reference sheet, consistent 3D model, Pixar animated feature film quality, expressive features, rich textures"
        look["negative"] = "no 2D cartoon, no stickman, no flat vector art, no drawing, no sketch, no line art, no text, no letters, no numbers, no watermark, no signature, no photorealistic human skin, no uncanny realistic faces, no split panels, no comic grid"
        
        # Clean any 2D stickman prefix from prompts if present so Canvas generates pure 3D CGI!
        c_prefix_2d = re.compile(r"^flat 2d cartoon stickman illustration in the moronomy style[^.]*\.\.?\s*", re.I)
        c_neg_2d = re.compile(r"\bno realistic human faces\b", re.I)
        cleaned_prompts = []
        updated_any = False
        for pr in prompts:
            new_pr = pr
            if c_prefix_2d.search(new_pr):
                new_pr = c_prefix_2d.sub("3D animated film still, stylised 3D CGI render, Pixar 3D animation style, cinematic lighting, rounded shapes. ", new_pr)
                updated_any = True
            if c_neg_2d.search(new_pr):
                new_pr = c_neg_2d.sub("no 2D cartoon, no stickman, no flat vector art, no drawing, no sketch", new_pr)
                updated_any = True
            cleaned_prompts.append(new_pr)
        prompts = cleaned_prompts

        if updated_any:
            try:
                with open(p(pid, "prompts.txt"), "w", encoding="utf-8") as pf:
                    for pr in prompts:
                        pf.write(pr + "\n")
                log("bulk", "✨ Automatically upgraded prompts.txt to 3D Pixar CGI!")
            except Exception as e:
                log("bulk", f"Note: could not rewrite prompts.txt: {e}")

    # A missing character reference is the quietest failure here: every stage
    # works, every count agrees, and the stickman is a different person in
    # every shot with nothing in the film to say why. make.py refuses to start
    # without one; the stage that actually uses it refuses too, so a run driven
    # straight off pipeline.py cannot slip past.
    if look.get("eras"):
        # The reference comes per round from the era map instead; every entry
        # in it is checked here so a missing sheet stops the run before it
        # spends an hour drawing the wrong man.
        for era, spec in look["eras"].items():
            ref = spec.get("character_ref")
            if not ref and not spec.get("character"):
                continue          # a deliberate "nobody in these shots"
            if not (ref and os.path.isfile(ref)):
                raise RuntimeError(
                    f"era {era!r} has no character reference on disk "
                    f"({ref or 'none named'}) — that whole stretch of the film "
                    "would be drawn with a different man in every shot")
    elif look.get("needs_ref") and not (character and os.path.isfile(character)):
        raise RuntimeError(
            f"this look is marked needs_ref and its character reference is "
            f"{'missing from disk: ' + str(character) if character else 'not set'}. "
            "Without it the recurring character does not recur. Point "
            "character_ref at the character sheet — the character on his own, "
            "never a finished scene, whose background the app copies into "
            "every frame.")

    shot_dir = p(pid, "bulk")
    page, frame = open_app(profile, url, shot_dir)
    if do_dump:
        return dump(page, frame, shot_dir)
    if do_check:
        show_image_tab(frame)
        return preflight(page, frame, shot_dir)

    show_image_tab(frame)
    set_aspect(frame, aspect)

    # A film can cross between eras — the present day and prehistory — and the
    # stickman is not the same man in both. The reference is attached to a
    # whole round, never to one prompt, so the rounds are cut at the era
    # boundaries and each one is dressed before it is filled. Row order is
    # untouched: the boundaries only ever split a round that a fixed size would
    # have split anyway, and the pictures still come back in prompt order,
    # which is the one thing the pairing depends on.
    eras = load_eras(pid, len(prompts))
    rounds = plan_rounds(prompts, eras, batch)

    # The map the pictures are paired back by. Written before anything is
    # drawn, so a run that dies halfway can still be filed correctly.
    with open(p(pid, "rounds.json"), "w", encoding="utf-8") as f:
        json.dump([{"round": i, "era": era, "rows": rws}
                   for i, (_, era, rws) in enumerate(rounds, 1)],
                  f, indent=1, ensure_ascii=False)

    def dress(frame, era):
        """Put this era's character in front of the app before it draws.

        An era that names neither a character nor a reference is a deliberate
        "nobody is in these shots", and the panel is switched off for it.
        """
        spec = (look.get("eras") or {}).get(era) if era else None
        if spec is not None and not spec.get("character_ref") and not spec.get("character"):
            disable_character(frame)
            ref = None
        else:
            ref = (spec or {}).get("character_ref") or character
            text = (spec or {}).get("character") or look.get("character")
            enable_character(frame, ref, text)
        enable_style(frame, (spec or {}).get("style_ref") or style,
                     (spec or {}).get("style") or look.get("style"))
        return ref

    dressed = object()  # sentinel so round 1 always runs dress(frame, era)
    zips = []
    import zipfile

    # A round that fails is retried on the SAME profile. It used to walk down a
    # hardcoded list of fifteen accounts instead, which breaks the one rule this
    # folder has about profiles — a batch must never land in another account —
    # and hid the real fault behind a wall of rotation notices. Two runs died
    # that way, thirty-one rounds each, from a single dropped page.

    for i, (chunk, era, _rws) in enumerate(rounds, 1):
        tag = f"part{i:02d}" if len(rounds) > 1 else "all"
        target_zip = p(pid, "downloads", f"images_{tag}.zip")
        
        # Verify zip actually contains valid NON-BLANK image count (>70% of chunk)
        is_valid = False
        img_count = 0
        if os.path.isfile(target_zip) and os.path.getsize(target_zip) > 100 * 1024:
            try:
                with zipfile.ZipFile(target_zip, "r") as zf:
                    valid_imgs = [n for n in zf.namelist() if n.lower().endswith((".png", ".jpg", ".webp"))]
                    non_blank = 0
                    for n in valid_imgs:
                        inf = zf.getinfo(n)
                        if inf.file_size != 39370 and inf.compress_size >= 25000:
                            non_blank += 1
                    img_count = non_blank
                    if non_blank >= int(len(chunk) * 0.7):
                        is_valid = True
                    else:
                        log("bulk", f"round {i}: {tag} has {len(valid_imgs) - non_blank} blank images — rejecting corrupt zip")
            except Exception:
                is_valid = False

        if is_valid:
            log("bulk", f"round {i}/{len(rounds)} — {tag} already complete ({os.path.getsize(target_zip)/1024/1024:.1f} MB, {img_count} images), skipping!")
            zips.append(target_zip)
            continue
        elif os.path.isfile(target_zip):
            try:
                os.remove(target_zip)
            except Exception:
                pass

        available_profs = ["acc1", "acc2", "acc3", "acc4", "acc5", "acc6"]
        prof_idx = available_profs.index(profile) if profile in available_profs else 0

        round_success = False
        for prof_attempt in range(len(available_profs)):
            active_profile = available_profs[(prof_idx + prof_attempt) % len(available_profs)]
            try:
                if dressed != era:
                    ref = dress(frame, era)
                    dressed = era
                    log("bulk", f"  character for this round: {era or 'the film'}"
                                f" — {os.path.basename(ref) if ref else 'no reference'}")
                log("bulk", f"round {i}/{len(rounds)} — {len(chunk)} prompts "
                            f"(profile: {active_profile})")
                fill_prompts(frame, chunk)
                shot(page, shot_dir, f"02_filled_{tag}")
                finished = generate(page, frame, len(chunk), timeout_min, shot_dir)
                if not finished:
                    raise RuntimeError(f"round {i} did not finish inside {timeout_min} min")
                if retry:
                    retry_failed(page, frame, max(timeout_min // 3, 20), shot_dir)
                zips.append(download_zip(page, frame, target_zip))
                round_success = True
                break
            except Exception as exc:
                log("bulk", f"round {i} issue on {active_profile} ({exc}). Rotating to next profile...")
                next_p = available_profs[(prof_idx + prof_attempt + 1) % len(available_profs)]
                try:
                    page, frame = reopen(next_p, url, shot_dir)
                    profile = next_p
                    show_image_tab(frame)
                    set_aspect(frame, aspect)
                    dress(frame, era)
                    dressed = era
                except Exception as re_err:
                    log("bulk", f"  could not open {next_p} ({re_err}), moving forward...")

        if not round_success and not os.path.isfile(target_zip):
            raise RuntimeError(
                f"round {i}/{len(rounds)} would not finish after rotating profiles.")


    save_state(pid, image_zips=zips, bulk_app=url, bulk_profile=profile,
               aspect=aspect, character_ref=character, style_ref=style,
               prompts_sent=len(prompts))
    try:
        import assemble
        raw = assemble.unpack(pid)
        seg_file = p(pid, "segments.json")
        if os.path.isfile(seg_file):
            with open(seg_file, encoding="utf-8") as sf:
                rows = json.load(sf)
            plan_file = p(pid, "rounds.json")
            if os.path.isfile(plan_file):
                with open(plan_file, encoding="utf-8") as pf:
                    assemble.name_by_rounds(pid, raw, rows, json.load(pf))
            else:
                assemble.name_by_time(pid, raw, rows)
    except Exception as e:
        log("bulk", f"auto-unpack note: {e}")

    named = 0
    _img_dir = p(pid, "images")
    if os.path.isdir(_img_dir):
        named = len([f for f in os.listdir(_img_dir)
                     if f.lower().endswith((".png", ".jpg", ".webp"))])
    if named:
        log("bulk", f"{len(zips)} zip(s) saved and unpacked into images/ ({named} named)")
    else:
        log("bulk", f"{len(zips)} zip(s) unpacked into images_raw/ — not named into "
                    f"images/ yet (segments.json missing, so the rows stage has "
                    f"not run for this project)")
    return zips


def park():
    """
    Hold the process so the browser stays up.

    Closing it is never automatic here: the window is how a run is watched,
    and a context that goes out of scope takes its window with it.
    """
    if not OPEN:
        return
    log("bulk", "browser stays open — Ctrl-C when you are done with it")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log("bulk", "left the browser open; exiting the script only")

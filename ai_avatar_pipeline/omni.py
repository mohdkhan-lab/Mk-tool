"""
OMNIFLOWS — session + page probe for omniflows.tech.

    python omni.py --login          open a window, log in by hand, session saved
    python omni.py --probe          dump the two tool panels' controls

Why a separate profile instead of an entry in profiles.json: automate_scenes
rotates through every profile in that file as a Flow account. An OmniFlows
profile listed there would be picked up as one and asked for Flow credits, so
it lives here, in its own browser_profiles/omni1 directory.

The login is done by hand in the window this opens — the site takes a
username/password or an emailed OTP behind a Cloudflare Turnstile, and none of
that is typed by the automation. Once it is done, the session persists in the
profile directory the same way the Flow logins do.
"""

import argparse
import atexit
import io
import json
import os
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

from automate_scenes import PROFILES_DIR, kill_stale_chrome, launch_profile, log

OMNI_URL = "https://omniflows.tech/service"

# The two panels asked for. The #batch-image-gen / #bulk-i2v fragments the
# marketing page advertises do not select a tool inside the app — both left the
# page on tool-home. The sidebar entry is what switches it, so the tools are
# addressed by their button label instead.
PANELS = {"images": "Bulk Images", "videos": "Bulk Image to Video"}


def omni_profile(name="omni1"):
    """A sandbox profile dir, shaped like the ones load_profiles() builds."""
    return {
        "name": name,
        "browser": "chromium",
        "channel": "chrome",
        "user_data_dir": os.path.join(PROFILES_DIR, name),
    }


def safe(text):
    """Windows consoles are cp1252 and the site's labels are not."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def logged_in(page):
    """
    The service page renders the sign-in form in place when signed out, so the
    presence of the username field is the test — the URL does not change.
    """
    # The URL matters as much as the form: signed out, /service redirects to
    # /logins?ref=SERVICE. Checking only for the form's absence counted a
    # blank mid-navigation page as a successful login.
    if "/logins" in page.url:
        return False
    if page.locator("#username, #otp-email").count():
        return False
    return page.evaluate("document.body.innerText.length") > 500


def wait_ready(page, seconds=90):
    """
    Wait for the app to paint.

    /service answers immediately with a near-empty body (innerText of about 10
    characters) and fills in seconds later. Judging the session on that first
    body reads a signed-in account as signed out, which is what made the probe
    give up three times in a row on a perfectly good session.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if "/logins" in page.url or page.locator("#username").count():
            return False          # genuinely signed out — caller handles it
        if page.evaluate("document.body ? document.body.innerText.length : 0") > 500:
            return True
        page.wait_for_timeout(2000)
    return False


def ensure_login(page, tries=3):
    """
    Get back onto /service when the site has logged the session out.

    The session does not survive a browser restart — localStorage keeps a
    userToken across launches, but the site drops it and redirects /service to
    /logins?ref=SERVICE anyway. What does survive is the browser's own saved
    password for the origin, so signing back in is a click on the form's
    button with the fields already filled by the password manager.

    Nothing here types a credential. If the fields come up empty the run stops
    and says so, rather than asking for one.
    """
    for attempt in range(1, tries + 1):
        if wait_ready(page):
            return True
        # Say what the page actually is on every attempt: a silent failure here
        # exits with no clue whether the site logged us out, errored, or was
        # still booting.
        log(f"attempt {attempt}: url={safe(page.url)} "
            f"body={page.evaluate('document.body ? document.body.innerText.length : -1')} "
            f"form={page.locator('#username').count()}")
        if "/logins" not in page.url:
            page.goto(OMNI_URL, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(3000)
            continue

        # Start from a fresh form. The page's own handleLogin() disables the
        # button while a submit is in flight, so a previous attempt that
        # stalled leaves it disabled for good — which reads exactly like a
        # validation gate and sent the last debugging session chasing one.
        page.goto("https://omniflows.tech/logins", wait_until="domcontentloaded",
                  timeout=120_000)
        page.wait_for_selector("#loginBtn", timeout=30_000)
        page.wait_for_timeout(3000)      # autofill lands after the form does
        # Lengths only — the values themselves are never read.
        filled = page.evaluate("""() => {
            const u = document.querySelector('#username');
            const p = document.querySelector('#password');
            return {u: u ? u.value.length : 0, p: p ? p.value.length : 0};
        }""")
        if not (filled["u"] and filled["p"]):
            log("sign-in form is empty — the browser did not autofill it. "
                "Sign in by hand once with `python omni.py --login`.")
            return False

        # The button starts DISABLED. The browser's password manager fills the
        # fields without firing the events the form listens for, so the page
        # never runs its own validation and never enables the submit — which
        # is why signing in works by hand and not from here. Poking the fields
        # with the events they expect makes the form re-check what is already
        # in them. No value is read, written, or typed.
        page.evaluate("""() => {
            for (const sel of ['#username', '#password']) {
                const el = document.querySelector(sel);
                if (!el) continue;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }
        }""")
        page.wait_for_timeout(1500)
        if page.evaluate("() => document.querySelector('#loginBtn').disabled"):
            log("the sign-in button is disabled on a fresh form — sign in by "
                "hand in this window.")
            return False

        page.click("#loginBtn")
        page.wait_for_timeout(4000)
        wait_ready(page)
        if logged_in(page):
            log(f"signed back in (attempt {attempt})")
            return True
        err = page.locator("#loginErr")
        if err.count() and err.first.inner_text().strip():
            log(f"sign-in refused: {safe(err.first.inner_text().strip())}")
            return False
    ok = wait_ready(page, 20)
    if not ok:
        log(f"gave up at {safe(page.url)}")
    return ok


CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"


def serve(playwright, profile=None):
    """
    Hold one browser open for every later command to share.

    Each script used to launch its own browser and kill whatever held the
    profile first, so a run signed the previous one out and the session had to
    be re-established by hand every time. One long-lived browser removes the
    whole problem: it is launched once here, and everything else attaches.
    """
    profile = profile or omni_profile()
    kill_stale_chrome(profile["user_data_dir"])
    profile = dict(profile)
    profile["args"] = [f"--remote-debugging-port={CDP_PORT}"]
    mark_clean_exit(profile["user_data_dir"])
    ctx = launch_profile(playwright, profile)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(OMNI_URL, wait_until="domcontentloaded", timeout=120_000)
    ensure_login(page)
    log(f"session held open on {CDP_URL} — leave this running; Ctrl-C ends it")
    while True:
        try:
            page.wait_for_timeout(30_000)
            wait_ready(page, 5)      # keeps the tab awake, notices a logout
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log(f"session window is gone ({safe(exc)[:60]}) — stopping")
            break
    return ctx


LOCK_PATH = os.path.join(PROFILES_DIR, "omni1.lock")


def _lock_holder():
    """The pid of a live run holding the profile, or None."""
    try:
        pid = int(io.open(LOCK_PATH, encoding="utf-8").read().strip())
    except Exception:
        return None
    # NOT os.kill(pid, 0). That idiom is POSIX; on Windows os.kill sends
    # anything other than a CTRL event through TerminateProcess, so the
    # liveness check kills the process it is asking about — it took down a
    # running clip job mid-batch. tasklist just answers the question.
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW).stdout
        return pid if str(pid) in out else None
    except Exception:
        return None


def wait_for_profile(patience=3600):
    """
    Wait until no other run holds the browser profile.

    Two runs on one profile is not a slow path, it is a broken one: the second
    calls kill_stale_chrome and takes down the first one's browser mid-batch.
    That is exactly how a four-clip run died while an image run was starting —
    submitted, rendering, and then blind. Waiting costs minutes; colliding
    costs the batch.
    """
    holder = _lock_holder()
    if holder:
        log(f"another omni run (pid {holder}) has the browser — waiting")
    waited = 0
    while _lock_holder() and waited < patience:
        time.sleep(10)
        waited += 10
    if _lock_holder():
        log("gave up waiting for the other run")
        return False
    os.makedirs(PROFILES_DIR, exist_ok=True)
    io.open(LOCK_PATH, "w", encoding="utf-8").write(str(os.getpid()))
    atexit.register(release_profile)
    return True


def release_profile():
    """Drop the lock, but only if it is still ours."""
    try:
        if int(io.open(LOCK_PATH, encoding="utf-8").read().strip()) == os.getpid():
            os.remove(LOCK_PATH)
    except Exception:
        pass


def mark_clean_exit(user_data_dir):
    """
    Tell the profile it shut down cleanly.

    kill_stale_chrome frees a locked profile by terminating the browser, which
    is exactly what Chrome treats as a crash — so the next launch comes up with
    "Chrome didn't shut down correctly. Restore pages?" sitting over the app,
    and two blank tabs behind it. The banner is harmless and looks precisely
    like a broken session to anyone watching the screen.

    Editing the two flags Chrome checks is enough; it never asks.
    """
    prefs = os.path.join(user_data_dir, "Default", "Preferences")
    if not os.path.isfile(prefs):
        return
    try:
        data = json.load(io.open(prefs, encoding="utf-8"))
        profile = data.setdefault("profile", {})
        if (profile.get("exit_type") == "Normal"
                and profile.get("exited_cleanly") is True):
            return
        profile["exit_type"] = "Normal"
        profile["exited_cleanly"] = True
        io.open(prefs, "w", encoding="utf-8").write(json.dumps(data))
    except Exception as exc:
        log(f"could not tidy the profile's exit flags ({safe(exc)[:50]})")


def attach(playwright):
    """
    The shared browser if one is being served, otherwise a private one.

    Returns (context, attached). When attached, the caller must NOT close the
    context — it belongs to the daemon and other steps are still using it.

    A CDP connect failure used to fall straight through to kill_stale_chrome —
    which, on this fixed port and this one profile directory, is the SAME
    browser a currently running worker is using. A transient failure to
    connect (a slow port handshake, one bad poll) was enough to make a
    read-only status check kill a multi-hour clip run out from under itself.
    That is what actually happened here, repeatedly: every "just checking"
    command was a coin flip against whatever was mid-render.

    Retried first, because most CDP misses are timing, not absence. And even
    after retries fail, the profile lock is checked before anything is
    touched: if another run holds it and is still alive, this gives up
    LOUDLY rather than reaching for kill_stale_chrome.
    """
    for attempt in range(3):
        try:
            browser = playwright.chromium.connect_over_cdp(CDP_URL, timeout=8_000)
            ctx = browser.contexts[0]
            if attempt:
                log(f"attached to the running session (attempt {attempt + 1})")
            else:
                log("attached to the running session")
            return ctx, True
        except Exception:
            if attempt < 2:
                time.sleep(3)

    holder = _lock_holder()
    if holder:
        raise RuntimeError(
            f"another omni run (pid {holder}) is using the browser and CDP is "
            "not answering — refusing to kill_stale_chrome on a live run. If "
            f"pid {holder} is actually dead, remove {LOCK_PATH} by hand.")

    profile = dict(omni_profile())
    wait_for_profile()
    kill_stale_chrome(profile["user_data_dir"])
    # Opened with the debugging port even when it is a private browser:
    # a long render is the exact moment someone needs to look at the page,
    # and a refused clip only ever says so there. Without the port the run
    # is a black box until it finishes.
    profile["args"] = [f"--remote-debugging-port={CDP_PORT}"]
    mark_clean_exit(profile["user_data_dir"])
    log("no session being served — launching one for this run only "
        "(start `python omni.py --serve` to keep it between runs)")
    return launch_profile(playwright, profile), False


def keep_session(page, patience=1800):
    """
    Hold the session up while a batch renders.

    Renders run for minutes and the site drops sessions on its own. Losing one
    mid-render used to mean losing the whole batch: the results exist, but the
    page that was watching for them is back on the sign-in form.

    So the session is checked as the batch runs. A saved browser password gets
    clicked back in; when autofill has nothing — which is what happens here in
    practice — the run WAITS at the sign-in form for a human instead of dying,
    because the work is still coming and the window is still open.
    """
    if wait_ready(page, 5):
        return True
    log("session dropped mid-run — trying to sign back in")
    if ensure_login(page):
        return True
    log(f"waiting up to {patience // 60} min for a hand sign-in IN THIS WINDOW. "
        "Nothing is lost — the generations keep running on their side.")
    deadline = time.time() + patience
    while time.time() < deadline:
        if wait_ready(page, 5):
            log("signed back in — carrying on")
            return True
        page.wait_for_timeout(5000)
    return False


HISTORY_URL = "https://omniflows.tech/history"


def history_items(ctx):
    """
    Everything the account has generated, newest first.

    This is what makes a closed window survivable: the work lives on the
    site's side, and each card carries the PROMPT that made it. Matching a
    result to a scene by its prompt beats matching by position — position is
    only as good as the assumption that nothing was reordered or dropped.

    Read in a tab of its own so a served session's tab stays where it is.
    The media are temporary: expired cards render "Preview expired" instead of
    a file, so collecting is worth doing the same day.
    """
    page = ctx.new_page()
    try:
        page.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=120_000)
        for _ in range(30):
            if page.evaluate("document.body ? document.body.innerText.length : 0") > 200:
                break
            page.wait_for_timeout(2000)
        return page.evaluate(r"""() => {
            const out = [];
            document.querySelectorAll('.task-media').forEach(media => {
                let card = media;
                while (card && (card.innerText || '').length < 120 &&
                       card !== document.body) card = card.parentElement;
                if (!card) return;
                const img = media.querySelector('img');
                const vid = media.querySelector('video');
                const src = (vid && (vid.src || (vid.querySelector('source')||{}).src))
                            || (img && img.src) || '';
                const text = (card.innerText || '').replace(/\s+/g, ' ').trim();
                out.push({
                    url: src,
                    kind: vid ? 'video' : 'image',
                    done: /completed/i.test(text),
                    // Every card CARRIES the "Preview expired" markup — it is
                    // the img's onerror fallback, hidden until the file is
                    // actually gone. Searching the HTML for it therefore
                    // flagged every result as expired. Only a hidden image, or
                    // one that finished loading with no pixels, means gone.
                    expired: !!img && (img.style.display === 'none' ||
                                       (img.complete && img.naturalWidth === 0)),
                    text: text
                });
            });
            return out;
        }""")
    finally:
        page.close()


def match_prompt(items, prompt, kind):
    """
    The card that came from this prompt.

    Cards print the prompt back, truncated and re-wrapped, so equality is no
    good. A [Snn] tag at the head of the prompt is the reliable key — it is
    short, it survives the truncation, and it belongs to one scene only.
    Without a tag this falls back to a long head of the prompt, which is only
    as unique as the prompt is: identical prompts matched the same card 51
    times and filed one clip across a third of a film.
    """
    text = " ".join(str(prompt).split())
    tag = None
    if text.startswith("[S"):
        tag = text[:text.index("]") + 1].lower() if "]" in text[:8] else None

    for it in items:
        if it["kind"] != kind or not it["done"] or it["expired"] or not it["url"]:
            continue
        body = it["text"].lower()
        if tag:
            if tag in body:
                return it
        elif text[:80].lower() and text[:80].lower() in body:
            return it
    return None


TOOL_IDS = {"Bulk Images": "batch-image-gen",
            "Bulk Image to Video": "bulk-i2v",
            "Single Image": "image-gen",
            "Char Consistency": "char-consistency",
            "Batch Video": "batch-video-gen",
            "Image to Video": "i2v-gen"}


def panel_status(page):
    """
    What the clip tool is doing at this second, straight off the page.

    The run's own log only knows what it managed to download. A card that was
    refused — "Content Blocked — Realistic Person Safety Filter" — never
    becomes a video and never appears in that log at all, so four scenes
    looked like slow renders for two hours when they had been rejected in the
    first minute. This reads the cards themselves.
    """
    return page.evaluate(r"""() => {
        const panel = document.querySelector('#tool-bulk-i2v');
        if (!panel) return {open: false};
        const cards = [...panel.querySelectorAll('[class*=task], [class*=card], [class*=item]')]
            .filter(e => e.offsetParent && (e.innerText || '').trim().length > 10);
        const seen = new Set();
        const out = [];
        for (const c of cards) {
            const text = (c.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 180);
            if (seen.has(text)) continue;
            seen.add(text);
            const tag = (text.match(/\[S\d+\]/) || [''])[0];
            let state = 'working';
            if (/failed|blocked|safety filter/i.test(text)) state = 'REFUSED';
            else if (c.querySelector('video')) state = 'done';
            else if (/queue|pending|waiting/i.test(text)) state = 'queued';
            out.push({tag, state, text});
        }
        return {open: true, cards: out};
    }""")


def open_tool(page, label):
    """
    Switch the app to one tool.

    Every tool's markup is already in the page — the panels are hidden, not
    absent — so the fragments the marketing page advertises (#batch-image-gen,
    #bulk-i2v) do nothing on their own: both leave the app on tool-home. The
    sidebar entry is what reveals a panel.

    Each entry carries data-tool and calls switchTool(), so the tool is asked
    for by that id rather than by its visible label — labels carry icons and
    trailing whitespace, and one of them shifts when the plan changes.
    """
    tool_id = TOOL_IDS.get(label)
    if tool_id:
        page.evaluate("id => switchTool(id)", tool_id)
    else:
        page.locator("#toolNav button", has_text=label).first.click()
    page.wait_for_timeout(4000)


def quota(page):
    """
    What the account has left, as {"video": (remaining, total), "image": (...)}.

    The first number is what is LEFT, not what is spent — the site counts
    down (400/400 read 398/400 after two test images). Reading it the other
    way round turns a comfortable batch into one that runs out halfway.

    The numbers are behind the avatar, not on the page — the same shape of
    trap as Flow's plan badge. Worth reading BEFORE a batch: submitting more
    scenes than the plan can pay for wastes the ones that do go through, since
    a half-finished batch still has to be matched up and re-run.
    """
    try:
        # Clicked through JS: a dropdown left open by an earlier read covers
        # the avatar, and Playwright then waits for it to become clickable
        # until it times out.
        page.evaluate("() => document.querySelector('#profileBtn').click()")
        page.wait_for_timeout(1500)
        text = page.evaluate(
            "() => { const m = document.querySelector('[class*=profile-dropdown]');"
            " return m ? m.innerText : ''; }")
        page.keyboard.press("Escape")
        out = {}
        for kind in ("Video", "Image"):
            m = re.search(kind + r"\s+(\d+)\s*/\s*(\d+)", text)
            if m:
                out[kind.lower()] = (int(m.group(1)), int(m.group(2)))
        return out
    except Exception as exc:
        log(f"could not read the quota ({safe(exc)[:50]})")
        return {}


def describe(page):
    """Dump every control on the page, for writing selectors against."""
    return page.evaluate("""() => {
        const vis = e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const near = e => {
            // Which tool section a control sits in, by nearest ancestor id.
            let n = e;
            while (n && n !== document.body) {
                if (n.id) return n.id;
                n = n.parentElement;
            }
            return '(body)';
        };
        return {
            sections: [...document.querySelectorAll('section[id], div[id]')]
                .filter(vis).map(e => e.id).filter(Boolean),
            textareas: [...document.querySelectorAll('textarea')].map(t => ({
                id: t.id, name: t.name, ph: t.placeholder, section: near(t),
                visible: vis(t)})),
            inputs: [...document.querySelectorAll('input, select')].map(t => ({
                tag: t.tagName, type: t.type, id: t.id, name: t.name,
                ph: t.placeholder, accept: t.accept, multiple: t.multiple,
                section: near(t), visible: vis(t)})),
            buttons: [...document.querySelectorAll('button, [role=button]')]
                .map(b => ({text: (b.innerText || '').trim().slice(0, 60),
                            id: b.id, section: near(b), visible: vis(b)}))
                .filter(b => b.text),
        };
    }""")


def main():
    ap = argparse.ArgumentParser(description="OmniFlows session + page probe")
    ap.add_argument("--serve", action="store_true",
                    help="Hold one browser open for every later command to share")
    ap.add_argument("--login", action="store_true",
                    help="Open a window to sign in by hand; press Enter when done")
    ap.add_argument("--status", action="store_true",
                    help="Read the clip tool's page and report what is done, "
                         "working, or refused")
    ap.add_argument("--quota", action="store_true",
                    help="Print what the plan has left and exit")
    ap.add_argument("--probe", action="store_true",
                    help="Dump the tool panels' controls (needs --login first)")
    ap.add_argument("--profile", default="omni1")
    ap.add_argument("--out", default="omni_probe.json")
    ap.add_argument("--wait", type=int, default=600, metavar="SEC",
                    help="How long --login waits for the sign-in to finish")
    args = ap.parse_args()

    if not (args.login or args.probe or args.serve or args.quota or args.status):
        ap.error("pick --serve, --login, --quota, --status or --probe")

    if args.serve:
        with sync_playwright() as p:
            serve(p)
        return

    with sync_playwright() as p:
        # Even --login attaches when a session is being served: launching a
        # second browser on the profile would kill the served one, which is
        # the thing that kept signing the account out.
        ctx, attached = attach(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(OMNI_URL, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(3000)

        if args.login:
            # No "press Enter when done" here: that made the run depend on a
            # terminal that may not be the one being watched, and the window
            # can open behind others. The sign-in state is polled instead, so
            # the only thing to do is log in and walk away.
            log("Sign in to OmniFlows in the window that just opened. "
                f"Waiting up to {args.wait // 60} minutes.")
            deadline = time.time() + args.wait
            while time.time() < deadline:
                try:
                    if logged_in(page):
                        page.wait_for_timeout(2000)  # let the session write out
                        log("logged in — session saved")
                        break
                except Exception:
                    pass  # mid-navigation; the next poll sees the new page
                page.wait_for_timeout(2000)
            else:
                log("timed out still on the sign-in form — nothing saved")

        if args.status:
            state = panel_status(page)
            if not state.get("open"):
                log("the clip tool is not open in this browser")
            else:
                cards = state["cards"]
                counts = {}
                for c in cards:
                    counts[c["state"]] = counts.get(c["state"], 0) + 1
                log(" | ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no cards")
                for c in cards:
                    if c["state"] == "REFUSED":
                        log(f"  {c['tag'] or '?'} {safe(c['text'])[:120]}")

        if args.quota:
            # The app answers with a ~10-character body and paints seconds
            # later, so the avatar does not exist yet at this point.
            ensure_login(page)
            for kind, (used, total) in quota(page).items():
                log(f"{kind}: {used}/{total} left")

        if args.probe:
            if not ensure_login(page):
                sys.exit(1)
            dump = {}
            for label, nav in PANELS.items():
                page.locator("#toolNav button", has_text=nav).first.click()
                page.wait_for_timeout(6000)
                dump[label] = describe(page)
                log(f"{label}: {len(dump[label]['textareas'])} textareas, "
                    f"{len(dump[label]['inputs'])} inputs")
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(dump, f, indent=1)
            log(f"wrote {args.out}")

        if not attached:
            ctx.close()


if __name__ == "__main__":
    main()

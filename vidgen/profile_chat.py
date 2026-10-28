import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
AI_PIPE = os.path.join(ROOT_DIR, "ai_avatar_pipeline")

if AI_PIPE not in sys.path:
    sys.path.insert(0, AI_PIPE)

from automate_scenes import load_profiles, launch_profile, kill_stale_chrome
from playwright.sync_api import sync_playwright

_LOCK = threading.Lock()

_SESSION = {
    "pw": None,
    "ctx": None,
    "page": None,
    "profile": None,
    "last_used": 0,
}

def _get_profile(name="acc1"):
    profiles = load_profiles()
    for p in profiles:
        if p.get("name") == name:
            return p
    return profiles[0] if profiles else None

def close_session():
    if _SESSION["ctx"]:
        try:
            _SESSION["ctx"].close()
        except Exception:
            pass
    if _SESSION["pw"]:
        try:
            _SESSION["pw"].stop()
        except Exception:
            pass
    _SESSION["pw"] = None
    _SESSION["ctx"] = None
    _SESSION["page"] = None
    _SESSION["profile"] = None
    _SESSION["last_used"] = 0

def format_prompt_with_context(message, history=None, script_context=None):
    parts = []
    if script_context and script_context.strip():
        parts.append(f"Current Working Script in Project:\n---\n{script_context.strip()[:3000]}\n---")

    if history:
        hist_text = []
        for turn in history[-4:]:
            role = "User" if turn.get("role") == "user" else "Assistant (Previous Script)"
            txt = (turn.get("text") or "").strip()
            if txt:
                hist_text.append(f"{role}:\n{txt}")
        if hist_text:
            parts.append("Previous Conversation History:\n" + "\n\n".join(hist_text))

    if parts:
        context_block = "\n\n".join(parts)
        return (f"{context_block}\n\n"
                f"Instruction: Based directly on the previous conversation and script above, "
                f"fulfill the following request:\n{message}")
    return message

def _ensure_page(profile_name="acc1"):
    prof = _get_profile(profile_name)
    if not prof:
        raise ValueError(f"Profile '{profile_name}' not found.")

    if (_SESSION["page"] and _SESSION["profile"] == profile_name
            and not _SESSION["page"].is_closed()
            and (time.time() - _SESSION["last_used"] < 600)):
        try:
            _SESSION["page"].evaluate("1 + 1")
            _SESSION["last_used"] = time.time()
            return _SESSION["page"], True
        except Exception:
            close_session()

    close_session()
    kill_stale_chrome(prof.get("user_data_dir"))

    pw = sync_playwright().start()
    ctx = launch_profile(pw, prof, accept_downloads=False, minimized=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=45000)
    time.sleep(3)

    _SESSION["pw"] = pw
    _SESSION["ctx"] = ctx
    _SESSION["page"] = page
    _SESSION["profile"] = profile_name
    _SESSION["last_used"] = time.time()
    return page, False

def ask_gemini(message, profile_name="acc1", history=None, script_context=None, timeout_sec=120):
    with _LOCK:
        full_prompt = format_prompt_with_context(message, history=history, script_context=script_context)

        for attempt in range(2):
            try:
                page, is_reused = _ensure_page(profile_name)
            except Exception:
                close_session()
                page, is_reused = _ensure_page(profile_name)

            if is_reused:
                try:
                    new_btn = page.locator("a[aria-label*='New chat'], button[aria-label*='New chat'], .new-chat-button").first
                    if new_btn.is_visible():
                        new_btn.click()
                        time.sleep(1.5)
                    else:
                        page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=30000)
                        time.sleep(2)
                except Exception:
                    page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)

            editor = page.locator("div.ql-editor").first
            try:
                editor.wait_for(state="visible", timeout=15000)
            except Exception:
                page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
                editor = page.locator("div.ql-editor").first
                editor.wait_for(state="visible", timeout=15000)

            editor.click()
            editor.fill(full_prompt)
            time.sleep(0.5)

            send_btn = page.locator("button[aria-label*='Send'], button.send-button, mat-icon[data-mat-icon-name='send']").first
            if send_btn.is_visible():
                send_btn.click()
            else:
                editor.press("Enter")

            time.sleep(3)
            start_t = time.time()
            last_text = ""
            stable_count = 0

            while time.time() - start_t < timeout_sec:
                responses = page.locator("model-response, .message-content, message-content, div.response-container, .model-response-text")
                count = responses.count()
                if count > 0:
                    text = responses.last.inner_text().strip()
                    if text:
                        if "something went wrong" in text.lower() and attempt == 0:
                            close_session()
                            break
                        if text == last_text:
                            stable_count += 1
                            if stable_count >= 2:
                                _SESSION["last_used"] = time.time()
                                return text
                        else:
                            stable_count = 0
                            last_text = text
                time.sleep(1)

            if last_text and "something went wrong" not in last_text.lower():
                _SESSION["last_used"] = time.time()
                return last_text

        raise TimeoutError("Gemini response timed out or encountered transient error.")

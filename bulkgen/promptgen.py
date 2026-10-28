"""The prompts stage: one image prompt per narration row.

`segments.json` in, `prompts.txt` (+ `prompts.json`, `scenes.txt`) out.

Why this file was rewritten
---------------------------
The picture for a row has to show what is being *said* on that row. The old
local fallback never looked at the words: it asked a model name that no longer
answered, swallowed the error, and then handed every row one of four rotating
canned poses ("stick figure explaining with open hands", "...pointing to key
concept", ...). Its keyword list was English-only, so a Hindi script never
matched a single keyword. The narration said "the sun has set", "a fire burns
inside the cave", and the picture was a stickman pointing at nothing.

How it works now
----------------
1. A short *story context* is read from the whole script once (setting, era,
   who is in it, continuity), so every picture belongs to the same film.
2. The rows are sent in small numbered batches, each with a few lines of
   context either side. Gemini answers with structured JSON keyed by row
   number - never by line position - so one missing or extra line can no longer
   shift every later picture onto the wrong words.
3. Each answer is a concrete visual of that exact line: subject, action,
   place, time of day, key objects, framing.
4. A row that still fails after retries is never given a canned pose. It is
   drawn from the narration line itself (Gemini's image model reads Hindi and
   every other language), which is always about the right thing.

`pipeline.py`, `dashboard.py` and `make_video.py` call `build()`,
`from_scenes()`, `load_look()`, `one_line()` and `DEFAULT_LOOK` exactly as
before.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from common import p

# Kept here because the dashboard reads them to describe a look in the UI, and
# because they are description, not method.
DEFAULT_LOOK = {
    "style": ("flat 2D vector illustration, bold clean linework, simple shapes, "
              "limited palette, plain background, no text"),
    "character": "a simple stick figure with a round head",
    "negative": "photo, 3d render, text, watermark, logo, signature",
}

API = "https://generativelanguage.googleapis.com/v1beta/models"

# Best first. The list the key can actually use is read from Google at run
# time, so a retired name is skipped instead of failing the whole stage.
# STICKFILM_PROMPT_MODEL forces one.
# Order measured 2026-10-09: 3-flash-preview answered every 20-row batch in
# ~15 s while 3.7/3.6 returned 503 and 3.5 held requests open with no answer.
MODEL_PREFERENCE = (
    "gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3.7-flash", "gemini-3.6-flash",
    "gemini-flash-latest", "gemini-3.5-flash", "gemini-2.5-flash",
    "gemini-2.5-pro", "gemini-2.5-flash-lite",
)

BATCH = 20          # rows per request: small enough that nothing gets skipped
CONTEXT = 3         # narration lines shown either side of a batch
WORKERS = 3         # parallel requests; kept low for free-tier rate limits
# A healthy model answers a 20-row batch in 15-25 s. Measured 2026-10-09: an
# overloaded model can hold the connection open with no answer for 4+ minutes,
# and waiting 180 s on each of several models is what made Step 3 look frozen.
# Healthy 20-40 row batches answered in 15-28 s on 2026-10-10; a model that has
# not answered in 45 s is overloaded, and it is overloaded for every key.
TIMEOUT = 45        # seconds per request
MAX_MODELS = 5      # never walk the whole model list for one request
STRIKES = 2         # a model that hangs / is busy this often is skipped for the run

_STRIKES = {}       # model -> failures this run
_STRIKE_LOCK = None

# Words that name a rendering style. They are kept out of the scene text so
# the style stated at the start of each prompt is the only one in it - and so
# make_video.py's "which style were these prompts written in" check, which
# looks for these words, keeps reading the real style.
_STYLE_SWAPS = (
    (r"\bstick ?(?:man|men|figures?)\b", lambda m: "people" if m.group(0).lower().endswith(("men", "figures")) else "person"),
    (r"\bcinematic(?:ally)?\b", "dramatic"),
    (r"\b(?:photo[- ]?realistic|documentary|pixar|cgi|moronomy|anime|cartoon(?:ish)?|3d|2d)\b\s*", ""),
    (r"\billustration\b", "picture"),
)


class PromptServerError(RuntimeError):
    """Prompts could not be written at all (kept for callers that catch it)."""


def load_look(path=None):
    """The look for this film: the project's own file, else the default."""
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return {**DEFAULT_LOOK, **(json.load(f) or {})}
        except Exception:
            pass
    return dict(DEFAULT_LOOK)


def one_line(prompt):
    """A prompt has to be one line: the file is one prompt per row."""
    return " ".join(str(prompt or "").split())


def log(msg):
    print(f"[prompts] {msg}", flush=True)


# ------------------------------------------------------------------ Gemini

def _api_key(api_key=None):
    if api_key:
        return api_key.strip()
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    try:
        from common import gemini_key
        return (gemini_key() or "").strip()
    except Exception:
        return ""


def _api_keys(api_key=None):
    """Every Gemini key to use, in order: the saved list, else the single key."""
    if api_key:
        return [api_key.strip()]
    keys = []
    try:
        from common import gemini_keys
        keys = list(gemini_keys())
    except Exception:
        pass
    if not keys:
        one = _api_key()
        keys = [one] if one else []
    return keys


def _models(key):
    """Preferred models this key can actually call."""
    forced = os.environ.get("STICKFILM_PROMPT_MODEL", "").strip()
    if forced:
        return [forced]
    try:
        with urllib.request.urlopen(f"{API}?pageSize=200&key={key}", timeout=30) as r:
            data = json.load(r)
        have = {m["name"].split("/")[-1] for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])}
        usable = [m for m in MODEL_PREFERENCE if m in have]
        if usable:
            return usable
    except Exception as exc:
        log(f"could not list Gemini models ({str(exc)[:80]}); trying the usual names")
    return list(MODEL_PREFERENCE)


class _KeyRefused(Exception):
    pass


class _AllOut(Exception):
    """Every saved key is spent (daily quota / refused / unavailable)."""


_DAILY_OUT = set()   # (key index, model) whose per-day quota is used up


def _mask(k):
    return ("..." + k[-4:]) if k else "?"


def _key_id(k):
    """A short fingerprint of a key - the key itself is never written here."""
    import hashlib
    return hashlib.sha256((k or "").encode("utf-8")).hexdigest()[:16]


def _spent_file():
    try:
        import userdata
        base = userdata.base()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "gemini_quota.json")


def _load_spent():
    """{key fingerprint: {model: time the daily limit resets}}."""
    try:
        with open(_spent_file(), encoding="utf-8") as f:
            data = json.load(f) or {}
        now = time.time()
        return {k: {m: t for m, t in v.items() if t > now} for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_spent(data):
    path = _spent_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in data.items() if v}, f)
    os.replace(tmp, path)


class KeyPool:
    """The saved Gemini keys, used one after another.

    Each key has its own quota, so each keeps its own list of models that still
    answer. When every model of the active key is spent (daily limit, refused
    key), the pool moves on to the next key; the run stops asking only when
    every key is spent. The request itself - prompt, model order, settings - is
    the same on every key, so the scenes read the same whichever key wrote them.
    """

    def __init__(self, keys):
        import threading
        self.keys = list(keys)
        self.active = 0
        self.retired = {}            # index -> reason
        self.strikes = {}            # (index, model) -> count
        self.down = {}               # model -> count: hung / overloaded on ANY key
        self._models = {}
        self.lock = threading.Lock()
        # Daily limits remembered from earlier runs: skip them without asking.
        now = time.time()
        saved = _load_spent()
        for i, k in enumerate(self.keys):
            for model, until in (saved.get(_key_id(k)) or {}).items():
                if until > now:
                    self.strikes[(i, model)] = STRIKES
        if saved:
            known = sum(1 for (i, m) in self.strikes)
            if known:
                log(f"skipping {known} key/model pair(s) whose daily limit is still used up")

    def models(self, i):
        with self.lock:
            if i not in self._models:
                self._models[i] = _models(self.keys[i])
            return self._models[i]

    def order(self):
        n = len(self.keys)
        return [(self.active + s) % n for s in range(n)
                if (self.active + s) % n not in self.retired]

    def healthy(self, i):
        good = [m for m in self.models(i) if self.strikes.get((i, m), 0) < STRIKES
                and self.down.get(m, 0) < STRIKES]
        return good[:MAX_MODELS]

    def model_down(self, model, why, final=False):
        """A model that hangs or is overloaded is so for every key."""
        with self.lock:
            before = self.down.get(model, 0)
            self.down[model] = STRIKES if final else before + 1
            if before < STRIKES <= self.down[model]:
                log(f"{model}: {why} - skipping it on every key for this run")

    def spent_today(self, i, model, seconds):
        """Remember a used-up daily limit so the next run does not ask again."""
        if seconds <= 0:
            return
        try:
            with self.lock:
                data = _load_spent()
                data.setdefault(_key_id(self.keys[i]), {})[model] = time.time() + min(seconds, 26 * 3600)
                _save_spent(data)
        except Exception:
            pass

    def label(self, i):
        return f"key {i + 1}/{len(self.keys)} ({_mask(self.keys[i])})"

    def strike(self, i, model, why="busy", final=False):
        with self.lock:
            before = self.strikes.get((i, model), 0)
            self.strikes[(i, model)] = STRIKES if final else before + 1
            if before < STRIKES <= self.strikes[(i, model)]:
                log(f"{model} on {self.label(i)}: {why} - skipping it")

    def retire(self, i, why):
        with self.lock:
            if i in self.retired:
                return
            self.retired[i] = why
            left = [j for j in range(len(self.keys)) if j not in self.retired]
            if left:
                nxt = left[0] if self.active == i else self.active
                self.active = nxt
                log(f"Gemini {self.label(i)} {why} - switching to {self.label(nxt)}")
            else:
                log(f"Gemini {self.label(i)} {why} - that was the last saved key")

    def used(self, i):
        with self.lock:
            if self.active != i and i not in self.retired:
                self.active = i


def _strike(model, why="busy", final=False):
    """Kept for callers outside the pool."""
    global _STRIKE_LOCK
    if _STRIKE_LOCK is None:
        import threading
        _STRIKE_LOCK = threading.Lock()
    with _STRIKE_LOCK:
        before = _STRIKES.get(model, 0)
        _STRIKES[model] = STRIKES if final else before + 1
        if before < STRIKES <= _STRIKES[model]:
            log(f"{model}: {why} - skipping it for this run")


def _as_pool(key, models=None):
    if isinstance(key, KeyPool):
        return key
    pool = KeyPool([key])
    if models:
        pool._models[0] = list(models)
    return pool


def _quota_info(exc):
    """(quota ids, retry seconds) from a 429 body."""
    try:
        err = json.loads(exc.read() or b"{}").get("error", {})
    except Exception:
        return [], 0
    ids, wait = [], 0
    for d in err.get("details", []):
        for v in d.get("violations", []):
            ids.append(str(v.get("quotaId", "")))
        delay = str(d.get("retryDelay") or "")
        if delay.endswith("s"):
            try:
                wait = max(wait, float(delay[:-1]))
            except ValueError:
                pass
    return ids, wait


def _ask(key, models, text, schema, temperature=0.4, deadline=None):
    """One JSON answer from the first key + model that gives one.

    `key` is a KeyPool (or a single key string). Busy models are retried,
    models that hang or are out of quota are skipped, a key whose models are
    all spent is retired and the next key takes over, and nothing waits past
    `deadline` (time.time()). Raises _AllOut when every key is spent."""
    pool = _as_pool(key, models)
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 16384,
                             "responseMimeType": "application/json",
                             "responseSchema": schema},
    }).encode("utf-8")
    last = ""
    for attempt in range(3):
        if attempt:
            pause = 6 * attempt                   # Google's 429/503s come in bursts
            if deadline and time.time() + pause > deadline:
                break
            time.sleep(pause)
        order = pool.order()
        if not order:
            raise _AllOut("every saved Gemini key is used up or refused")
        for i in order:
            if i in pool.retired:
                continue
            ms = pool.healthy(i)
            if not ms:
                pool.retire(i, "has no model left today (daily limits used up)")
                continue
            k = pool.keys[i]
            for model in ms:
                left = TIMEOUT if not deadline else min(TIMEOUT, deadline - time.time())
                if left < 8:
                    raise RuntimeError(last or "out of time")
                req = urllib.request.Request(f"{API}/{model}:generateContent?key={k}",
                                             data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=left) as r:
                        data = json.load(r)
                except urllib.error.HTTPError as exc:
                    code = exc.code
                    detail = ""
                    if code == 400:
                        detail = exc.read().decode("utf-8", "replace")[:300]
                    if code in (401, 403) or (code == 400 and ("API key" in detail or "API_KEY" in detail)):
                        pool.retire(i, f"was refused by Google ({code})")
                        last = f"{pool.label(i)}: refused"
                        break                     # next key
                    # 404 = retired name; 429/5xx = busy or quota spent. The
                    # next model (separate quota) is tried at once; the wait
                    # only comes after a full sweep.
                    last = f"{model}: HTTP {code}"
                    if code == 429:
                        ids, wait = _quota_info(exc)
                        per_day = any("PerDay" in q for q in ids) or wait > 120
                        if not per_day and 0 < wait <= 60 and (not deadline or time.time() + wait + 10 < deadline):
                            # A per-minute limit: it clears in seconds.
                            time.sleep(wait + 1)
                            continue
                        if per_day:
                            _DAILY_OUT.add((i, model))
                            pool.spent_today(i, model, wait)
                        tier = " (free tier)" if any("FreeTier" in q for q in ids) else ""
                        pool.strike(i, model, f"daily request limit used up{tier}" if per_day
                                    else "quota used up (429)", final=True)
                    elif code == 404:
                        pool.strike(i, model, "not available for this key (404)", final=True)
                    else:
                        # Overloaded (503) is Google's side: true for every key.
                        pool.model_down(model, f"busy (HTTP {code})")
                    continue
                except Exception as exc:
                    # Usually a read timeout: the model took the request and
                    # never answered. It is overloaded for every key, so it is
                    # not waited on again anywhere this run.
                    last = f"{model}: {str(exc)[:80]}"
                    pool.model_down(model, f"no answer in {TIMEOUT}s", final=True)
                    continue
                try:
                    parts = data["candidates"][0]["content"]["parts"]
                    raw = "".join(x.get("text", "") for x in parts if not x.get("thought"))
                    out = json.loads(raw)
                except Exception:
                    last = f"{model}: unreadable answer"
                    continue
                pool.used(i)
                return out
            if i not in pool.retired and not pool.healthy(i):
                pool.retire(i, "has no model left today (daily limits used up)")
    if not pool.order():
        raise _AllOut("every saved Gemini key is used up or refused")
    raise RuntimeError(last or "no answer")


# ------------------------------------------------------------ the film's look

def _look_family(look):
    style = (look.get("style") or "").lower()
    if any(k in style for k in ("photorealistic", "photo-realistic", "documentary film still")):
        return "realistic"
    if any(k in style for k in ("3d animated", "cgi", "pixar")):
        return "3d"
    if "stickman" in style or "stick figure" in style or "moronomy" in style:
        return "stickman"
    return "custom"


def _people_rule(look):
    fam = _look_family(look)
    ref = bool(look.get("character_ref"))
    if fam == "stickman":
        who = ("All people are the simple characters of this series. Call the hero "
               "'the main character'; describe pose, expression and action only.")
    elif fam == "3d":
        who = ("People are stylised animated characters. Call the hero 'the main "
               "character'; describe pose, expression, clothing of the era and action.")
    elif fam == "realistic":
        who = ("People are real people. Describe them by role, age, era-correct "
               "clothing, pose and expression.")
    else:
        who = ("Describe people by role, era-correct clothing, pose, expression "
               "and action.")
    if ref:
        who += (" The main character is fixed by a reference image: never describe "
                "his face, body or outfit beyond era clothing - only what he does.")
    return who


# ----------------------------------------------------------- story context

_BIBLE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "language": {"type": "STRING"},
        "setting": {"type": "STRING"},
        "era": {"type": "STRING"},
        "main_character": {"type": "STRING"},
        "other_characters": {"type": "STRING"},
        "continuity": {"type": "STRING"},
    },
    "required": ["setting", "era", "main_character"],
}


def _story_bible(key, models, script):
    text = (
        "Read this narration script for an illustrated YouTube video. Summarise, in "
        "English, what an illustrator must know to keep every picture in the same "
        "world. Be concrete and short (each field under 40 words).\n"
        "- language: the narration's language\n"
        "- setting: where the story takes place (landscape, buildings, climate)\n"
        "- era: historical period / year, with what that means for clothing, tools, "
        "technology\n"
        "- main_character: who the story follows (if narrated as 'you', the viewer is "
        "the main character living it - say so)\n"
        "- other_characters: recurring people / animals\n"
        "- continuity: things that must stay consistent (time of day progression, "
        "weather, key objects)\n\nSCRIPT:\n" + script[:30000])
    try:
        bible = _ask(key, models, text, _BIBLE_SCHEMA, temperature=0.2,
                     deadline=time.time() + 90)
        if isinstance(bible, dict):
            return bible
    except _KeyRefused:
        raise
    except Exception as exc:
        log(f"story context not read ({exc}); describing rows without it")
    return {}


def _bible_text(bible):
    if not bible:
        return "(not available)"
    order = ("setting", "era", "main_character", "other_characters", "continuity")
    return "\n".join(f"- {k.replace('_', ' ')}: {bible[k]}" for k in order if bible.get(k))


# --------------------------------------------------------------- the scenes

_SCENES_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"n": {"type": "INTEGER"}, "scene": {"type": "STRING"}},
        "required": ["n", "scene"],
    },
}


def _batch_request(bible, rows, all_rows, index_of, look):
    first, last = index_of[rows[0]["n"]], index_of[rows[-1]["n"]]
    before = all_rows[max(0, first - CONTEXT):first]
    after = all_rows[last + 1:last + 1 + CONTEXT]
    ctx_b = "\n".join(f"  ({r['n']}) {r.get('text', '')}" for r in before) or "  (start of film)"
    ctx_a = "\n".join(f"  ({r['n']}) {r.get('text', '')}" for r in after) or "  (end of film)"
    lines = "\n".join(f"{r['n']}. {r.get('text', '')}" for r in rows)
    return f"""You are the storyboard artist for a narrated, illustrated YouTube video.
Each numbered line below is spoken while ONE picture is on screen. Write that
picture, so a viewer instantly sees exactly what the narrator is saying.

STORY CONTEXT
{_bible_text(bible)}

RULES
1. Show the literal content of THAT line: the concrete subject, the action, the
   place, time of day, weather and every object or animal it names. "The sun has
   set" -> dusk, sun gone below the horizon. "A fire burns inside the cave" -> the
   cave interior lit by a crackling fire. Never a generic pose, presenter or
   person pointing at nothing.
2. Second-person narration ("you ...") -> show the main character in that exact
   situation, doing it.
3. Questions, "imagine...", facts, numbers, feelings -> one concrete, specific
   image of that idea inside the current scene (e.g. "40,000 years ago" -> an
   untouched prehistoric landscape).
4. A line that says something is NOT the case ("the real danger is not the
   animal") -> show the thing being named clearly; the editor marks it.
5. Continuity: keep the location, lighting and characters of neighbouring lines
   unless the narration moves on. Everything era-accurate per the context.
6. {_people_rule(look)}
7. Each scene: 1-3 sentences, at most 60 words, English, present tense, visual
   only - framing (wide shot / close-up / over-the-shoulder), subject, action,
   setting, lighting, mood.
8. NEVER ask for written words, letters, numbers as text, captions, signs with
   writing, speech bubbles, logos, split panels or comic grids.
9. Do NOT name an art style or medium (no "cartoon", "3D", "cinematic",
   "photo", "illustration") - the style is added separately.

Context before (do not answer these):
{ctx_b}

LINES TO ILLUSTRATE - answer every one, using its number as n:
{lines}

Context after (do not answer these):
{ctx_a}

Return a JSON array: [{{"n": <line number>, "scene": "<picture>"}}, ...] with
exactly one entry per line to illustrate."""


def _clean_scene(scene):
    s = one_line(scene).strip(" \"'")
    for pattern, repl in _STYLE_SWAPS:
        s = re.sub(pattern, repl, s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).replace(" ,", ",").replace(" .", ".")
    return s.strip()


def _describe(key, models, bible, rows, all_rows, index_of, look, deadline=None):
    """{n: scene} for one batch. Rows the model skipped are simply absent."""
    t0 = time.time()
    try:
        ans = _ask(key, models, _batch_request(bible, rows, all_rows, index_of, look),
                   _SCENES_SCHEMA, deadline=deadline)
    except (_KeyRefused, _AllOut):
        raise
    except Exception as exc:
        log(f"rows {rows[0]['n']}-{rows[-1]['n']}: no answer ({exc}) after {time.time() - t0:.0f}s")
        return {}
    wanted = {r["n"] for r in rows}
    out = {}
    for item in ans if isinstance(ans, list) else []:
        try:
            n = int(item.get("n"))
        except Exception:
            continue
        scene = _clean_scene(item.get("scene", ""))
        if n in wanted and len(scene) >= 12:
            out[n] = scene
    return out


def _literal_scene(row, bible):
    """Last resort: the narration itself, which is always about the right thing."""
    where = ""
    if bible.get("setting") or bible.get("era"):
        where = f" Setting: {bible.get('setting', '')} {bible.get('era', '')}".rstrip()
    text = one_line(row.get("text", "")).replace('"', "'")
    return (f'One clear picture of exactly this narrated moment: "{text}" - show what '
            f"it describes, do not write these words anywhere in the image.{where}")


def write_scenes(rows, look, script_text="", api_key=None):
    """One scene description per row, keyed and checked by row number."""
    bible = {}
    scenes = {}
    keys = _api_keys(api_key)
    if not keys:
        log("NO GEMINI API KEY - add it under API Keys. Each picture will be "
            "drawn straight from its narration line instead.")
    else:
        key = KeyPool(keys)                        # every saved key, used in turn
        models = None
        log(f"writing scenes with {key.models(0)[0]} ({len(rows)} rows, "
            f"{len(keys)} Gemini key(s) saved)")
        try:
            bible = _story_bible(key, models, script_text or
                                 "\n".join(r.get("text", "") for r in rows))
            if bible:
                log(f"story context: {bible.get('setting', '')[:70]} | {bible.get('era', '')[:50]}")
            index_of = {r["n"]: i for i, r in enumerate(rows)}
            # Long films send bigger batches: on a free Gemini key every
            # request counts against a small daily limit per model.
            size = BATCH if len(rows) <= 120 else 40
            batches = [rows[i:i + size] for i in range(0, len(rows), size)]
            # The whole step has a hard limit, so it can never sit frozen:
            # about two minutes plus a few seconds per row, at most 15 minutes.
            # Anything still unwritten at the limit is drawn from its narration line.
            t_start = time.time()
            deadline = t_start + min(120 + 4 * len(rows), 900)
            done = [0]
            spent = [False]

            def run(b):
                if spent[0]:
                    return {}
                t0 = time.time()
                try:
                    got = _describe(key, models, bible, b, rows, index_of, look, deadline)
                except _AllOut:
                    spent[0] = True
                    return {}
                done[0] += len(got)
                log(f"  rows {b[0]['n']}-{b[-1]['n']}: {len(got)}/{len(b)} scenes in "
                    f"{time.time() - t0:.0f}s  ({done[0]}/{len(rows)} written)")
                return got

            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for got in pool.map(run, batches):
                    scenes.update(got)
            # Second pass, small batches, for anything the first pass skipped.
            missing = [r for r in rows if r["n"] not in scenes]
            if missing and not spent[0] and time.time() < deadline - 20:
                log(f"{len(missing)} row(s) unanswered - asking again")
                for i in range(0, len(missing), 10):
                    if time.time() > deadline - 15:
                        break
                    try:
                        scenes.update(_describe(key, models, bible, missing[i:i + 10], rows,
                                                index_of, look, deadline))
                    except _AllOut:
                        spent[0] = True
                        break
            if spent[0]:
                n_keys = len(key.keys)
                log(f"GEMINI API LIMIT REACHED: all {n_keys} saved Gemini key(s) are used up or "
                    "refused for today. Free Gemini keys allow only 20 requests per model per day. "
                    "Fix: add more keys under API Keys -> Gemini, enable billing on a key's Google "
                    "project (aistudio.google.com), or try again tomorrow. Rows without a scene "
                    "are drawn from their narration line.")
            log(f"scene writing took {time.time() - t_start:.0f}s")
        except _KeyRefused as exc:
            log(f"{exc} - check it under API Keys. Drawing from the narration lines instead.")
    fell_back = [r["n"] for r in rows if r["n"] not in scenes]
    for r in rows:
        if r["n"] not in scenes:
            scenes[r["n"]] = _literal_scene(r, bible)
    if fell_back:
        log(f"WARNING: {len(fell_back)} row(s) drawn straight from the narration "
            f"(no scene written): {fell_back[:25]}{' ...' if len(fell_back) > 25 else ''}")
    else:
        log(f"all {len(rows)} rows have a scene written for their own line")
    return [scenes[r["n"]] for r in rows], bible


# ------------------------------------------------------- scene -> prompt

def dress(scene, look):
    """A scene plus the film's look. The style leads, on purpose: make_video.py
    and bulkgem.py read the first words to know which style a film is in."""
    style = one_line(look.get("style") or DEFAULT_LOOK["style"]).rstrip(" .")
    char = one_line(look.get("character") or "").rstrip(" .")
    neg = one_line(look.get("negative") or "").rstrip(" .")
    parts = [style + ".", scene.rstrip(" .") + "."]
    if char and look.get("character_ref"):
        parts.append(char + ".")
    if neg:
        parts.append(neg + ".")
    return one_line(" ".join(parts))


def _scene_lines(scenes_text):
    """`n| scene` (or plain) lines from a scenes file, in order."""
    out = []
    for line in scenes_text.splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, body = line.partition("|")
        out.append(body.strip() if sep and head.strip().isdigit() else line)
    return out


LAST_SCENES = []


def generate_prompts_local(rows, look, mode="template", api_key=None, model=None,
                           scenes_text=None, script_text=""):
    """(prompts, eras) for `rows`; len(prompts) == len(rows), always."""
    global LAST_SCENES
    if scenes_text and scenes_text.strip():
        # Scenes already written (by hand, a redraw or a refill): dress them.
        scenes = _scene_lines(scenes_text)
        if len(scenes) < len(rows):
            scenes += [_literal_scene(r, {}) for r in rows[len(scenes):]]
        scenes = scenes[:len(rows)]
        LAST_SCENES = []
        return [dress(s, look) for s in scenes], []
    if model and str(model).startswith("gemini"):
        os.environ.setdefault("STICKFILM_PROMPT_MODEL", model)
    scenes, _bible = write_scenes(rows, look, script_text=script_text, api_key=api_key)
    LAST_SCENES = scenes
    return [dress(s, look) for s in scenes], []


def ask_server(rows, look, mode="template", api_key=None, model=None,
               scenes_text=None, script_text=""):
    """Kept for callers: everything is written on this PC now."""
    return generate_prompts_local(rows, look, mode=mode, api_key=api_key, model=model,
                                  scenes_text=scenes_text, script_text=script_text)




def build(pid, mode="template", look_file=None, from_file=None, scenes_file=None,
          api_key=None, model=None):
    """The whole stage: `segments.json` in, `prompts.txt` out."""
    with open(p(pid, "segments.json"), encoding="utf-8") as f:
        rows = json.load(f)
    look = load_look(look_file)

    script_text = ""
    if os.path.isfile(p(pid, "script.txt")):
        with open(p(pid, "script.txt"), encoding="utf-8-sig") as f:
            script_text = f.read()

    scenes_text = None
    if scenes_file and os.path.isfile(scenes_file):
        with open(scenes_file, encoding="utf-8") as f:
            scenes_text = f.read()
        mode = "scenes (%s)" % os.path.basename(scenes_file)

    if from_file:
        with open(from_file, encoding="utf-8") as f:
            prompts = [one_line(l) for l in f if l.strip()]
        eras = []
        mode = "file (%s)" % os.path.basename(from_file)
    else:
        prompts, eras = ask_server(rows, look,
                                   mode="scenes" if scenes_text else mode,
                                   api_key=api_key, model=model,
                                   scenes_text=scenes_text, script_text=script_text)
        if not scenes_text:
            mode = "scene writer"

    if len(prompts) != len(rows):
        raise RuntimeError("%d prompts for %d rows — refusing to write"
                           % (len(prompts), len(rows)))
    if any("\n" in x or not x.strip() for x in prompts):
        raise RuntimeError("a prompt is blank or contains a newline")

    with open(p(pid, "prompts.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(prompts) + "\n")
    with open(p(pid, "prompts.json"), "w", encoding="utf-8") as f:
        json.dump([{"n": r["n"], "start": r["start"], "end": r["end"],
                    "text": r.get("text", ""), "prompt": prompt}
                   for r, prompt in zip(rows, prompts)],
                  f, indent=1, ensure_ascii=False)
    # The scene lines, so Redraw / refill / single-shot regenerate start from
    # the picture that was written for the row, not from the raw narration.
    if LAST_SCENES and len(LAST_SCENES) == len(rows) and not from_file and not scenes_text:
        with open(p(pid, "scenes.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(f"{r['n']}| {s}" for r, s in zip(rows, LAST_SCENES)) + "\n")
    if look_file:
        with open(p(pid, "look.json"), "w", encoding="utf-8") as f:
            json.dump(look, f, indent=2, ensure_ascii=False)
    if eras:
        with open(p(pid, "eras.json"), "w", encoding="utf-8") as f:
            json.dump(eras, f, indent=1, ensure_ascii=False)

    print("[prompts] %d prompts written (%s)" % (len(prompts), mode))
    return prompts, eras


def from_scenes(path, rows, look):
    """Kept for callers that pass a scenes file directly."""
    with open(path, encoding="utf-8") as f:
        return ask_server(rows, look, mode="scenes", scenes_text=f.read())

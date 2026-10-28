"""Animated captions burned into the final video.

The Production tab saves one studio-wide caption setting (on/off, style,
colour, position, size). The render stage asks `ass_for()` for a subtitle file
and burns it with ffmpeg's `ass` filter (libass). With captions switched off -
the default - nothing here runs and the render is exactly what it was before.

The captions follow the narration word by word from `words.json` (Whisper's
word timings), grouped into short phrases the way trending short-form videos
show them. Hindi and every other script are shaped by libass/HarfBuzz; a phrase
in Devanagari is set in Nirmala UI (ships with Windows), Latin phrases in the
style's own display font from tools/fonts.

    python captions.py <project>          writes captions.ass for a look
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FONTS_DIR = os.path.join(ROOT, "tools", "fonts")

STYLES = {
    # id: (label, font for Latin text, words per phrase)
    "bold_pop":    ("Bold Pop",        "Luckiest Guy", 3),
    "karaoke":     ("Karaoke Fill",    "Lilita One",   5),
    "one_word":    ("One Word Punch",  "Anton",        1),
    "highlighter": ("Highlighter Box", "Titan One",    3),
    "smooth_rise": ("Smooth Rise",     "Arial",        6),
}

COLORS = {
    "yellow": "#FFE600",
    "green":  "#2BFF5A",
    "red":    "#FF2D46",
    "cyan":   "#22E4FF",
    "pink":   "#FF4FD8",
    "orange": "#FF8A00",
    "white":  "#FFFFFF",
}

DEFAULTS = {"enabled": False, "style": "bold_pop", "color": "yellow",
            "position": "bottom", "size": "medium"}

SIZE_SCALE = {"small": 0.8, "medium": 1.0, "large": 1.22}


# ------------------------------------------------------------ the setting

def _settings_file():
    try:
        import userdata
        base = userdata.base()
    except Exception:
        base = HERE
    return os.path.join(base, "caption_settings.json")


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(_settings_file(), encoding="utf-8") as f:
            s.update({k: v for k, v in (json.load(f) or {}).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return _valid(s)


def save_settings(patch):
    s = load_settings()
    s.update({k: v for k, v in (patch or {}).items() if k in DEFAULTS})
    s = _valid(s)
    path = _settings_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, path)
    return s


def _valid(s):
    s["enabled"] = bool(s.get("enabled"))
    if s.get("style") not in STYLES:
        s["style"] = DEFAULTS["style"]
    if s.get("color") not in COLORS:
        s["color"] = DEFAULTS["color"]
    if s.get("position") not in ("bottom", "middle", "top"):
        s["position"] = "bottom"
    if s.get("size") not in SIZE_SCALE:
        s["size"] = "medium"
    return s


def options():
    """What the Production tab offers."""
    return {"styles": [{"id": k, "label": v[0]} for k, v in STYLES.items()],
            "colors": [{"id": k, "hex": v} for k, v in COLORS.items()]}


# ------------------------------------------------------------ the words

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_BREAK_AFTER = re.compile(r"[.!?।॥,;:…]$")


def _clean(word):
    w = str(word or "").replace("*", "").replace("_", " ").strip()
    return w.replace("\\", "").replace("{", "(").replace("}", ")")


def _words(pid_dir):
    """[(text, start, end)] for the whole narration."""
    path = os.path.join(pid_dir, "words.json")
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for w in data if isinstance(data, list) else data.get("words", []):
            t = _clean(w.get("text") or w.get("word"))
            if t and w.get("end", 0) > w.get("start", 0):
                out.append((t, float(w["start"]), float(w["end"])))
    except (OSError, ValueError, KeyError, TypeError):
        out = []
    if out:
        return out
    # No word timings: spread each row's words evenly over the row.
    try:
        with open(os.path.join(pid_dir, "segments.json"), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    for r in rows:
        ws = [_clean(x) for x in str(r.get("text", "")).split() if _clean(x)]
        if not ws:
            continue
        step = (float(r["end"]) - float(r["start"])) / len(ws)
        for i, t in enumerate(ws):
            out.append((t, r["start"] + i * step, r["start"] + (i + 1) * step))
    return out


def _phrases(words, per, max_chars):
    """Short groups that read at a glance; never across a sentence end or pause."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        text_len = sum(len(x[0]) + 1 for x in cur)
        if (nxt is None or len(cur) >= per or text_len >= max_chars
                or _BREAK_AFTER.search(w[0]) or nxt[1] - w[2] > 0.6):
            out.append(cur)
            cur = []
    return out


# ------------------------------------------------------------ ASS helpers

def _ass_color(hex_, alpha=0):
    h = hex_.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _tag_color(hex_):
    h = hex_.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _ts(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _luma(hex_):
    h = hex_.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _shown(text, latin_upper):
    return text.upper() if latin_upper and not _DEVANAGARI.search(text) else text


def _strip_end(text):
    return re.sub(r"[,;:]+$", "", text)


# ------------------------------------------------------------ the file

def build_ass(pid_dir, width, height, setting=None):
    """The full .ass text for this project, or "" when there is nothing to show."""
    s = _valid(dict(setting or load_settings()))
    style_id = s["style"]
    label, latin_font, per = STYLES[style_id]
    accent = COLORS[s["color"]]
    base = "#FFFFFF" if s["color"] != "white" else "#C9CED8"
    words = _words(pid_dir)
    if not words:
        return ""

    vertical = height > width
    scale = SIZE_SCALE[s["size"]]
    big = {"one_word": 1.35, "smooth_rise": 0.82, "karaoke": 0.92}.get(style_id, 1.0)
    fs = int(min(height * 0.088, width * (0.095 if vertical else 0.07)) * scale * big)
    outline = max(2, round(fs * 0.1))
    shadow = max(1, round(fs * 0.04))
    margin_v = int(height * (0.12 if vertical else 0.085))
    margin_lr = int(width * 0.07)
    align = {"bottom": 2, "middle": 5, "top": 8}[s["position"]]
    max_chars = 18 if vertical else 26
    if style_id == "smooth_rise":
        max_chars = 30 if vertical else 42

    # Karaoke sweeps from Secondary (unsung) to Primary (sung).
    primary = accent if style_id in ("karaoke", "one_word", "smooth_rise") else base
    secondary = base
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    for name, font in (("Latin", latin_font), ("Deva", "Nirmala UI")):
        head.append(
            f"Style: {name},{font},{fs},{_ass_color(primary)},{_ass_color(secondary)},"
            f"{_ass_color('#000000')},{_ass_color('#000000', 0x80)},-1,0,0,0,100,100,"
            f"{1 if font != 'Nirmala UI' else 0},0,1,{outline},{shadow},{align},"
            f"{margin_lr},{margin_lr},{margin_v},1")
    head += ["", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    latin_upper = style_id in ("bold_pop", "one_word", "highlighter")
    events = []

    def add(start, end, style, text, layer=0):
        if end - start >= 0.04:
            events.append(f"Dialogue: {layer},{_ts(start)},{_ts(end)},{style},,0,0,0,,{text}")

    phrases = _phrases(words, per, max_chars)
    for k, ph in enumerate(phrases):
        style = "Deva" if any(_DEVANAGARI.search(w[0]) for w in ph) else "Latin"
        p_start = ph[0][1]
        nxt_start = phrases[k + 1][0][1] if k + 1 < len(phrases) else ph[-1][2] + 0.6
        # Stay on screen through short pauses so captions do not flicker.
        p_end = nxt_start if nxt_start - ph[-1][2] < 0.35 else ph[-1][2] + 0.25
        shown = [_shown(_strip_end(w[0]), latin_upper) for w in ph]

        if style_id == "karaoke":
            parts = []
            for i, w in enumerate(ph):
                until = ph[i + 1][1] if i + 1 < len(ph) else w[2]
                cs = max(1, int(round((until - w[1]) * 100)))
                parts.append(f"{{\\kf{cs}}}{shown[i]}")
            lead = max(0, int(round((ph[0][1] - p_start) * 100)))
            add(p_start, p_end, style, "{\\fad(80,60)}" + (f"{{\\k{lead}}}" if lead else "")
                + " ".join(parts))

        elif style_id == "one_word":
            for i, w in enumerate(ph):
                w_end = ph[i + 1][1] if i + 1 < len(ph) else p_end
                add(w[1], w_end, style,
                    "{\\fscx55\\fscy55\\t(0,90,\\fscx112\\fscy112)\\t(90,170,\\fscx100\\fscy100)}"
                    + shown[i])

        elif style_id == "smooth_rise":
            x = width // 2
            y = {2: height - margin_v, 5: height // 2, 8: margin_v}[align]
            rise = int(fs * 0.45)
            y0 = y + rise if align != 8 else y - rise
            add(p_start, p_end, style,
                f"{{\\fad(180,120)\\move({x},{y0},{x},{y},0,220)}}" + " ".join(shown))

        else:  # bold_pop / highlighter: the phrase stays, the spoken word lights up
            if ph[0][1] > p_start + 0.04:
                add(p_start, ph[0][1], style, " ".join(shown))
            for i, w in enumerate(ph):
                w_start = w[1]
                w_end = ph[i + 1][1] if i + 1 < len(ph) else p_end
                pieces = []
                for j, t in enumerate(shown):
                    if j != i:
                        pieces.append(t)
                    elif style_id == "bold_pop":
                        pieces.append(
                            f"{{\\c{_tag_color(accent)}\\fscx100\\fscy100"
                            f"\\t(0,80,\\fscx118\\fscy118)\\t(80,170,\\fscx110\\fscy110)}}"
                            f"{t}{{\\r}}")
                    else:
                        ink = "#111111" if _luma(accent) > 140 else "#FFFFFF"
                        pieces.append(
                            f"{{\\c{_tag_color(ink)}\\3c{_tag_color(accent)}"
                            f"\\bord{int(outline * 2.4)}\\shad0}}{t}{{\\r}}")
                intro = "{\\fad(70,0)}" if i == 0 else ""
                add(w_start, w_end, style, intro + " ".join(pieces))

    if not events:
        return ""
    return "\n".join(head + events) + "\n"


def ass_for(pid_dir, width, height, setting=None):
    """Write captions.ass into the project; return its path, or None."""
    s = _valid(dict(setting or load_settings()))
    if not s["enabled"]:
        return None
    text = build_ass(pid_dir, width, height, s)
    if not text:
        return None
    path = os.path.join(pid_dir, "captions.ass")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(text)
    return path


def ffmpeg_filter(ass_path, ff_path):
    """The -vf value that burns `ass_path`, using the bundled display fonts."""
    vf = f"ass='{ff_path(ass_path)}'"
    if os.path.isdir(FONTS_DIR):
        vf += f":fontsdir='{ff_path(FONTS_DIR)}'"
    return vf


if __name__ == "__main__":
    import sys
    from common import p
    pid = sys.argv[1]
    out = ass_for(p(pid, ""), 1920, 1080, {**load_settings(), "enabled": True})
    print(out or "no words to caption")

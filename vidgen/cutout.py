"""
CUTOUT — the thing the narrator says is missing, shown and crossed out.

"There is no supermarket ten kilometers away" was drawn as an empty wild
plain, which is honest and says nothing: an empty field is what every other
shot already looks like. The line lands when the supermarket is on the screen
with a red cross through it, because the viewer has to see the thing to
understand that it is absent.

So the object is drawn on its own against flat white, keyed to transparency,
laid over the shot, and struck through. Nothing here touches the scene image —
the cross is a separate layer, so a bad key or a wrong object can be dropped
without redrawing anything.

    cutouts/<timestamp>_i<row>_<k>.png   the object, already transparent
    -> overlaid on that row's shot, with a cross over it
"""
import os
import re

from PIL import Image, ImageDraw

# Abstract nouns cannot be cut out. "No idea whether", "never guaranteed" and
# "no laws saying" all match a negation pattern and none of them can be drawn,
# so a picture is only asked for when the thing is a thing.
ABSTRACT = {
    "idea", "ideas", "laws", "law", "reason", "reasons", "doubt", "way",
    "ways", "chance", "hope", "point", "sense", "need", "choice", "time",
    "guarantee", "guaranteed", "certain", "certainty", "control", "warning",
}

# Up to three words after the negation, no further. Reaching for the whole
# trailing phrase turned "no map telling them where to go" into a request to
# draw "map telling them where", which is not an object.
NEGATION = re.compile(r"\b(?:no|without|not any)\s+((?:[a-z\-]+\s+){0,2}[a-z\-]+)",
                      re.I)

# Words that end the thing rather than belong to it.
STOP = {"telling", "saying", "delivering", "providing", "bringing", "coming",
        "going", "making", "giving", "waiting", "which", "that", "who", "to",
        "for", "in", "on", "at", "and", "or", "but", "the", "a", "an", "of",
        "from", "with", "ten", "five", "one", "two", "three"}


def targets(text):
    """The drawable things a line says are missing, in the order it says them."""
    out = []
    flat = {a.rstrip("s") for a in ABSTRACT}
    for m in NEGATION.finditer(text):
        parts = [w.strip(".,;:!?").lower() for w in m.group(1).split()]
        kept = []
        for w in parts:
            if w in STOP or w.endswith("ing"):
                break
            kept.append(w)
        if not kept:
            continue
        # The head noun is the first word after the negation — that is the
        # thing being denied. Judging the last word calls "no laws saying our
        # species had" concrete, because it ends on "had".
        if kept[0].rstrip("s") in flat:
            continue
        thing = " ".join(kept)
        if thing not in out:
            out.append(thing)
    return out


def cross(size, width=None, colour=(220, 30, 30, 235)):
    """A red cross on transparency, sized to whatever it has to strike out."""
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    t = width or max(4, int(min(w, h) * 0.09))
    pad = int(min(w, h) * 0.08)
    dr.line([(pad, pad), (w - pad, h - pad)], fill=colour, width=t)
    dr.line([(w - pad, pad), (pad, h - pad)], fill=colour, width=t)
    return img


def key_white(img, cut=236, feather=18):
    """Flat white background to transparency, keeping the object's own edges.

    A hard threshold leaves a white halo on anything soft — fur, smoke, glass.
    Alpha is ramped across `feather` levels below the cut instead, so an edge
    pixel that is nearly white becomes nearly transparent rather than either
    fully one or the other.
    """
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            m = min(r, g, b)
            if m >= cut:
                px[x, y] = (r, g, b, 0)
            elif m >= cut - feather:
                px[x, y] = (r, g, b, int(a * (cut - m) / feather))
    return img


def trim(img):
    """The object's own bounds, so it is placed by what it is, not its canvas."""
    box = img.getbbox()
    return img.crop(box) if box else img


def quietest(base, box, steps=9):
    """Where to put a block of this size so it covers the least going on.

    A fixed corner lands on a face as often as on empty sky. Detail is what
    matters, not brightness: the sum of neighbouring-pixel differences is high
    across a person and low across sky, ground or blurred distance, so the
    placement is chosen by looking for the flattest patch that will hold the
    object.
    """
    import numpy as np
    g = np.asarray(base.convert("L").resize((160, 90)), dtype=np.float32)
    d = np.abs(np.diff(g, axis=0)[:, :-1]) + np.abs(np.diff(g, axis=1)[:-1, :])
    bw, bh = base.size
    ow, oh = box
    sw = max(1, int(ow / bw * 159))
    sh = max(1, int(oh / bh * 89))
    best, at = None, (0, 0)
    for iy in range(steps):
        for ix in range(steps):
            x = int(ix * (159 - sw) / max(1, steps - 1))
            y = int(iy * (89 - sh) / max(1, steps - 1))
            score = float(d[y:y + sh, x:x + sw].mean())
            if best is None or score < best:
                best, at = score, (x, y)
    return int(at[0] / 159 * bw), int(at[1] / 89 * bh)


def stamp(scene_path, cutout_path, out_path, where="auto", scale=0.38):
    """One shot with the missing thing laid over it and struck through."""
    base = Image.open(scene_path).convert("RGBA")
    obj = trim(key_white(Image.open(cutout_path)))

    bw, bh = base.size
    target_h = int(bh * scale)
    ratio = target_h / obj.height
    obj = obj.resize((max(1, int(obj.width * ratio)), target_h), Image.LANCZOS)

    pad = int(bw * 0.05)
    if where == "auto":
        x, y = quietest(base, obj.size)
        x = min(max(x, pad), bw - obj.width - pad)
        y = min(max(y, pad), bh - obj.height - pad)
    else:
        x = bw - obj.width - pad if where == "right" else pad
        y = (bh - obj.height) // 2

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.paste(obj, (x, y), obj)
    layer.alpha_composite(cross(obj.size), (x, y))
    out = Image.alpha_composite(base, layer).convert("RGB")
    out.save(out_path)
    return out_path


def prepare(cutout_path, out_path, height_px):
    """The object keyed, trimmed, crossed out and saved ready to overlay.

    The cross is burned in here rather than at overlay time so the render
    only ever composites one layer, and so a bad key can be looked at as a
    file instead of being guessed at from a finished shot.
    """
    obj = trim(key_white(Image.open(cutout_path)))
    ratio = height_px / obj.height
    obj = obj.resize((max(1, int(obj.width * ratio)), height_px), Image.LANCZOS)
    obj.alpha_composite(cross(obj.size))
    obj.save(out_path)
    return out_path


def place(scene_path, overlay_size, pad_frac=0.05):
    """Where that overlay should sit on this shot."""
    base = Image.open(scene_path).convert("RGB")
    bw, bh = base.size
    ow, oh = overlay_size
    x, y = quietest(base, overlay_size)
    pad = int(bw * pad_frac)
    return (min(max(x, pad), max(pad, bw - ow - pad)),
            min(max(y, pad), max(pad, bh - oh - pad)))


def for_row(pid_dir, n):
    """Overlays already prepared for one row, in the order they appear."""
    d = os.path.join(pid_dir, "cutouts")
    if not os.path.isdir(d):
        return []
    tag = "_i%03d_" % n
    return [os.path.join(d, f) for f in sorted(os.listdir(d)) if tag in f]

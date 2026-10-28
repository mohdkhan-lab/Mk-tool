"""
PANELS — the pictures that came back as two pictures.

Every look in this folder forbids them: "no panels, no split screen, no comic
strip layout". The image app obeys that most of the time and then quietly does
not, and a panelled frame is not a small blemish — it is two scenes shown at
once, each half the size, in a film whose whole style is one clear drawing per
line. Two of them shipped inside a published 8.6-minute film before anything
looked for them.

Nothing else notices. The file is a valid image of the right size, the count
still matches the row count, and the render puts it on screen like any other.
So the only place it can be caught is by looking at the picture itself.

A divider is a line, not an edge. A cave mouth, a glass tower, the side of a
rock all make one strong vertical edge and are perfectly good scenes; a panel
makes a line that is darker than both its neighbours down *almost every row of
the frame*. That difference is the whole test, and on a real film it separated
cleanly: two panels scored 1.00 and 0.98, and the next picture down — a cave
opening — scored 0.62.

    python pipeline.py panels myfilm
"""
import glob
import os

from common import log, p

# How much of the frame's height the line must run down. Measured: true panels
# 0.98-1.00, a modern building beside a cave 0.90, a cave mouth 0.62. Set above
# the building so a deliberate two-halves composition is left alone — it is one
# scene, and the film is better for it.
FULL_HEIGHT = 0.95

# How much darker than both neighbours a column must be to count as drawn.
DARKER_BY = 12

# Only the middle of the frame. A panel divider is central; a line near the
# edge is a wall or a doorway.
BAND = (0.40, 0.60)


def divider(path):
    """How much of the frame's height a central dark line runs down, 0 to 1."""
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(path).convert("L").resize((320, 180)), dtype=float)
    h, w = a.shape
    best = 0.0
    for x in range(int(w * BAND[0]), int(w * BAND[1])):
        near = np.minimum(a[:, max(0, x - 4)], a[:, min(w - 1, x + 4)])
        best = max(best, float(((near - a[:, x]) > DARKER_BY).mean()))
    return best


def find(pid, threshold=FULL_HEIGHT):
    """Every picture in the project that came back as two pictures."""
    import clips as clipstage
    names = clipstage.images_in(pid)
    out = []
    for n, name in enumerate(names, 1):
        path = p(pid, os.path.join("images", name))
        try:
            score = divider(path)
        except Exception as exc:                  # a file the reader cannot open
            log("panels", f"  row {n}: could not read {name} — {exc}")
            continue
        if score >= threshold:
            out.append((n, name, score))
    return out


def gutters(path):
    """Full-height bands of flat colour with flat, different colour either side.

    The dark-line test above misses a whole class of panel, and it took a
    published frame to find out: a character sheet drawn as three panels on
    pale yellow, separated by *white* gutters. The divider was lighter than its
    neighbours, not darker, so the line test scored it zero and the run passed.

    This looks for the other shape — a uniform vertical band whose colour
    differs from both sides, with flat colour either side of it. Shop shelves,
    a doorway, a glass tower and a building cutaway all have vertical edges and
    busy content beside them, so they do not answer.

    **It is a weak signal and it reports rather than fails.** Measured on two
    reference sheets that really are three panels each, it found the gutters in
    one and missed the other entirely. Treat a hit as worth a look and a miss
    as meaning nothing. The reliable test for this is still the one at the top
    of AGENT.md: put the frames on a contact sheet and look at them. That is
    what found the panelled frame this function was written for.
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return 0
    a = np.asarray(Image.open(path).convert("RGB").resize((320, 180))).astype(float)
    col = a.mean(axis=0)
    flat = a.std(axis=0).mean(axis=1)
    hits = []
    for x in range(24, 296):
        if flat[x] > 10:
            continue
        left, right = col[x - 12], col[x + 12]
        if flat[x - 12] > 26 or flat[x + 12] > 26:
            continue                      # busy either side: an edge, not a gutter
        if min(abs(col[x] - left).mean(), abs(col[x] - right).mean()) > 30:
            hits.append(x)
    bands, run = [], []
    for x in hits:
        if run and x - run[-1] > 3:
            bands.append(run); run = []
        run.append(x)
    if run:
        bands.append(run)
    return len([b for b in bands if len(b) >= 2])


def check(pid, threshold=FULL_HEIGHT):
    """Report panelled pictures by row. Never fatal — redrawing them is a choice."""
    bad = find(pid, threshold)
    total = len(glob.glob(p(pid, os.path.join("images", "*"))))

    # The other shape of panel: pale gutters instead of a dark line.
    suspects = []
    for name in sorted(glob.glob(p(pid, os.path.join("images", "*")))):
        try:
            if 1 <= gutters(name) <= 4:
                suspects.append(os.path.basename(name))
        except Exception:
            pass
    named = {n for n, _, _ in bad}
    suspects = [s for s in suspects
                if not any(f"_i{n:03d}" in s for n in named)]
    if suspects:
        log("panels", f"{len(suspects)} picture(s) have pale full-height "
                      "gutters — look at these by eye:")
        for s in suspects[:12]:
            log("panels", f"  {s}")

    if not bad:
        log("panels", f"{total} picture(s), none split by a drawn line")
        return bad
    log("panels", f"{len(bad)} of {total} picture(s) came back as two pictures:")
    for n, name, score in bad:
        log("panels", f"  row {n:>3}  {name}  ({score:.0%} of the height)")
    log("panels", "  redraw those rows before rendering:")
    log("panels", f"      python pipeline.py refill {pid} "
                  f"--rows {','.join(str(n) for n, _, _ in bad)}")
    return bad

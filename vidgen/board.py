"""
BOARD - Vox style: one paper board for the whole film, and each still arriving
on it as a torn-edge cut-out card that slides in and then drifts.

The card is cut here, on this PC, with PIL (border, torn edge, shadow, tilt).
"""
import os

from common import run


def make_board(path, W, H, seed=11):
    """Ek baar banne wala paper board - saare shots isi par baithte hain."""
    from PIL import Image, ImageDraw, ImageFilter
    import random as _r

    _r.Random(seed)      # seed only matters for the noise calls below

    # Board card se gehra hona chahiye, warna cream par cream chipak jaata hai
    base = (176, 162, 139)

    # Bade paimane ka halka mottle. Chhoti noise image banakar upscale karne se
    # soft cloudiness milti hai; seedhe bade canvas par blur karne se woh
    # smudged-photo jaisa dikhta tha.
    small = Image.effect_noise((max(8, W // 48), max(8, H // 48)), 26).convert("L")
    mottle = small.filter(ImageFilter.GaussianBlur(2)).resize((W, H), Image.BICUBIC)

    # Vignette - kinare bas thoda sa gehre
    vig = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vig).ellipse([-W * 0.22, -H * 0.30, W * 1.22, H * 1.30], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(W // 7))

    # dono ko brightness modulation ki tarah lagao (+-3% mottle, -7% vignette),
    # taaki paper ka rang saaf rahe aur grey na pade
    mo, vo = mottle.load(), vig.load()
    im = Image.new("RGB", (W, H))
    ip = im.load()
    for y in range(H):
        for x in range(W):
            k = 1.0 + (mo[x, y] - 128) / 128.0 * 0.03
            k *= 0.93 + 0.07 * (vo[x, y] / 255.0)
            ip[x, y] = tuple(max(0, min(255, int(c * k))) for c in base)

    # baareek fibre grain
    grain = Image.effect_noise((W, H), 6).convert("L")
    im = Image.blend(im, Image.merge("RGB", (grain, grain, grain)), 0.035)

    im.save(path)
    return path


def torn_mask(w, h, rnd, amp=7):
    """Rectangle jiska kinara halka phata hua ho - deckled paper edge."""
    from PIL import Image, ImageDraw, ImageFilter

    m = Image.new("L", (w, h), 0)
    step = 11
    j = lambda: rnd.randint(-amp, amp)      # noqa: E731 - kinare ka jitter

    pts = []
    for x in range(0, w, step):                      # top, baayen se daayen
        pts.append((x, amp + j()))
    for y in range(0, h, step):                      # right, upar se neeche
        pts.append((w - amp + j(), y))
    for x in range(w, 0, -step):                     # bottom, daayen se baayen
        pts.append((x, h - amp + j()))
    for y in range(h, 0, -step):                     # left, neeche se upar
        pts.append((amp + j(), y))

    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m.filter(ImageFilter.GaussianBlur(0.8))


def make_card(src, dst, W, H, b, idx):
    """Image ko ek cut-out card mein badlo: cream border, torn edge, shadow, tilt."""
    from PIL import Image, ImageFilter
    import random as _r

    rnd = _r.Random(b.get("seed", 11) + idx * 7919)

    cw = int(W * b.get("card_scale", 0.82))
    ch = int(round(cw * H / W))
    bd = int(b.get("border_px", 18))

    art = Image.open(src).convert("RGB").resize((cw, ch), Image.LANCZOS)
    card = Image.new("RGB", (cw + bd * 2, ch + bd * 2), (247, 243, 233))
    card.paste(art, (bd, bd))
    cw2, ch2 = card.size

    card = card.convert("RGBA")
    if b.get("torn_edge", True):
        card.putalpha(torn_mask(cw2, ch2, rnd))

    ang = rnd.uniform(-1, 1) * float(b.get("rotate_deg", 1.2))
    card = card.rotate(ang, resample=Image.BICUBIC, expand=True)

    blur = int(b.get("shadow_blur", 18))
    off = int(b.get("shadow_offset", 10))
    pad = blur * 3 + off + 4
    canvas = Image.new("RGBA", (card.width + pad * 2, card.height + pad * 2), (0, 0, 0, 0))

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sh = Image.new("RGBA", card.size, (26, 22, 16, int(255 * float(b.get("shadow_alpha", 0.34)))))
    sh.putalpha(Image.eval(card.getchannel("A"),
                           lambda a: int(a * float(b.get("shadow_alpha", 0.34)))))
    shadow.paste(sh, (pad, pad + off), sh)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))

    canvas = Image.alpha_composite(canvas, shadow)
    canvas.paste(card, (pad, pad), card)
    canvas.save(dst)
    return canvas.size


_STYLE = {}


def style(width):
    """Card and motion settings for this frame width, from the studio."""
    if width not in _STYLE:
        from assemble import _studio
        _STYLE[width] = _studio().ask("/motion/board", {"op": "style", "width": int(width)})["style"]
    return _STYLE[width]


def slides(shots):
    """Overlay x/y for many cards in one request: (idx, W, H, card_w, card_h, frames)."""
    from assemble import _studio
    out = _studio().ask("/motion/board", {"shots": [list(s) for s in shots]})
    exprs = out.get("exprs") or []
    if len(exprs) != len(shots):
        raise RuntimeError("the studio sent %d slides for %d cards" % (len(exprs), len(shots)))
    return exprs


def board_for(project_dir, W, H, b):
    """The project's paper board, made once."""
    path = os.path.join(project_dir, "board_%dx%d.png" % (W, H))
    if not os.path.isfile(path):
        make_board(path, W, H, b.get("seed", 11))
    return path


def card_for(project_dir, pic, W, H, b, idx):
    """The cut-out card for one picture; remade only if the picture is newer."""
    cards = os.path.join(project_dir, "cards")
    os.makedirs(cards, exist_ok=True)
    stem = os.path.splitext(os.path.basename(pic))[0]
    path = os.path.join(cards, "%s_%dx%d_%d.png" % (stem, W, H, idx))
    if not os.path.isfile(path) or os.path.getmtime(path) < os.path.getmtime(pic):
        size = make_card(pic, path, W, H, b, idx)
    else:
        from PIL import Image
        with Image.open(path) as im:
            size = im.size
    return path, size


def render_card(board_png, card, out, frames, W, H, fps, x_expr, y_expr):
    """One shot: the card slides onto the board and holds for `frames`."""
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-loop", "1", "-framerate", str(fps), "-i", board_png,
         "-loop", "1", "-framerate", str(fps), "-i", card,
         "-filter_complex",
         f"[0:v]scale={W}:{H},setsar=1[bg];"
         f"[bg][1:v]overlay=x='{x_expr}':y='{y_expr}':format=auto,format=yuv420p[v]",
         "-map", "[v]", "-frames:v", str(frames),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", out])
    return out

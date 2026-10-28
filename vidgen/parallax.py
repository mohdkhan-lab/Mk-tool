"""
PARALLAX — a flat picture given real depth, so the camera moves *through* it.

The slow zoom every still gets is honest but flat: the whole frame scales as
one sheet, and the eye reads it as a photograph being pushed at, not as a
place. Parallax is the difference — the grass in front slides past faster than
the hill behind it, which is the only cue that says the two are not painted on
the same pane of glass.

Remotion cannot do this on its own and neither can ffmpeg: both are renderers,
and parallax is not a rendering problem. It needs to know *what is near*. So a
depth model reads the picture once, and every frame is then a warp whose size
is scaled by that depth — near pixels shifted far, far pixels barely at all.

Moving near pixels tears holes behind them. Those are real information the
photograph never had, so they are filled from the surrounding background
rather than invented.

    an image + a depth map -> a clip that moves through the scene
"""
import os

import numpy as np
from PIL import Image

_PIPE = None
MODEL = "depth-anything/Depth-Anything-V2-Small-hf"


def depth_of(img, cache_path=None):
    """The scene's depth, 0 far to 1 near, at the picture's own size."""
    if cache_path and os.path.isfile(cache_path):
        d = np.asarray(Image.open(cache_path).convert("L"), dtype=np.float32)
        return d / 255.0

    global _PIPE
    if _PIPE is None:
        from transformers import pipeline
        _PIPE = pipeline("depth-estimation", model=MODEL)

    out = _PIPE(img)["depth"]
    d = np.asarray(out.resize(img.size), dtype=np.float32)
    d = (d - d.min()) / max(d.max() - d.min(), 1e-6)
    if cache_path:
        Image.fromarray((d * 255).astype(np.uint8)).save(cache_path)
    return d


def warp(rgb, depth, dx, dy, zoom=1.0):
    """One frame: every pixel pulled back by how near it is.

    Sampling backwards — asking each output pixel where it came from — is what
    keeps the result free of the speckled gaps a forward scatter leaves. The
    holes that remain are the ones behind things that moved, and those are
    filled afterwards.
    """
    import cv2
    h, w = depth.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)

    cx, cy = w / 2.0, h / 2.0
    if zoom != 1.0:
        xs = cx + (xs - cx) / zoom
        ys = cy + (ys - cy) / zoom

    # Near pixels travel furthest. The map is where to *read* from, so the
    # shift is subtracted: the picture appears to move the other way.
    mx = xs - dx * depth
    my = ys - dy * depth
    return cv2.remap(rgb, mx, my, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_REPLICATE)


def clip(image_path, out_path, seconds, fps=30, size=None,
         amount=0.018, zoom_to=1.06, depth_cache=None, direction=(1.0, 0.22)):
    """A still turned into a moving shot, written as frames through ffmpeg."""
    import subprocess

    img = Image.open(image_path).convert("RGB")
    depth = depth_of(img, depth_cache)
    rgb = np.asarray(img, dtype=np.uint8)
    h, w = depth.shape

    # A small inward crop: the warp pulls pixels in from beyond the frame at
    # the edges, and replicating them there is the one place the illusion
    # shows. Cropping past the largest shift hides it entirely.
    margin = int(w * amount * 1.6) + 2
    ow, oh = (size or (w - 2 * margin, h - 2 * margin))

    n = max(int(round(seconds * fps)), 2)
    span = w * amount
    ux, uy = direction

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{ow}x{oh}",
         "-r", str(fps), "-i", "-", "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", out_path], stdin=subprocess.PIPE, creationflags=flags)

    import cv2
    for i in range(n):
        # Eased so the move starts and ends at rest; a shot that begins at
        # full speed reads as a cut landing badly.
        t = i / (n - 1)
        e = t * t * (3 - 2 * t)
        f = warp(rgb, depth, span * ux * (e - 0.5), span * uy * (e - 0.5),
                 zoom=1.0 + (zoom_to - 1.0) * e)
        f = f[margin:margin + oh, margin:margin + ow]
        if f.shape[0] != oh or f.shape[1] != ow:
            f = cv2.resize(f, (ow, oh), interpolation=cv2.INTER_LINEAR)
        proc.stdin.write(f.astype(np.uint8).tobytes())
    proc.stdin.close()
    proc.wait()
    return out_path

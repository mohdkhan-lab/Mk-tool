import os
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))


def _fonts_dir():
    """tools/fonts beside this file, or in the install folder: installed, this
    runs from the user's folder while the fonts ship in Program Files, and
    every thumbnail came out in the fallback font."""
    import sys
    for d in [HERE] + [x for x in sys.path if x]:
        cand = os.path.join(d, "tools", "fonts")
        if os.path.isdir(cand):
            return cand
    return os.path.join(HERE, "tools", "fonts")


FONTS_DIR = _fonts_dir()
LUCKIEST_FONT = os.path.join(FONTS_DIR, "LuckiestGuy.ttf")
TITAN_FONT = os.path.join(FONTS_DIR, "TitanOne.ttf")
LILITA_FONT = os.path.join(FONTS_DIR, "LilitaOne.ttf")
BANGBERS_FONT = os.path.join(FONTS_DIR, "Bangers.ttf")

COLOR_THEMES = {
    "yellow_gold": {
        "part1_top": (255, 255, 255, 255),
        "part1_bot": (255, 240, 170, 255),
        "part2_top": (255, 245, 10, 255),
        "part2_bot": (255, 140, 0, 255),
        "inset1": (255, 255, 255, 200),
        "inset2": (255, 255, 180, 200),
    },
    "danger_red": {
        "part1_top": (255, 255, 255, 255),
        "part1_bot": (255, 220, 220, 255),
        "part2_top": (255, 60, 40, 255),
        "part2_bot": (180, 10, 10, 255),
        "inset1": (255, 255, 255, 200),
        "inset2": (255, 170, 170, 200),
    },
    "electric_cyan": {
        "part1_top": (255, 255, 255, 255),
        "part1_bot": (220, 250, 255, 255),
        "part2_top": (0, 240, 255, 255),
        "part2_bot": (0, 140, 220, 255),
        "inset1": (255, 255, 255, 200),
        "inset2": (180, 255, 255, 200),
    },
    "toxic_green": {
        "part1_top": (255, 255, 255, 255),
        "part1_bot": (230, 255, 220, 255),
        "part2_top": (100, 255, 40, 255),
        "part2_bot": (40, 180, 10, 255),
        "inset1": (255, 255, 255, 200),
        "inset2": (200, 255, 180, 200),
    }
}

def create_gradient_mask(w, h, top_color, bottom_color):
    """Creates a vertical linear gradient image."""
    top_c = np.array(top_color, dtype=float)
    bot_c = np.array(bottom_color, dtype=float)
    
    gradient = np.zeros((h, w, 4), dtype=np.uint8)
    for y in range(h):
        factor = y / max(1, h - 1)
        curr = (1.0 - factor) * top_c + factor * bot_c
        gradient[y, :] = curr.astype(np.uint8)
        
    return Image.fromarray(gradient, "RGBA")

def render_3d_text(text: str, font_size=150, font_path=LUCKIEST_FONT, split_words=True, tilt_angle=-3.0, theme="yellow_gold"):
    """
    Renders 3D cartoon typography with:
    - 2-tone gradient styling
    - 3D downward block extrusion
    - Heavy outer black cartoon stroke
    - Dynamic angle tilt
    - Safety auto-scaling to avoid clipping
    """
    if not os.path.exists(font_path):
        font_path = LUCKIEST_FONT
        if not os.path.exists(font_path):
            font_path = "C:/Windows/Fonts/impact.ttf"

    t_cfg = COLOR_THEMES.get(theme, COLOR_THEMES["yellow_gold"])

    words = text.strip().split(" ")
    if len(words) >= 2 and split_words:
        if len(words) == 2:
            part1, part2 = words[0], words[1]
        elif len(words) == 3:
            part1, part2 = words[0], " ".join(words[1:])
        else:
            mid = len(words) // 2
            part1, part2 = " ".join(words[:mid]), " ".join(words[mid:])
    else:
        part1, part2 = text, ""

    # Measure and auto-fit font size so text never exceeds max safe width (1700px on 1920 canvas)
    max_text_width = 1700
    while font_size > 70:
        font = ImageFont.truetype(font_path, font_size)
        dummy = Image.new("RGBA", (10, 10))
        d = ImageDraw.Draw(dummy)
        bbox1 = d.textbbox((0, 0), part1, font=font) if part1 else (0,0,0,0)
        w1, h1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]
        bbox2 = d.textbbox((0, 0), part2, font=font) if part2 else (0,0,0,0)
        w2, h2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
        spacing = int(font_size * 0.22) if part2 else 0
        total_w = w1 + spacing + w2
        if total_w <= max_text_width:
            break
        font_size -= 8

    max_h = max(h1, h2)
    extrusion_depth = max(14, int(font_size * 0.14))
    stroke_w = max(10, int(font_size * 0.10))

    pad = 120
    canvas_w = total_w + pad * 2
    canvas_h = max_h + pad * 2 + extrusion_depth

    txt_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(txt_layer)

    x1 = pad
    y1 = pad
    x2 = x1 + w1 + spacing
    y2 = pad

    # 1. 3D Block Extrusion (Black downward shadow)
    for depth in range(extrusion_depth, 0, -2):
        dy = depth
        for sx in range(-stroke_w, stroke_w + 1, 2):
            for sy in range(-stroke_w, stroke_w + 1, 2):
                if sx*sx + sy*sy <= stroke_w*stroke_w:
                    if part1:
                        draw.text((x1 + sx, y1 + sy + dy), part1, font=font, fill=(0, 0, 0, 255))
                    if part2:
                        draw.text((x2 + sx, y2 + sy + dy), part2, font=font, fill=(0, 0, 0, 255))

    # 2. Main Outer Black Stroke
    for sx in range(-stroke_w, stroke_w + 1):
        for sy in range(-stroke_w, stroke_w + 1):
            if sx*sx + sy*sy <= stroke_w*stroke_w:
                if part1:
                    draw.text((x1 + sx, y1 + sy), part1, font=font, fill=(0, 0, 0, 255))
                if part2:
                    draw.text((x2 + sx, y2 + sy), part2, font=font, fill=(0, 0, 0, 255))

    # 3. Inner Gradient Text Masks
    if part1:
        mask1 = Image.new("L", (canvas_w, canvas_h), 0)
        draw_m1 = ImageDraw.Draw(mask1)
        draw_m1.text((x1, y1), part1, font=font, fill=255)
        grad1 = create_gradient_mask(canvas_w, canvas_h, t_cfg["part1_top"], t_cfg["part1_bot"])
        txt_layer.paste(grad1, (0, 0), mask1)

    if part2:
        mask2 = Image.new("L", (canvas_w, canvas_h), 0)
        draw_m2 = ImageDraw.Draw(mask2)
        draw_m2.text((x2, y2), part2, font=font, fill=255)
        grad2 = create_gradient_mask(canvas_w, canvas_h, t_cfg["part2_top"], t_cfg["part2_bot"])
        txt_layer.paste(grad2, (0, 0), mask2)

    # 4. Subtle Inner Inset Line for 3D tactile pop
    if part1:
        draw.text((x1, y1 - 2), part1, font=font, fill=t_cfg["inset1"])
    if part2:
        draw.text((x2, y2 - 2), part2, font=font, fill=t_cfg["inset2"])

    # Apply dynamic tilt
    if tilt_angle != 0:
        txt_layer = txt_layer.rotate(tilt_angle, resample=Image.Resampling.BICUBIC, expand=True)

    return txt_layer

def enhance_image(base_img: Image.Image) -> Image.Image:
    """Enhances background image with cinematic contrast, vibrancy, sharpness and subtle radial vignette."""
    img = base_img.resize((1920, 1080), Image.Resampling.LANCZOS).convert("RGBA")
    
    # 1. Boost Color & Contrast
    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(1.24)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(1.16)
    enhancer = ImageEnhance.Sharpness(img)
    img = enhancer.enhance(1.22)
    
    # 2. Cinematic Radial Vignette
    w, h = 1920, 1080
    Y, X = np.ogrid[:h, :w]
    cx, cy = w / 2, h / 2
    dist = np.sqrt(((X - cx) / (w / 2)) ** 2 + ((Y - cy) / (h / 2)) ** 2)
    vignette_alpha = np.clip((dist - 0.65) / (1.4 - 0.65), 0, 1) * 115
    
    vig_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vig_alpha_img = Image.fromarray(vignette_alpha.astype(np.uint8), 'L')
    black_fill = Image.new("RGBA", (w, h), (10, 8, 15, 255))
    vig_layer.paste(black_fill, (0, 0), vig_alpha_img)
    
    img = Image.alpha_composite(img, vig_layer)
    return img

def compose_pro_thumbnail(base_img: Image.Image, text: str, font_size=155, tilt=-3.0, theme="yellow_gold", y_offset=30) -> Image.Image:
    """Combines enhanced base image with 3D cartoon text."""
    enhanced_bg = enhance_image(base_img)
    
    # Dynamic default font sizing
    if len(text) > 16:
        font_size = min(font_size, 135)
    if len(text) > 22:
        font_size = min(font_size, 115)
    if len(text) > 28:
        font_size = min(font_size, 98)
        
    txt_layer = render_3d_text(text, font_size=font_size, tilt_angle=tilt, theme=theme)
    
    # Center text horizontally
    tx = max(20, (1920 - txt_layer.width) // 2)
    ty = max(15, y_offset)
    
    final_img = enhanced_bg.copy()
    final_img.paste(txt_layer, (tx, ty), txt_layer)
    
    return final_img.convert("RGB")

def save_thumbnail_pack(final_img: Image.Image, out_dirs: list, option_num: int, is_primary=False):
    """Saves thumbnail in multiple formats and destinations (HD 1920x1080 and YouTube 1280x720)."""
    img_720 = final_img.resize((1280, 720), Image.Resampling.LANCZOS)
    
    for d in out_dirs:
        if not os.path.exists(d):
            continue
        # Save option
        opt_file = os.path.join(d, f"thumbnail_option{option_num}.jpg")
        final_img.save(opt_file, quality=96)
        
        # Save standard thumbnail.jpg if primary
        if is_primary:
            prim_file = os.path.join(d, "thumbnail.jpg")
            final_img.save(prim_file, quality=96)
            # Also save 720p version
            prim_720 = os.path.join(d, "thumbnail_720p.jpg")
            img_720.save(prim_720, quality=96)

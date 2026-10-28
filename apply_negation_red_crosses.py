import os
import sys
import json
import re
from PIL import Image, ImageDraw

sys.stdout.reconfigure(encoding='utf-8')

def draw_bold_cartoon_red_cross(img: Image.Image) -> Image.Image:
    """Draws a bold, comical, thick cartoon red cross (❌) over the image."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    w, h = img.size
    cx, cy = w // 2, h // 2
    size = int(min(w, h) * 0.38)
    thickness = int(size * 0.22)
    
    # Coordinates for the X cross
    x1, y1 = cx - size, cy - size
    x2, y2 = cx + size, cy + size
    x3, y3 = cx - size, cy + size
    x4, y4 = cx + size, cy - size
    
    # 1. Black outer outline for cartoon pop
    outline_extra = 10
    draw.line([(x1, y1), (x2, y2)], fill=(0, 0, 0, 255), width=thickness + outline_extra * 2)
    draw.line([(x3, y3), (x4, y4)], fill=(0, 0, 0, 255), width=thickness + outline_extra * 2)
    
    # 2. Bright vibrant red fill
    draw.line([(x1, y1), (x2, y2)], fill=(235, 25, 35, 255), width=thickness)
    draw.line([(x3, y3), (x4, y4)], fill=(235, 25, 35, 255), width=thickness)
    
    # 3. Highlight stripe on top for 3D cartoon shine
    draw.line([(x1 + 3, y1 - 3), (x2 - 3, y2 - 3)], fill=(255, 100, 110, 255), width=thickness // 4)
    draw.line([(x3 + 3, y3 - 3), (x4 - 3, y4 - 3)], fill=(255, 100, 110, 255), width=thickness // 4)
    
    # Composite overlay on original image
    base = img.convert("RGBA")
    combined = Image.alpha_composite(base, overlay)
    return combined.convert("RGB")

def is_negation_sentence(text: str) -> bool:
    t = text.lower().strip()
    neg_patterns = [
        r"\bthere were no\b",
        r"\bthere was no\b",
        r"\bno\s+[a-z]+",
        r"\bwithout\s+[a-z]+",
        r"\bnot\s+with\s+a\b",
        r"\bnever\s+had\b",
        r"\bnever\s+invented\b",
        r"\bno\s+portrait\b",
        r"\bno\s+diary\b",
        r"\bno\s+name\b",
        r"\bno\s+coffee\b",
        r"\bno\s+espresso\b",
        r"\bno\s+paper\s+cup\b",
        r"\bno\s+menu\b",
    ]
    for pat in neg_patterns:
        if re.search(pat, t):
            # Avoid false positives like "no wonder" or "no one knows"
            if not re.search(r"\b(no wonder|no one|no idea|no matter|in no time)\b", t):
                return True
    return False

def stamp_project_negations(pid: str):
    """Universal stamper for any stickfilm project before render."""
    pdir = os.path.join(r"g:\stickfilm\vidgen\projects", pid)
    seg_file = os.path.join(pdir, "segments.json")
    imgd = os.path.join(pdir, "images")
    
    if not os.path.isfile(seg_file) or not os.path.isdir(imgd):
        return

    with open(seg_file, "r", encoding="utf-8") as f:
        segments = json.load(f)

    stamped_count = 0
    for idx, seg in enumerate(segments):
        if is_negation_sentence(seg["text"]):
            # Try both 1-indexed format and raw name
            candidates = [f"{idx+1:03d}.png", f"{idx:03d}.png"]
            for img_name in candidates:
                img_path = os.path.join(imgd, img_name)
                if os.path.isfile(img_path):
                    try:
                        img = Image.open(img_path)
                        stamped = draw_bold_cartoon_red_cross(img)
                        stamped.save(img_path, quality=98)
                        stamped_count += 1
                        print(f"[AUTO-NEGATION STAMP] '{seg['text']}' -> {img_name}")
                    except Exception as e:
                        print(f"Failed stamping {img_name}: {e}")
                    break

    if stamped_count > 0:
        print(f"[render] Stamped bold red crosses on {stamped_count} negation shots!")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        stamp_project_negations(sys.argv[1])
    else:
        stamp_project_negations("12_WHO_DRANK_THE_FIRST_CUP_OF_COFFEE")

import os
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUTPUT_DIR = r"C:\Users\mohda\.gemini\antigravity-ide\scratch\Mk-tool\logo_samples"
os.makedirs(OUTPUT_DIR, exist_ok=True)

SIZE = 1024
RADIUS = 220

def create_squircle_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size, size], radius=radius, fill=255)
    return mask

def create_gradient_canvas(size, color1, color2, angle_deg=45):
    img = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(img)
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    
    for y in range(size):
        for x in range(size):
            t = ((x / size) * cos_a + (y / size) * sin_a)
            t = max(0.0, min(1.0, (t + 0.5) / 1.5))
            r = int(color1[0] * (1 - t) + color2[0] * t)
            g = int(color1[1] * (1 - t) + color2[1] * t)
            b = int(color1[2] * (1 - t) + color2[2] * t)
            img.putpixel((x, y), (r, g, b, 255))
    return img

def render_concept_1():
    # CONCEPT 1: Cyber Neon (Electric Cyan -> Royal Purple) with Play Arrow
    canvas = Image.new("RGBA", (SIZE, SIZE), (13, 17, 28, 255)) # Dark Obsidian
    draw = ImageDraw.Draw(canvas)
    
    # Ambient radial glow in center
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse([200, 200, 824, 824], fill=(79, 172, 254, 70))
    glow_draw.ellipse([300, 300, 724, 724], fill=(127, 0, 255, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    canvas.alpha_composite(glow)
    
    # Inner border line
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([20, 20, SIZE-20, SIZE-20], radius=RADIUS-10, outline=(255, 255, 255, 30), width=4)
    
    # Load bold font
    font_path = r"C:\Windows\Fonts\segoeuib.ttf"
    if not os.path.exists(font_path):
        font_path = r"C:\Windows\Fonts\arialbd.ttf"
    font = ImageFont.truetype(font_path, 400)
    font_sub = ImageFont.truetype(font_path, 80)
    
    # Shadow layer
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.text((260, 220), "MK", font=font, fill=(0, 0, 0, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    canvas.alpha_composite(shadow)
    
    # Gradient text mask
    text_mask = Image.new("L", (SIZE, SIZE), 0)
    tdraw = ImageDraw.Draw(text_mask)
    tdraw.text((250, 210), "MK", font=font, fill=255)
    
    # Draw Play triangle in K
    tdraw.polygon([(780, 410), (780, 570), (920, 490)], fill=255)
    
    grad = create_gradient_canvas(SIZE, (0, 242, 254), (170, 70, 255), angle_deg=35)
    canvas.paste(grad, (0, 0), text_mask)
    
    # Subtitle: STORY STUDIO
    draw = ImageDraw.Draw(canvas)
    sub_text = "STORYSTUDIO"
    bbox = draw.textbbox((0, 0), sub_text, font=font_sub)
    w = bbox[2] - bbox[0]
    draw.text(((SIZE - w) // 2, 720), sub_text, font=font_sub, fill=(200, 220, 255, 220))
    
    # Accent dot
    draw.ellipse([495, 830, 525, 860], fill=(0, 242, 254, 255))
    
    # Apply squircle mask
    mask = create_squircle_mask(SIZE, RADIUS)
    final_img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    final_img.paste(canvas, (0, 0), mask)
    
    out_path = os.path.join(OUTPUT_DIR, "MK_StoryStudio_Concept_1_CyberNeon.png")
    final_img.save(out_path, "PNG")
    
    # Save as ICO
    ico_path = os.path.join(OUTPUT_DIR, "MK_StoryStudio_Concept_1.ico")
    final_img.save(ico_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"Generated: {out_path}")

def render_concept_2():
    # CONCEPT 2: Sunset Forge (Vibrant Warm Sunset -> Violet Gradient)
    canvas = Image.new("RGBA", (SIZE, SIZE), (15, 12, 25, 255))
    draw = ImageDraw.Draw(canvas)
    
    # Ambient warm glow
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse([200, 200, 824, 824], fill=(255, 81, 47, 80))
    glow_draw.ellipse([300, 300, 724, 724], fill=(221, 36, 118, 100))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    canvas.alpha_composite(glow)
    
    draw.rounded_rectangle([20, 20, SIZE-20, SIZE-20], radius=RADIUS-10, outline=(255, 255, 255, 30), width=4)
    
    font_path = r"C:\Windows\Fonts\segoeuib.ttf"
    if not os.path.exists(font_path):
        font_path = r"C:\Windows\Fonts\arialbd.ttf"
    font = ImageFont.truetype(font_path, 400)
    font_sub = ImageFont.truetype(font_path, 80)
    
    # Shadow
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.text((260, 220), "MK", font=font, fill=(0, 0, 0, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    canvas.alpha_composite(shadow)
    
    text_mask = Image.new("L", (SIZE, SIZE), 0)
    tdraw = ImageDraw.Draw(text_mask)
    tdraw.text((250, 210), "MK", font=font, fill=255)
    
    # Play spark
    tdraw.polygon([(780, 410), (780, 570), (920, 490)], fill=255)
    
    grad = create_gradient_canvas(SIZE, (255, 81, 47), (221, 36, 118), angle_deg=45)
    canvas.paste(grad, (0, 0), text_mask)
    
    draw = ImageDraw.Draw(canvas)
    sub_text = "STORYSTUDIO"
    bbox = draw.textbbox((0, 0), sub_text, font=font_sub)
    w = bbox[2] - bbox[0]
    draw.text(((SIZE - w) // 2, 720), sub_text, font=font_sub, fill=(255, 210, 225, 230))
    draw.ellipse([495, 830, 525, 860], fill=(255, 81, 47, 255))
    
    mask = create_squircle_mask(SIZE, RADIUS)
    final_img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    final_img.paste(canvas, (0, 0), mask)
    
    out_path = os.path.join(OUTPUT_DIR, "MK_StoryStudio_Concept_2_SunsetForge.png")
    final_img.save(out_path, "PNG")
    ico_path = os.path.join(OUTPUT_DIR, "MK_StoryStudio_Concept_2.ico")
    final_img.save(ico_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"Generated: {out_path}")

def render_concept_3():
    # CONCEPT 3: Emerald Tech (Mint Emerald -> Electric Cyan) Modern Minimalist
    canvas = Image.new("RGBA", (SIZE, SIZE), (10, 20, 22, 255))
    draw = ImageDraw.Draw(canvas)
    
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse([200, 200, 824, 824], fill=(0, 245, 160, 70))
    glow_draw.ellipse([300, 300, 724, 724], fill=(0, 217, 245, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    canvas.alpha_composite(glow)
    
    draw.rounded_rectangle([20, 20, SIZE-20, SIZE-20], radius=RADIUS-10, outline=(255, 255, 255, 30), width=4)
    
    font_path = r"C:\Windows\Fonts\segoeuib.ttf"
    if not os.path.exists(font_path):
        font_path = r"C:\Windows\Fonts\arialbd.ttf"
    font = ImageFont.truetype(font_path, 400)
    font_sub = ImageFont.truetype(font_path, 80)
    
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.text((260, 220), "MK", font=font, fill=(0, 0, 0, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    canvas.alpha_composite(shadow)
    
    text_mask = Image.new("L", (SIZE, SIZE), 0)
    tdraw = ImageDraw.Draw(text_mask)
    tdraw.text((250, 210), "MK", font=font, fill=255)
    tdraw.polygon([(780, 410), (780, 570), (920, 490)], fill=255)
    
    grad = create_gradient_canvas(SIZE, (0, 245, 160), (0, 217, 245), angle_deg=35)
    canvas.paste(grad, (0, 0), text_mask)
    
    draw = ImageDraw.Draw(canvas)
    sub_text = "STORYSTUDIO"
    bbox = draw.textbbox((0, 0), sub_text, font=font_sub)
    w = bbox[2] - bbox[0]
    draw.text(((SIZE - w) // 2, 720), sub_text, font=font_sub, fill=(200, 255, 240, 230))
    draw.ellipse([495, 830, 525, 860], fill=(0, 245, 160, 255))
    
    mask = create_squircle_mask(SIZE, RADIUS)
    final_img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    final_img.paste(canvas, (0, 0), mask)
    
    out_path = os.path.join(OUTPUT_DIR, "MK_StoryStudio_Concept_3_EmeraldTech.png")
    final_img.save(out_path, "PNG")
    ico_path = os.path.join(OUTPUT_DIR, "MK_StoryStudio_Concept_3.ico")
    final_img.save(ico_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"Generated: {out_path}")

if __name__ == "__main__":
    render_concept_1()
    render_concept_2()
    render_concept_3()
    print("ALL LOGOS GENERATED SUCCESSFULLY!")


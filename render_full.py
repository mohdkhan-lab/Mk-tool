"""
STICKFILM - RENDER FINAL MASTER VIDEO & DELIVERABLES
Renders all 1080p shots, stitches with voiceover, stamps negations,
copies to D:\\Video Projects, and creates viral thumbnails + metadata.
"""

import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
VIDGEN_DIR = os.path.join(HERE, "vidgen")
PROJECTS_DIR = os.environ.get("STICKFILM_PROJECTS") or os.path.join(VIDGEN_DIR, "projects")
PUB_BASE = r"D:\Video Projects"

sys.path.insert(0, HERE)
sys.path.insert(0, VIDGEN_DIR)

from apply_negation_red_crosses import stamp_project_negations
from make_video import generate_viral_thumbnails, create_upload_txt

def render_project(pid):
    pdir = os.path.join(PROJECTS_DIR, pid)
    if not os.path.isdir(pdir):
        sys.exit(f"Error: Project folder '{pdir}' not found!")

    print(f"\n=======================================================")
    print(f"🎬 RENDERING 1080p FULL HD VIDEO FOR: {pid}")
    print(f"=======================================================\n")

    # 1. Apply Automatic Negation Red Cross (❌) Stamps
    print("--- STEP 5: APPLYING AUTOMATIC NEGATION RED CROSS (❌) STAMPS ---")
    stamp_project_negations(pid)

    # 2. Zero-Blank Image Verification
    print("\n--- STEP 6: VERIFYING ALL IMAGES (Zero-Blank Policy) ---")
    img_dir = os.path.join(pdir, "images")
    if not os.path.isdir(img_dir):
        sys.exit(f"❌ Error: Images directory '{img_dir}' missing!")

    imgs = [f for f in os.listdir(img_dir) if f.lower().endswith((".png", ".jpg", ".webp"))]
    blanks = [f for f in imgs if os.path.getsize(os.path.join(img_dir, f)) in (39370,) or os.path.getsize(os.path.join(img_dir, f)) < 15000]
    print(f"[VERIFY] Total shots: {len(imgs)} | Blank frames: {len(blanks)}")
    if blanks:
        sys.exit(f"❌ Error: {len(blanks)} blank images found! Aborting render.")
    print("✅ All frames verified clean! Proceeding to 1080p render.\n")

    # 3. 1080p Master Video Render
    print("--- STEP 7: RENDERING 1080p MASTER VIDEO ---")
    cmd = [sys.executable, os.path.join(VIDGEN_DIR, "pipeline.py"), "render", pid, "--jobs", "8"]
    print(f"[RUN] {' '.join(cmd)}", flush=True)
    # Relayed, not inherited: under pythonw a child's output goes nowhere.
    from make_video import relay
    code = relay(cmd)
    if code != 0:
        raise subprocess.CalledProcessError(code, cmd)

    # 4. Copy to Deliverables Folder
    pub_dir = os.path.join(PUB_BASE, pid)
    os.makedirs(pub_dir, exist_ok=True)
    src_mp4 = os.path.join(pdir, "final.mp4")
    if not os.path.isfile(src_mp4):
        sys.exit(f"❌ Error: Expected output '{src_mp4}' was not generated!")

    dest_mp4 = os.path.join(pub_dir, f"{pid}.mp4")
    # Side name, then one rename: the Deliverables page must never list a
    # copy that is still being written.
    shutil.copyfile(src_mp4, dest_mp4 + ".part")
    os.replace(dest_mp4 + ".part", dest_mp4)
    sz_mb = os.path.getsize(dest_mp4) / (1024 * 1024)
    print(f"\n[MASTER VIDEO] Successfully published to: {dest_mp4} ({sz_mb:.1f} MB)")

    # 5. Viral Thumbnails & Upload Metadata
    print("\n--- STEP 8: GENERATING VIRAL THUMBNAILS & UPLOAD METADATA ---")
    generate_viral_thumbnails(pid, pid.replace("_", " "), pub_dir)

    script_path = os.path.join(pdir, "script.txt")
    stext = ""
    if os.path.isfile(script_path):
        with open(script_path, "r", encoding="utf-8") as sf:
            stext = sf.read()
    create_upload_txt(pid, stext, pub_dir)

    print("\n=======================================================")
    print(f"🎉 PRODUCTION COMPLETE! DELIVERABLES READY IN:")
    print(f"👉 {pub_dir}")
    print("=======================================================\n")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python render_full.py <project_id>")
    render_project(sys.argv[1])

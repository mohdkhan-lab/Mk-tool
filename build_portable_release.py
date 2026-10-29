"""
Mk-Tool Standalone Portable Package Builder
===========================================
This script creates a 100% standalone, portable folder and ZIP for Mk-Tool (Frameloom).
The resulting package contains:
  1. Mk-Tool.exe (Native Windows GUI launcher - zero terminal)
  2. runtime/ (Embedded Python 3.14 + Playwright browsers + faster-whisper)
  3. app/ (Source code, vidgen, bulkgen, tools with ffmpeg, assets)
Any user on any Windows 10/11 64-bit PC can simply unzip and double-click Mk-Tool.exe!
"""

import os
import shutil
import sys
import zipfile

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALLED_BASE = r"C:\Users\mohda\AppData\Local\Programs\Frameloom"
OUTPUT_DIR = os.path.join(os.path.dirname(ROOT_DIR), "Mk-Tool-Portable-v1.0.0")

def build_package():
    print("=" * 60)
    print("       MK-TOOL STANDALONE PORTABLE PACKAGER")
    print("=" * 60)
    print(f"Target Output: {OUTPUT_DIR}")
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # 1. Copy Mk-Tool.exe and Update-App.bat
    exe_src = os.path.join(ROOT_DIR, "Mk-Tool.exe")
    if not os.path.isfile(exe_src):
        print("[ERROR] Mk-Tool.exe not found! Please compile launcher.cs first.")
        return 1
    
    print("[1/5] Copying Mk-Tool.exe and Update-App.bat...")
    shutil.copy2(exe_src, os.path.join(OUTPUT_DIR, "Mk-Tool.exe"))
    updater_src = os.path.join(ROOT_DIR, "Update-App.bat")
    if os.path.isfile(updater_src):
        shutil.copy2(updater_src, os.path.join(OUTPUT_DIR, "Update-App.bat"))
    
    # 2. Setup app/ folder
    app_target = os.path.join(OUTPUT_DIR, "app")
    print(f"[2/5] Assembling app/ folder at {app_target}...")
    if os.path.exists(app_target):
        shutil.rmtree(app_target)
    os.makedirs(app_target, exist_ok=True)
    
    # Copy repo files into app/
    ignore_patterns = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".server_session.json", "browser_profiles")
    for item in os.listdir(ROOT_DIR):
        if item in (".git", "Mk-Tool.exe", "launcher.cs", "build_portable_release.py"):
            continue
        s = os.path.join(ROOT_DIR, item)
        d = os.path.join(app_target, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=ignore_patterns, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
            
    # Also ensure tools/ (ffmpeg, fonts) are present from installed copy
    tools_src = os.path.join(INSTALLED_BASE, "app", "tools")
    tools_target = os.path.join(app_target, "tools")
    if os.path.isdir(tools_src) and not os.path.isdir(tools_target):
        print("  - Copying bundled tools (ffmpeg, fonts)...")
        shutil.copytree(tools_src, tools_target, dirs_exist_ok=True)
        
    # Ensure refs/ (sample characters) are present
    refs_src = os.path.join(INSTALLED_BASE, "app", "refs")
    refs_target = os.path.join(app_target, "refs")
    if os.path.isdir(refs_src) and not os.path.isdir(refs_target):
        print("  - Copying character refs...")
        shutil.copytree(refs_src, refs_target, dirs_exist_ok=True)

    # 3. Copy runtime/
    runtime_src = os.path.join(INSTALLED_BASE, "runtime")
    runtime_target = os.path.join(OUTPUT_DIR, "runtime")
    if not os.path.exists(runtime_target):
        print(f"[3/5] Copying embedded Python & Playwright runtime (~1 GB)...")
        shutil.copytree(runtime_src, runtime_target, dirs_exist_ok=True)
    else:
        print("[3/5] runtime/ already exists in target, skipping copy.")
        
    # 4. Write README.txt
    readme_content = """===============================================================
    MK-TOOL (FRAMELOOM) — STANDALONE AI VIDEO STUDIO
===============================================================

HOW TO RUN:
  1. Simply double-click "Mk-Tool.exe".
  2. The AI Studio Desktop App will launch automatically on your screen!
  3. No command prompt, no terminal, and no Python setup required.

FIRST-TIME SETUP (OPTIONAL):
  - Go to the API Keys section in the Studio to add your Gemini API Key.
  - Choose your narrators, character poses, write or generate scripts,
    and render high-retention 9:16 Shorts or 16:9 Reels directly.

PORTABLE:
  - You can move this entire folder to any Windows 10/11 64-bit PC.
"""
    with open(os.path.join(OUTPUT_DIR, "README.txt"), "w", encoding="utf-8") as f:
        f.write(readme_content)
        
    # 5. Create ZIP Archive
    zip_output = os.path.join(os.path.dirname(ROOT_DIR), "Mk-Tool-v1.0.0-Windows-x64.zip")
    print(f"\n[5/5] Compressing portable package into ZIP: {zip_output}...")
    with zipfile.ZipFile(zip_output, "w", zipfile.ZIP_DEFLATED) as zf:
        for foldername, subfolders, filenames in os.walk(OUTPUT_DIR):
            for filename in filenames:
                filepath = os.path.join(foldername, filename)
                arcname = os.path.relpath(filepath, os.path.dirname(OUTPUT_DIR))
                zf.write(filepath, arcname)

    print("\n[SUCCESS] Standalone Portable Package & ZIP created successfully!")
    print(f"Folder: {OUTPUT_DIR}")
    print(f"ZIP File: {zip_output}")
    print("\nYou can now upload this ZIP to GitHub Releases or double-click Mk-Tool.exe to run!")
    return 0

if __name__ == "__main__":
    sys.exit(build_package())

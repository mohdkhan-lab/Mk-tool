# StickFilm Studio — Complete Architecture & Operational Guide

## 1. What is StickFilm Studio?
StickFilm Studio is an automated, AI-assisted video creation studio specifically engineered for **viral stickman storytelling, YouTube Shorts, and Reels**. 
It handles the entire pipeline:
1. Scriptwriting & Scene Planning
2. AI Voiceover & Timestamped Word Alignments (Auto Subtitles)
3. Visuals / Animations & Canvas generation
4. Sound FX & Zoom Motion (Parallax)
5. FFmpeg Stitching & Final MP4 Rendering
6. Thumbnail Generation
7. Local Wi-Fi transfer to phone

---

## 2. Directory Structure

```
stickfilm_backup/
├── start_studio.bat         # 1-Click standalone runner (no licensing gate)
├── requirements.txt         # All Python library dependencies
├── tools/                   # Pre-compiled binaries
│   ├── ffmpeg.exe           # High-speed video encoder
│   ├── ffprobe.exe          # Media analyzer
│   └── fonts/               # Subtitle & header typography
├── bulkgen/                 # Script, Prompt, Voice & Character Engine
│   ├── bulkgem.py           # Gemini/Claude bulk script engine
│   ├── promptgen.py         # AI image/video prompt synthesizer
│   ├── voice.py             # TTS / ElevenLabs / Whisper integration
│   ├── char_sheet.png       # Stickman character sprite references
│   └── clean_stickman_ref.png
├── vidgen/                  # Main Studio Dashboard & Video Pipeline
│   ├── dashboard.py         # 3,900+ lines Web API server
│   ├── dash.html            # Studio Web UI (Vue/vanilla SPA)
│   ├── pipeline.py          # Master orchestrator
│   ├── assemble_clips.py    # FFmpeg multi-layer video compositor
│   ├── cutout.py            # Character background segmentation
│   ├── parallax.py          # 2.5D camera pan & zoom engine
│   ├── share_server.py      # Wi-Fi file sender (port 10246)
│   └── pro_thumbnail_engine.py
├── ai_avatar_pipeline/      # AI avatar generator scripts
└── make_video.py            # Standalone direct video builder
```

---

## 3. How the Pipeline Actually Works (Stage by Stage)

### Stage 1: Scripting & Topic Decomposition
- Handled by `bulkgen/bulkgem.py`.
- Uses Google Gemini API (`google-generativeai`) with custom system prompts tailored for punchy, high-retention narration.
- Takes a topic (e.g., "Why 99% of people stay broke") and splits it into:
  - Narration text (Hook -> Story -> Climax -> Call to Action).
  - Scene visual prompts (which stickman pose, background color, expressions).

### Stage 2: Audio Synthesis & Word-Level Alignment
- Handled by `bulkgen/voice.py`.
- Generates speech audio via Text-to-Speech engines.
- Feeds the resulting audio into `faster-whisper`.
- `faster-whisper` outputs exact millisecond timestamps for every spoken word.
- These timestamps dictate the exact video frame where text pops up and camera zooms trigger.

### Stage 3: Visual Generation & Stickman Posing
- Uses base references (`char_sheet.png`, `stick_modern.png`, `clean_stickman_ref.png`).
- Uses Playwright headless Chromium or image generation APIs to construct the visual assets.
- `cutout.py` strips unwanted backgrounds, leaving clean alpha-transparent stick characters.

### Stage 4: Motion & Effects (Parallax)
- Handled by `vidgen/parallax.py` and `apply_negation_red_crosses.py`.
- Moves the foreground character and background at different speeds to create a dynamic 3D depth feeling (parallax).
- Adds popular memes / graphic accents like red negation crosses, sound hit markers, and dynamic shake.

### Stage 5: Final Render & Assembly
- Handled by `vidgen/assemble_clips.py` and `make_video.py`.
- Directly executes bundled `tools/ffmpeg.exe`.
- Combines:
  1. Base background track
  2. Character motion layer
  3. Animated subtitle overlays (synced to Whisper timestamps)
  4. Background music & sound effects (pops, wooshes, bells)
- Outputs a ready-to-upload `1080x1920` (9:16 vertical) or `1920x1080` (16:9) `.mp4`.

### Stage 6: Direct Phone Transfer
- `vidgen/share_server.py` hosts a local HTTP server on port `10246`.
- Generates a local QR code on the dashboard. Scanning it with a mobile phone downloads the rendered `.mp4` instantly over local Wi-Fi without cables or cloud uploads.

---

## 4. How to Run It Anytime

Double-click `start_studio.bat` inside this folder, or run:
```powershell
.\start_studio.bat
```
It immediately launches the full studio dashboard on **`http://localhost:5154`**!

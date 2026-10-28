# mk — Frameloom App Backup (Private)

Private backup of the Frameloom desktop app's source code, kept so future
updates/edits can be tracked cleanly in Git instead of by loose zip files.

## What is in this repo

- `vidgen/` — main studio dashboard, web UI (`dash.html`), video pipeline
- `bulkgen/` — script, prompt, voice and character engine
- `ai_avatar_pipeline/` — AI avatar generator scripts
- `launch_desktop_app.py` — desktop app launcher (starts the server, opens the app window)
- `requirements.txt` — Python dependencies
- `HOW_IT_WORKS.md` — architecture and pipeline guide (from the app itself)

## What is NOT in this repo (comes from the installed copy)

- `Frameloom.exe` — the small .NET launcher (binary, lives next to `app/`)
- `runtime/` — bundled Python 3.14 + Playwright browsers (~1.1 GB)
- `app/tools/` — ffmpeg/ffprobe binaries and fonts
- `app/refs/`, sample video/thumbnail, `vidgen/projects/` — project data
- Browser profiles (`ai_avatar_pipeline/browser_profiles/`), API keys,
  cloned voices, session files — personal data, never committed

## How to run from this backup (on your Windows PC)

1. Keep your existing installed folder (`AppData\Local\Programs\Frameloom`).
2. Copy the files from this repo over the installed `app/` folder
   (same structure: `vidgen/`, `bulkgen/`, `ai_avatar_pipeline/`, …).
3. Double-click `Frameloom.exe` as usual. No rebuild needed — the app is
   plain Python; editing a `.py` file and relaunching applies the change.

## How to build a standalone package yourself (on Windows)

The repo holds source only — the runnable standalone is assembled on a
Windows PC from an installed copy:

1. Start from a full installed folder: `Frameloom.exe` + `app/` +
   `runtime/` together. This layout must stay together — the launcher
   finds `app/` and `runtime/` next to itself.
2. Copy this repo's files over the installed `app/` folder so it has the
   latest code.
3. Delete these before zipping (personal/transient data, never ship them):
   - `app/ai_avatar_pipeline/browser_profiles/` (Google/Gemini session cookies)
   - `app/.server_session.json` (port/pid of the last run)
   - `app/my_cloned_voices.json` (unless you intentionally want your cloned
     voices in the package)
4. Zip the three items together: `Frameloom.exe`, `app/`, `runtime/`
   (plus `README.txt` if you want). That zip is the portable standalone —
   on any Windows PC, extract it anywhere and double-click `Frameloom.exe`.
5. First run on a new PC: paste your own Gemini + AI33 keys in the app's
   API Keys panel and sign in to the Chrome profiles (acc1..acc6) — a
   fresh package ships clean, with no keys or logins inside.

Note: `runtime/` (~1.1 GB) and `Frameloom.exe` are NOT in this repo
(binaries cannot be pushed here) — they always come from an installed copy.

## How updates work

1. Edit here on GitHub (or locally and push).
2. On the PC, pull the latest code and copy it over the installed `app/` folder.
3. Relaunch `Frameloom.exe` and check the change.

Keep this repository PRIVATE — it is a backup of a purchased tool, not a
public release.

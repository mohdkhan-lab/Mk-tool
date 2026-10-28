"""
Frameloom (By TJ X Shoaib X Soni) - Isolated Windows Desktop App Launcher
=========================================================================
This script launches Frameloom as a native Windows Desktop Application
with 100% server and profile isolation:
1. Dynamic Free Port: Automatically detects if port 5154 is busy (used by any other tool),
   and binds to a guaranteed free, isolated port (e.g. 5155-5250) so servers NEVER collide.
2. Server Handshake: Verifies that the StickFilm server is fully healthy and responding
   before launching the app interface.
3. Windows Desktop App Mode: Uses Chrome/Edge/Brave in standalone window mode (--app)
   with an isolated user data profile directory to prevent cookies, cache, or extensions
   from clashing with any regular browser sessions or other web tools.
4. Clean Lifecycle: When the Desktop App window is closed, the backend server process
   is terminated cleanly without leaving orphan background servers.
"""

import os
import sys
import time
import json
import socket
import shutil
import urllib.request
import urllib.error
import subprocess

# Ensure paths
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
VIDGEN_DIR = os.path.join(ROOT_DIR, "vidgen")
SESSION_FILE = os.path.join(ROOT_DIR, ".server_session.json")

# Add paths to environment
os.environ["STICKFILM_INSTALL"] = ROOT_DIR
os.environ["PYTHONPATH"] = f"{ROOT_DIR};{VIDGEN_DIR};{os.path.join(ROOT_DIR, 'bulkgen')};{os.path.join(ROOT_DIR, 'ai_avatar_pipeline')};" + os.environ.get("PYTHONPATH", "")
# A packaged install ships its own Playwright browsers next to the runtime, so
# image generation works on a PC that never ran "playwright install".
_pw = os.path.join(os.path.dirname(ROOT_DIR), "runtime", "ms-playwright")
if os.path.isdir(_pw):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _pw)

tools_dir = os.path.join(ROOT_DIR, "tools")
if os.path.isdir(tools_dir) and tools_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = tools_dir + os.pathsep + os.environ.get("PATH", "")


def is_port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is in use on localhost."""
    # Try connecting
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except (OSError, socket.timeout):
            pass

    # Try binding to be certain
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return False
        except OSError:
            return True


def verify_stickfilm_server(port: int) -> bool:
    """Verify if the server on `port` is actually an already running StickFilm Studio instance."""
    url = f"http://127.0.0.1:{port}/api/version"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "StickFilmLauncher/1.0"})
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(data, dict) and ("version" in data or "payload_build" in data):
                    return True
    except Exception:
        pass
    return False


def find_isolated_port(preferred_port: int = 5154) -> tuple[int, bool]:
    """
    Finds a guaranteed isolated port.
    Returns: (port, is_already_our_running_instance)
    """
    # 1. Check if preferred port is already running StickFilm Studio
    if verify_stickfilm_server(preferred_port):
        return preferred_port, True

    # 2. If preferred port is completely free, use it
    if not is_port_busy(preferred_port):
        return preferred_port, False

    # 3. Port 5154 is occupied by another tool or process! Find next free port
    print(f"[ISOLATION] Port {preferred_port} dusre tool ya program dwara busy hai.")
    print("[ISOLATION] Server collision rokne ke liye naya isolated port scan kar rahe hain...")

    for cand_port in range(5155, 5299):
        if verify_stickfilm_server(cand_port):
            return cand_port, True
        if not is_port_busy(cand_port):
            return cand_port, False

    # 4. Fallback to OS assigned free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        ephemeral = s.getsockname()[1]
        return ephemeral, False


def find_browser_executable() -> str | None:
    """Detect available Chromium browsers for Desktop App Mode."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    prog_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    prog_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

    candidates = [
        # Google Chrome
        os.path.join(prog_files, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(prog_files_x86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe"),
        # Microsoft Edge
        os.path.join(prog_files_x86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(prog_files, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(local_app_data, r"Microsoft\Edge\Application\msedge.exe"),
        # Brave Browser
        os.path.join(prog_files, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.join(prog_files_x86, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.join(local_app_data, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]

    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand

    # Check in PATH
    for name in ["chrome.exe", "msedge.exe", "brave.exe"]:
        path = shutil.which(name)
        if path and os.path.isfile(path):
            return path

    # Last resort: the Chromium bundled with a packaged install. An installed
    # browser is always preferred above (Google's sign-in accepts those), so
    # this only kicks in on a PC with no Chromium browser at all.
    return bundled_chromium()


def bundled_chromium() -> str | None:
    """chrome.exe from the Chromium shipped next to a packaged install, if any."""
    base = os.path.join(os.path.dirname(ROOT_DIR), "runtime", "ms-playwright")
    if not os.path.isdir(base):
        return None
    for entry in sorted(os.listdir(base), reverse=True):
        if not entry.startswith("chromium-"):
            continue
        for sub in ("chrome-win64", "chrome-win"):
            exe = os.path.join(base, entry, sub, "chrome.exe")
            if os.path.isfile(exe):
                return exe
    return None


def wait_for_server(port: int, timeout_sec: float = 18.0) -> bool:
    """Poll the server until it's ready to accept connections."""
    start_time = time.time()
    url = f"http://127.0.0.1:{port}/api/version"
    while time.time() - start_time < timeout_sec:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "StickFilmLauncher/1.0"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    print("=" * 70)
    print("        FRAMELOOM - BY TJ X SHOAIB X SONI - DESKTOP APP RUNNER")
    print("=" * 70)
    print()

    # Determine isolated port
    port, already_running = find_isolated_port(5154)
    app_url = f"http://127.0.0.1:{port}"

    server_proc = None

    if already_running:
        print(f"[OK] Frameloom server pehle se hi port {port} par chal raha hai.")
    else:
        print(f"[START] Starting isolated backend server on port: {port}...")
        py_exe = sys.executable
        dash_script = os.path.join(VIDGEN_DIR, "dashboard.py")
        cmd = [py_exe, dash_script, "--port", str(port)]

        server_proc = subprocess.Popen(
            cmd,
            cwd=ROOT_DIR,
            env=os.environ.copy()
        )

        # Save session file
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump({"port": port, "pid": server_proc.pid, "url": app_url}, f)
        except Exception:
            pass

        print("[WAIT] Server ke ready hone ka intezar ho raha hai...")
        if not wait_for_server(port, timeout_sec=20.0):
            print(f"[ERROR] Server port {port} par samay par start nahi hua.")
            if server_proc:
                server_proc.terminate()
            input("\nPress Enter to exit...")
            return 1

        print(f"[OK] Server successfully started and verified on {app_url}!")

    # Find Desktop App browser
    browser_exe = find_browser_executable()
    profile_dir = os.path.join(os.environ.get("LOCALAPPDATA", ROOT_DIR), "StickFilmStudio_AppProfile")
    os.makedirs(profile_dir, exist_ok=True)

    try:
        if browser_exe:
            browser_name = os.path.basename(browser_exe)
            print(f"[APP MODE] Opening as Windows Desktop App using {browser_name}...")
            print(f"[ISOLATION] Profile Directory: {profile_dir}")
            print(f"[ISOLATION] Dedicated URL: {app_url}")
            print()
            print("=" * 70)
            print("  Windows Desktop App khul chuki hai!")
            print("  App ko band karne par backend server bhi clean ho jayega.")
            print("=" * 70)

            app_cmd = [
                browser_exe,
                f"--app={app_url}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-background-networking",
                "--window-size=1400,900"
            ]

            browser_proc = subprocess.Popen(app_cmd)
            browser_proc.wait()
            print("\n[INFO] Desktop App window band kar di gayi hai.")
        else:
            print("[NOTICE] Chromium browser nahi mila, default browser me open kar rahe hain...")
            import webbrowser
            webbrowser.open(app_url)
            print("=" * 70)
            print(f"  Tool open: {app_url}")
            print("  Server band karne ke liye is window me Ctrl+C dabayein.")
            print("=" * 70)
            if server_proc:
                server_proc.wait()

    except KeyboardInterrupt:
        print("\n[STOP] Stop signal prapt hua...")
    finally:
        if server_proc and server_proc.poll() is None:
            print("[CLEANUP] Backend server ko surakshit band kiya ja raha hai...")
            try:
                server_proc.terminate()
                server_proc.wait(timeout=3.0)
            except Exception:
                try:
                    server_proc.kill()
                except Exception:
                    pass
            print("[CLEANUP] Server successfully stopped. Port released.")

        if os.path.exists(SESSION_FILE):
            try:
                os.remove(SESSION_FILE)
            except Exception:
                pass

    print("[DONE] Alvida!")
    return 0


if __name__ == "__main__":
    sys.exit(main())

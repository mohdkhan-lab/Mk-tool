"""
Video Projects Local Wi-Fi Sharing Server
Allows any PC, laptop, phone or tablet on the same Wi-Fi to browse, stream, and download Video Projects.
Includes "Ask where to save" folder guidance and native File System Access integration.
"""
import os
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import io
import zipfile
import urllib.parse
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

_DEFAULT_PUB = os.path.join(os.path.expanduser("~"), "StickFilm")
BASE_DIR = os.environ.get("STICKFILM_PUB_BASE") or _DEFAULT_PUB
PORT = 10246

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.1.9"

def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def get_projects_data():
    projects = []
    if not os.path.exists(BASE_DIR):
        return projects
    
    for item in sorted(os.listdir(BASE_DIR)):
        item_path = os.path.join(BASE_DIR, item)
        if not os.path.isdir(item_path):
            continue
        
        files = os.listdir(item_path)
        video_file = None
        video_size = 0
        for f in files:
            if f.lower().endswith((".mp4", ".mov", ".mkv")):
                video_file = f
                try:
                    video_size = os.path.getsize(os.path.join(item_path, f))
                except Exception:
                    pass
                break
        
        thumb_file = None
        for tf in ["thumbnail.jpg", "thumbnail_720p.jpg", "thumbnail.png", "thumbnail_option1.jpg"]:
            if tf in files:
                thumb_file = tf
                break
        
        file_list = []
        for f in files:
            fp = os.path.join(item_path, f)
            try:
                sz = os.path.getsize(fp)
                file_list.append({"name": f, "size": format_size(sz), "raw_size": sz})
            except Exception:
                pass
        
        projects.append({
            "name": item,
            "video_file": video_file,
            "video_size": format_size(video_size),
            "thumb_file": thumb_file,
            "files": file_list
        })
    return projects

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Video Projects Network Share</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0b0f19;
  --surface: #141b2d;
  --surface-hover: #1c263f;
  --border: #232f48;
  --accent: #3b82f6;
  --accent-gradient: linear-gradient(135deg, #3b82f6, #8b5cf6);
  --success: #10b981;
  --text: #f8fafc;
  --text-muted: #94a3b8;
}
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Outfit', sans-serif; }
body { background: var(--bg); color: var(--text); min-height: 100vh; padding: 24px; }
.container { max-width: 1400px; margin: 0 auto; }
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  padding: 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  margin-bottom: 20px;
  box-shadow: 0 10px 25px -5px rgba(0,0,0,0.5);
}
.brand { display: flex; align-items: center; gap: 14px; }
.logo-icon {
  width: 48px; height: 48px; border-radius: 12px;
  background: var(--accent-gradient);
  display: flex; align-items: center; justify-content: center;
  font-size: 24px; box-shadow: 0 0 20px rgba(59,130,246,0.4);
}
h1 { font-size: 26px; font-weight: 800; letter-spacing: -0.5px; }
.subtitle { color: var(--text-muted); font-size: 14px; margin-top: 2px; }
.ip-badge {
  background: #1e293b;
  border: 1px solid #334155;
  padding: 10px 18px;
  border-radius: 10px;
  font-size: 14px;
  color: #38bdf8;
  display: flex; align-items: center; gap: 8px;
  font-family: monospace;
}
.ip-badge span { color: var(--text-muted); font-family: sans-serif; }

/* Tip Banner */
.tip-banner {
  background: linear-gradient(90deg, #1e293b, #172554);
  border: 1px solid #3b82f6;
  border-radius: 14px;
  padding: 16px 20px;
  margin-bottom: 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  box-shadow: 0 4px 20px rgba(59,130,246,0.2);
}
.tip-left { display: flex; align-items: center; gap: 14px; }
.tip-icon { font-size: 28px; }
.tip-title { font-weight: 700; font-size: 15px; color: #60a5fa; }
.tip-desc { font-size: 13px; color: #cbd5e1; margin-top: 2px; }
.tip-btn {
  background: #2563eb;
  color: #fff;
  border: none;
  padding: 10px 18px;
  border-radius: 8px;
  font-weight: 600;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.2s;
  display: flex; align-items: center; gap: 6px;
}
.tip-btn:hover { background: #1d4ed8; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 24px;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  display: flex;
  flex-direction: column;
}
.card:hover {
  transform: translateY(-4px);
  border-color: #3b82f6;
  box-shadow: 0 12px 30px rgba(0,0,0,0.6);
}
.thumb-container {
  position: relative;
  aspect-ratio: 16/9;
  background: #0f172a;
  overflow: hidden;
}
.thumb-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 0.3s ease;
}
.card:hover .thumb-img { transform: scale(1.03); }
.thumb-placeholder {
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  color: #475569; font-size: 40px;
}
.video-pill {
  position: absolute;
  bottom: 12px; right: 12px;
  background: rgba(0,0,0,0.85);
  backdrop-filter: blur(8px);
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
  color: #38bdf8;
  border: 1px solid rgba(255,255,255,0.1);
}
.card-body { padding: 20px; flex: 1; display: flex; flex-direction: column; justify-content: space-between; }
.card-title {
  font-size: 18px;
  font-weight: 700;
  margin-bottom: 8px;
  color: #f1f5f9;
  word-break: break-word;
}
.card-meta {
  color: var(--text-muted);
  font-size: 13px;
  margin-bottom: 16px;
  display: flex;
  gap: 12px;
}
.btn-group {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 12px;
}
.btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
  border: none;
  cursor: pointer;
  transition: all 0.2s ease;
}
.btn-primary {
  background: var(--accent);
  color: #fff;
  grid-column: span 2;
}
.btn-primary:hover { background: #2563eb; }
.btn-secondary {
  background: #1e293b;
  color: #cbd5e1;
  border: 1px solid #334155;
}
.btn-secondary:hover { background: #334155; color: #fff; }
.btn-zip {
  background: #0f766e;
  color: #ccfbf1;
}
.btn-zip:hover { background: #115e59; }

/* Modals */
.modal {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.85);
  backdrop-filter: blur(10px);
  z-index: 1000;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.modal.open { display: flex; }
.modal-content {
  width: 100%;
  max-width: 960px;
  background: var(--surface);
  border-radius: 16px;
  border: 1px solid var(--border);
  overflow: hidden;
  box-shadow: 0 25px 50px -12px rgba(0,0,0,0.8);
}
.modal-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 18px 24px; border-bottom: 1px solid var(--border);
}
.modal-header h3 { font-size: 17px; font-weight: 700; color: #f8fafc; }
.modal-close {
  background: none; border: none; color: #94a3b8; font-size: 24px;
  cursor: pointer; line-height: 1;
}
.modal-close:hover { color: #fff; }
video { width: 100%; aspect-ratio: 16/9; background: #000; outline: none; }

/* Toast */
#toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: #1e293b;
  border: 1px solid #3b82f6;
  color: #f8fafc;
  padding: 14px 20px;
  border-radius: 10px;
  box-shadow: 0 10px 25px rgba(0,0,0,0.5);
  font-size: 14px;
  display: none;
  z-index: 2000;
  animation: slideUp 0.3s ease;
}
@keyframes slideUp {
  from { transform: translateY(100%); opacity: 0; }
  to { transform: translateY(0); opacity: 1; }
}
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="brand">
      <div class="logo-icon">🎬</div>
      <div>
        <h1>Video Projects Network Share</h1>
        <div class="subtitle">Direct Wi-Fi Transfer & Streaming Hub</div>
      </div>
    </div>
    <div class="ip-badge">
      <span>Wi-Fi Address:</span>
      <strong>http://{IP}:{PORT}</strong>
    </div>
  </header>

  <div class="tip-banner">
    <div class="tip-left">
      <div class="tip-icon">📂</div>
      <div>
        <div class="tip-title">Download se pehle puchhe kis folder me save karna hai?</div>
        <div class="tip-desc">Apne browser (Chrome/Edge) me 1 setting ON karein, jisse har file par Windows ka <strong>"Save As / Choose Folder"</strong> window khulega!</div>
      </div>
    </div>
    <button class="tip-btn" onclick="openGuideModal()">⚙️ Setting Kaise Karein? (Click Here)</button>
  </div>

  <div class="grid">
    {CARDS}
  </div>
</div>

<!-- Video Modal -->
<div id="videoModal" class="modal" onclick="closeModal(event)">
  <div class="modal-content" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 id="modalTitle">Playing Video</h3>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <video id="player" controls autoplay></video>
  </div>
</div>

<!-- Folder Setting Guide Modal -->
<div id="guideModal" class="modal" onclick="closeGuideModal(event)">
  <div class="modal-content" style="max-width: 680px;" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3>📁 Browser me "Save As / Choose Folder" Dialog kaise ON karein:</h3>
      <button class="modal-close" onclick="closeGuideModal()">&times;</button>
    </div>
    <div style="padding: 24px; line-height: 1.6; font-size: 14px;">
      <div style="margin-bottom: 18px; background: #1e293b; padding: 16px; border-radius: 12px; border: 1px solid #334155;">
        <h4 style="color: #60a5fa; margin-bottom: 8px; font-size: 15px; display: flex; align-items: center; gap: 8px;">
          <span>🔵</span> Google Chrome me (Dusre PC par):
        </h4>
        <ol style="margin-left: 24px; color: #cbd5e1;">
          <li style="margin-bottom: 4px;">Top-right corner par <strong>3 Dots</strong> par click karein ➔ <strong>Settings</strong> kholiye.</li>
          <li style="margin-bottom: 4px;">Left menu me <strong>Downloads</strong> par click karein.</li>
          <li><strong>"Ask where to save each file before downloading"</strong> ko <strong style="color: #4ade80;">Turn ON</strong> kar dein!</li>
        </ol>
      </div>

      <div style="margin-bottom: 18px; background: #1e293b; padding: 16px; border-radius: 12px; border: 1px solid #334155;">
        <h4 style="color: #60a5fa; margin-bottom: 8px; font-size: 15px; display: flex; align-items: center; gap: 8px;">
          <span>🌊</span> Microsoft Edge me (Dusre PC par):
        </h4>
        <ol style="margin-left: 24px; color: #cbd5e1;">
          <li style="margin-bottom: 4px;">Top-right corner par <strong>3 Dots</strong> par click karein ➔ <strong>Settings</strong> kholiye.</li>
          <li style="margin-bottom: 4px;">Left menu me <strong>Downloads</strong> par click karein.</li>
          <li><strong>"Ask me what to do with each download"</strong> ko <strong style="color: #4ade80;">Turn ON</strong> kar dein!</li>
        </ol>
      </div>

      <div style="background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.35); padding: 14px 18px; border-radius: 10px; color: #a7f3d0; font-size: 13px;">
        ✨ <strong>Fayda:</strong> Is setting ke baad aap jaise hi kisi bhi video ya .ZIP par click karenge, Windows turant aapse poochega ki <strong>kis drive (C:, D:, E:) aur kis folder me save karna hai!</strong>
      </div>
      <div style="text-align: right; margin-top: 18px;">
        <button onclick="closeGuideModal()" class="btn btn-primary" style="display: inline-block; padding: 10px 24px;">Got It / Samajh Gaya</button>
      </div>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
function showToast(msg) {
  const t = document.getElementById('toast');
  t.innerText = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 4500);
}

async function smartDownload(url, filename) {
  // If browser supports File System Access API
  if (window.showSaveFilePicker) {
    try {
      const handle = await window.showSaveFilePicker({ suggestedName: filename });
      showToast('Downloading ' + filename + ' to chosen folder...');
      const res = await fetch(url);
      const writable = await handle.createWritable();
      await res.body.pipeTo(writable);
      showToast('✓ ' + filename + ' saved successfully!');
      return;
    } catch (e) {
      if (e.name === 'AbortError') return; // User cancelled
    }
  }

  // Fallback to standard download
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  showToast('Download started! Agar folder nahi poocha to upar "Setting" button dekhein.');
}

function playVideo(url, title) {
  const modal = document.getElementById('videoModal');
  const player = document.getElementById('player');
  const titleEl = document.getElementById('modalTitle');
  titleEl.innerText = title;
  player.src = url;
  modal.classList.add('open');
  player.play();
}
function closeModal(e) {
  const modal = document.getElementById('videoModal');
  const player = document.getElementById('player');
  player.pause();
  player.src = '';
  modal.classList.remove('open');
}
function openGuideModal() {
  document.getElementById('guideModal').classList.add('open');
}
function closeGuideModal() {
  document.getElementById('guideModal').classList.remove('open');
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeModal();
    closeGuideModal();
  }
});
</script>
</body>
</html>
"""

class VideoProjectsHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        # Decode and map directly to BASE_DIR
        parsed = urllib.parse.urlparse(path)
        rel_path = urllib.parse.unquote(parsed.path).lstrip("/")
        if rel_path.startswith("raw/"):
            rel_path = rel_path[4:]
        return os.path.abspath(os.path.join(BASE_DIR, rel_path))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Handle zip download
        if path == "/zip":
            project_name = query.get("project", [""])[0]
            project_dir = os.path.join(BASE_DIR, project_name)
            if not os.path.exists(project_dir) or not os.path.isdir(project_dir):
                self.send_error(404, "Project not found")
                return
            
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            safe_name = urllib.parse.quote(project_name + ".zip")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
            self.end_headers()

            # Stream zip directly
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(project_dir):
                    for f in files:
                        fp = os.path.join(root, f)
                        arcname = os.path.relpath(fp, project_dir)
                        zf.write(fp, arcname)
            return

        # Main Dashboard Page
        if path == "/" or path == "/index.html":
            projects = get_projects_data()
            ip = get_local_ip()
            cards_html = []

            for p in projects:
                p_name_encoded = urllib.parse.quote(p["name"])
                thumb_url = f"/raw/{p_name_encoded}/{p['thumb_file']}" if p['thumb_file'] else ""
                video_url = f"/raw/{p_name_encoded}/{urllib.parse.quote(p['video_file'])}" if p['video_file'] else ""
                
                thumb_html = f'<img src="{thumb_url}" class="thumb-img" alt="{p["name"]}">' if thumb_url else '<div class="thumb-placeholder">🎬</div>'
                pill_html = f'<div class="video-pill">{p["video_size"]}</div>' if p['video_file'] else ''

                video_meta = f"<span>• 🎥 {p['video_size']}</span>" if p['video_file'] else ""
                play_btn = f'<button class="btn btn-primary" onclick="playVideo(\'{video_url}\', \'{p_name_encoded}\')">▶ Watch / Stream</button>' if video_url else ''
                
                vid_filename = p['video_file'] if p['video_file'] else f"{p['name']}.mp4"
                thumb_filename = p['thumb_file'] if p['thumb_file'] else "thumbnail.jpg"
                zip_filename = f"{p['name']}.zip"

                dl_vid_btn = f'<button onclick="smartDownload(\'{video_url}\', \'{vid_filename}\')" class="btn btn-secondary">⬇ Video (.mp4)</button>' if video_url else ''
                dl_thumb_btn = f'<button onclick="smartDownload(\'{thumb_url}\', \'{thumb_filename}\')" class="btn btn-secondary">🖼 Thumbnail</button>' if thumb_url else ''
                zip_btn = f'<button onclick="smartDownload(\'/zip?project={p_name_encoded}\', \'{zip_filename}\')" class="btn btn-zip" style="grid-column: span 2;">📦 Download All as .ZIP</button>'

                card = f"""
                <div class="card">
                  <div class="thumb-container">
                    {thumb_html}
                    {pill_html}
                  </div>
                  <div class="card-body">
                    <div>
                      <div class="card-title">{p['name']}</div>
                      <div class="card-meta">
                        <span>📁 {len(p['files'])} files</span>
                        {video_meta}
                      </div>
                    </div>
                    <div>
                      <div class="btn-group">
                        {play_btn}
                        {dl_vid_btn}
                        {dl_thumb_btn}
                        {zip_btn}
                      </div>
                    </div>
                  </div>
                </div>
                """
                cards_html.append(card)

            rendered = HTML_TEMPLATE.replace("{IP}", ip).replace("{PORT}", str(PORT)).replace("{CARDS}", "\n".join(cards_html))
            data = rendered.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # Fallback to SimpleHTTPRequestHandler with range support for raw files
        return super().do_GET()

def run():
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, VideoProjectsHandler)
    ip = get_local_ip()
    print("============================================================")
    print("Video Projects Wi-Fi Server RUNNING!")
    print(f"Local Access:   http://localhost:{PORT}")
    print(f"Network Access: http://{ip}:{PORT}")
    print("============================================================")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Server stopped.")
        httpd.server_close()

if __name__ == "__main__":
    run()

import os
import sys
import requests

file_path = r"C:\Users\mohda\.gemini\antigravity-ide\scratch\Mk-tool\dist\MkTool_v1.0.0_Setup.exe"
token = "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"
url = "https://uploads.github.com/repos/mohdkhan-lab/Mk-tool/releases/428042741/assets?name=MkTool_v1.0.0_Setup.exe"

if not os.path.isfile(file_path):
    print(f"File not found: {file_path}")
    sys.exit(1)

size_mb = os.path.getsize(file_path) / (1024 * 1024)
print(f"Uploading {size_mb:.2f} MB directly to GitHub Releases via streaming...")

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/octet-stream",
    "Accept": "application/vnd.github+json"
}

with open(file_path, "rb") as f:
    r = requests.post(url, headers=headers, data=f, timeout=1200)

print(f"Status Code: {r.status_code}")
if r.status_code in (200, 201):
    data = r.json()
    print("SUCCESS! Asset uploaded successfully!")
    print(f"Download URL: {data.get('browser_download_url')}")
else:
    print(f"Upload failed: {r.text}")
    sys.exit(1)


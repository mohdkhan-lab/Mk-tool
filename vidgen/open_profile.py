"""
Open one browser profile so the user can sign into their Google account — the
one thing a copied install cannot carry, and what the image stage needs.

    python open_profile.py acc1

It opens the Gemini bulk-image app in that profile's Chrome. Signing into Google
there is what makes `images` work. When the app appears (or the sign-in is done)
the user just closes the window; the login is saved into the profile by itself.

Launched detached by the dashboard's "Open & Login" button; also runnable by
hand. Blocks until the window is closed, on purpose.
"""
import sys

import env  # noqa: F401 — sets up sys.path (bulkgen, ai_avatar_pipeline) and ffmpeg

from bulkgem import login  # noqa: E402


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    print(f"[open_profile] opening '{name}' — sign into Google, then close the window.",
          flush=True)
    login(name)
    print(f"[open_profile] '{name}' saved.", flush=True)


if __name__ == "__main__":
    main()

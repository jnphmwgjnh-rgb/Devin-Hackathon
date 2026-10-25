"""Build the landing page if possible, then launch the app on port 8080."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
LANDING = ROOT / "landing"
DIST = LANDING / "dist"

if not PY.is_file():
    raise SystemExit(f"Python environment not found: {PY}")

if not DIST.is_dir():
    npm = shutil.which("npm")
    if npm:
        if not (LANDING / "node_modules").is_dir():
            subprocess.run([npm, "ci"], cwd=LANDING, check=True)
        subprocess.run([npm, "run", "build"], cwd=LANDING, check=True)
    else:
        print("npm is not installed; the server will redirect / to /intake.")

subprocess.run([str(PY), str(ROOT / "deploy" / "server.py")], cwd=ROOT, check=True)

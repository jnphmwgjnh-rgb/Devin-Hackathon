"""One command to launch the demo stack: dashboard (:8000) + chat (:7860).
Ctrl-C stops both."""
import os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / ("python.exe" if os.name == "nt" else "python3")
env = {**os.environ, "DEMO": os.environ.get("DEMO", "true")}

procs = [
    subprocess.Popen([str(PY), "-m", "app", "--runs-dir",
                      str(ROOT / "cfd-dashboard" / "runs_modelling"),
                      "--poll-interval", "0.25", "--port", "8000",
                      "--budget", "48"],
                     cwd=ROOT / "cfd-dashboard" / "backend", env=env),
    subprocess.Popen([str(PY), "app.py"], cwd=ROOT / "stage1_intake", env=env),
]
print("Dashboard: http://127.0.0.1:8000")
print("Chat:      http://127.0.0.1:7860")
print("Demo: chat -> finalize -> Open modelling dashboard -> watch the Pareto fill in ~10 s")
try:
    procs[0].wait()
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()

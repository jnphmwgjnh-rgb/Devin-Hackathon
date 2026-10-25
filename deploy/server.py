"""Single-process application entry point on port 8080."""

import os
import sys
from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("PORT", "8080"))
RUNS_DIR = Path(os.getenv("CFD_RUNS_DIR", ROOT / "cfd-dashboard" / "runs_modelling"))
sys.path.insert(0, str(ROOT / "stage3_modelling"))
sys.path.insert(0, str(ROOT / "cfd-dashboard" / "backend"))

from app.config import AppConfig  # noqa: E402
from app.main import create_app  # noqa: E402

app = create_app(AppConfig(
    runs_dir=RUNS_DIR,
    poll_interval=float(os.getenv("CFD_POLL_INTERVAL", "0.25")),
))

# The backend's standalone dashboard mount is replaced by explicit app routes here.
app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "name", None) != "frontend"
]
frontend = ROOT / "cfd-dashboard" / "frontend"
landing_dist = ROOT / "landing" / "dist"

app.add_api_route("/", lambda: (
    FileResponse(landing_dist / "index.html")
    if (landing_dist / "index.html").is_file()
    else RedirectResponse("/intake")
), methods=["GET"], include_in_schema=False)
app.add_api_route(
    "/intake",
    lambda: FileResponse(frontend / "intake.html"),
    methods=["GET"],
    include_in_schema=False,
)
app.add_api_route(
    "/intake.js",
    lambda: FileResponse(frontend / "intake.js", media_type="text/javascript"),
    methods=["GET"],
    include_in_schema=False,
)
app.add_api_route(
    "/dashboard",
    lambda: RedirectResponse("/dashboard/"),
    methods=["GET"],
    include_in_schema=False,
)
app.add_api_route(
    "/chat",
    lambda: RedirectResponse("/intake"),
    methods=["GET"],
    include_in_schema=False,
)
app.mount("/dashboard", StaticFiles(directory=frontend, html=True), name="dashboard")
if landing_dist.is_dir():
    app.mount("/", StaticFiles(directory=landing_dist, html=True), name="landing")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, proxy_headers=True, forwarded_allow_ips="*")

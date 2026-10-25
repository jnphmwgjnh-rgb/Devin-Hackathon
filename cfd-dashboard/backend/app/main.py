from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import AppConfig
from app.store import RunStore
from app.watcher import DirectoryWatcher, WebSocketHub


def create_app(cfg: AppConfig) -> FastAPI:
    store = RunStore(cfg)
    hub = WebSocketHub()
    watcher = DirectoryWatcher(store, hub)

    @asynccontextmanager
    async def lifespan(app):
        store.scan()
        watcher.start()
        yield
        await watcher.stop()

    app = FastAPI(title="CFD Optimization Dashboard", lifespan=lifespan)
    app.state.store = store
    app.state.watcher = watcher
    app.state.intake_sessions = {}

    from app.intake import router as intake_router

    app.include_router(intake_router)

    @app.get("/api/runs")
    def list_runs():
        return {"runs": store.list_runs(), "summary": store.summary()}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        residuals = run.residuals
        iterations = residuals["iterations"]
        fields = residuals["fields"]
        if len(iterations) > 500:
            indices = [round(i * (len(iterations) - 1) / 499) for i in range(500)]
            iterations = [iterations[index] for index in indices]
            fields = {
                name: [values[index] for index in indices]
                for name, values in fields.items()
            }
        return {**run.to_dict(), "residuals": {"iterations": iterations, "fields": fields}}

    @app.get("/api/runs/{run_id}/cad")
    def get_cad(run_id: str, format: str = "stl"):
        if format not in {"step", "stl", "glb"}:
            raise HTTPException(status_code=400, detail="format must be step, stl, or glb")
        store.scan()
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        design_id = (run.details or {}).get("design_id")
        if not design_id:
            raise HTTPException(status_code=404, detail="CAD not found for this run")
        path = cfg.runs_dir / run_id / f"{design_id}.{format}"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="CAD not found for this run")
        media_type = {
            "step": "application/step",
            "stl": "model/stl",
            "glb": "model/gltf-binary",
        }[format]
        return FileResponse(path, media_type=media_type, filename=f"{design_id}.{format}")

    from app import modelling

    @app.post("/api/modelling/start")
    def modelling_start(body: dict = None):
        body = body or {}
        intake_id = body.get("intake_id")
        session = app.state.intake_sessions.get(intake_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Intake not found")
        if session["assessment"]["status"] != "ok" or session["assessment"]["questions"]:
            raise HTTPException(status_code=409, detail="Intake still needs required answers")
        return modelling.start_intake(
            intake_id, session, cfg.runs_dir, app.state.intake_sessions
        )

    @app.get("/api/modelling/status")
    def modelling_status():
        return modelling.status()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await hub.connect(websocket)
        try:
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "runs": store.list_runs(),
                    "summary": store.summary(),
                }
            )
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            hub.disconnect(websocket)
        except Exception:
            hub.disconnect(websocket)
            raise

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    return app

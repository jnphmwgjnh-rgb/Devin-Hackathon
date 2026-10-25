"""Stage-3 modelling campaign: demo replay or live engine run into run folders."""

import base64
import json
import os
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_STAGE3 = Path(__file__).resolve().parents[3] / "stage3_modelling"
_DEMO_BUNDLE = _STAGE3 / "demo" / "itherm"
_ALLOWED_FILES = {"design.step", "design.stl", "preview.glb", "model_result.json"}

_state = {
    "campaign_id": None, "mode": None, "status": "idle", "stage": None,
    "progress": 0.0, "message": None, "started_at": None,
    "interpretation": {}, "temperature_label": "Hottest HBM temperature",
    "selected": [], "label": "Low-fidelity reduced-order estimate — not validated CFD",
    "demo_note": ("Demo replay · precomputed results from a real run, "
                  "accelerated (DEMO=false runs live)"),
    "error": None,
}
_lock = threading.Lock()
_cancel = threading.Event()


def _now():
    return datetime.now(timezone.utc).isoformat()


def status():
    with _lock:
        return dict(_state)


def _set(**kw):
    with _lock:
        _state.update(kw)


def _archive_runs(runs_dir: Path):
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return
    kids = [p for p in runs_dir.iterdir() if p.is_dir()]
    if not kids:
        return
    archive = runs_dir.parent / f"{runs_dir.name}_archive" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive.mkdir(parents=True, exist_ok=True)
    for p in kids:
        shutil.move(str(p), archive / p.name)


def _status_cb(runs_dir):
    def cb(d):
        _set(stage=d.get("stage"), progress=d.get("progress", 0.0),
             message=d.get("message"),
             interpretation=d.get("interpretation") or _state["interpretation"],
             selected=d.get("selected") or _state["selected"],
             status="running" if d.get("stage") != "completed" else "completed")
        (Path(runs_dir) / "_campaign.json").write_text(json.dumps(status(), indent=2))
    return cb


def _run_demo(runs_dir):
    sys.path.insert(0, str(_STAGE3))
    from coldplate.demo_replay import replay_bundle
    return replay_bundle(_DEMO_BUNDLE, runs_dir, total_seconds=10.0,
                         status_cb=_status_cb(runs_dir), cancel=_cancel)


def _run_live(runs_dir, document, reference_files):
    sys.path.insert(0, str(_STAGE3))
    from coldplate.dashboard_sink import candidate_record, write_cad, write_model_result
    from coldplate.pipeline import run_job
    runs = Path(runs_dir)
    n = [0]
    run_index = [0]

    def on_candidate(c):
        run_index[0] += 1
        if c.candidate_id != "c000":
            n[0] += 1
        write_model_result(runs, run_index[0], candidate_record(
            {"candidate_id": c.candidate_id, "params": c.params,
             "metrics": c.metrics, "feasible": c.feasible,
             "violations": c.violations, "role": None, "design_id": c.candidate_id},
            status="completed", started_at=_now(), finished_at=_now()))
        planned = 24 * 8
        _set(progress=0.2 + 0.6 * min(n[0] / planned, 1.0),
             message=f"evaluated {n[0]}/{planned}")

    def on_interp(info):
        _set(interpretation=info)

    job_dir = _STAGE3 / "runs" / "jobs" / f"live-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    run_job(job_dir, document, reference_files,
            {"parser": "devin", "prompt_version": "v2", "use_cache": True,
             "pop_size": 24, "n_gen": 8, "seed": 42, "grid_mm": 0.5,
             "sweep": True, "export_cad": True},
            on_interpretation=on_interp, on_candidate=on_candidate)
    return {"status": "completed", "n_runs": n[0]}


def start(body: dict, runs_dir: Path) -> dict:
    demo = body.get("demo")
    if demo is None:
        demo = os.environ.get("DEMO", "true").lower() not in ("false", "0", "no")
    with _lock:
        if _state.get("status") == "running":
            return {"error": "campaign already running"}
    runs_dir = Path(runs_dir)
    _archive_runs(runs_dir)
    _cancel.clear()
    _set(campaign_id=f"c{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
         mode="demo" if demo else "live", status="running", stage="interpreting",
         progress=0.0, message="starting", started_at=_now(),
         interpretation={}, selected=[], error=None)
    doc = body.get("requirements_markdown")
    final = body.get("requirements_final")
    refs = {f["name"]: base64.b64decode(f["content_base64"])
            for f in body.get("reference_files") or []}
    (runs_dir / "_campaign.json").parent.mkdir(parents=True, exist_ok=True)

    def work():
        try:
            if demo:
                res = _run_demo(runs_dir)
            else:
                if doc is None and final is not None:
                    sys.path.insert(0, str(_STAGE3))
                    from coldplate.intake_bridge import render_requirements_markdown
                    doc_ = render_requirements_markdown(final)
                else:
                    doc_ = doc
                if not doc_:
                    _set(status="failed", error="no requirements supplied")
                    return
                res = _run_live(runs_dir, doc_, refs)
            if res.get("status") == "completed":
                _set(status="completed", progress=1.0, stage="completed",
                     message="campaign complete")
            else:
                _set(status="failed", error=res.get("status", "failed"))
        except Exception as e:
            _set(status="failed", error=str(e))
        finally:
            try:
                (runs_dir / "_campaign.json").write_text(json.dumps(status(), indent=2))
            except OSError:
                pass

    threading.Thread(target=work, daemon=True).start()
    return {"campaign": status()}

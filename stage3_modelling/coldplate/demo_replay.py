"""Replay a completed stage-3 job bundle into cfd-dashboard run folders,
accelerated (~10 s) and clearly labelled as a demo replay."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from coldplate.dashboard_sink import candidate_record, run_dir, write_cad, write_model_result

STAGES = [
    ("interpreting", 1.5),
    ("generating", 0.5),
    ("evaluating", 6.5),
    ("selecting", 0.5),
    ("exporting", 1.0),
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def replay_bundle(bundle_dir: Path, runs_dir: Path, *,
                  total_seconds: float = 10.0, status_cb=None,
                  cancel: threading.Event | None = None) -> dict:
    bundle_dir = Path(bundle_dir)
    runs_dir = Path(runs_dir)
    results = json.loads((bundle_dir / "results.json").read_text())
    interp = results.get("interpretation", {})
    candidates = results.get("candidates", [])
    selected = {s["candidate_id"]: s for s in results.get("selected_designs", [])}
    scale = total_seconds / 10.0 if total_seconds else 0.0
    cancel = cancel or threading.Event()

    def emit(stage, frac, msg, **extra):
        if status_cb:
            status_cb(dict(stage=stage, progress=frac, message=msg, **extra))

    def wait(s):
        end = time.monotonic() + s * scale
        while time.monotonic() < end:
            if cancel.is_set():
                return False
            time.sleep(min(0.05, end - time.monotonic()))
        return True

    # interpreting: surface the recorded Devin session
    sess = (interp.get("sessions") or [{}])[0]
    emit("interpreting", 0.05,
         f"{interp.get('provider_label','parsed requirements')} "
         f"(session {sess.get('session_id','?')})",
         interpretation={
             "provider_label": interp.get("provider_label"),
             "session_url": sess.get("url"),
             "cached": interp.get("cached"),
             "devin_calls": interp.get("devin_calls"),
         })
    if not wait(STAGES[0][1]):
        return {"status": "cancelled"}
    emit("generating", 0.2, "generating candidate geometries")
    if not wait(STAGES[1][1]):
        return {"status": "cancelled"}

    # evaluating: stream candidates in original order, two in flight
    n = len(candidates)
    t_start = _now()
    per = STAGES[2][1] * scale / max(n, 1)
    eval_start = time.monotonic()
    in_flight = []
    run_of = {}
    for i, c in enumerate(candidates):
        if cancel.is_set():
            return {"status": "cancelled"}
        idx = i + 1
        run_of[c["candidate_id"]] = idx
        write_model_result(runs_dir, idx, candidate_record(
            {**c, "role": None}, status="running",
            started_at=t_start, finished_at=None))
        in_flight.append((idx, c))
        # pace the stage over its full budget: candidate i completes at stage start + (i + 1) * per
        deadline = eval_start + (i + 1) * per
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        done_idx, done_c = in_flight.pop(0)
        write_model_result(runs_dir, done_idx, candidate_record(
            {**done_c, "role": None}, status="completed",
            started_at=t_start, finished_at=_now()))
        emit("evaluating", 0.2 + 0.5 * (i + 1) / n,
             f"evaluated {i + 1}/{n}")
    for idx, c in in_flight:
        write_model_result(runs_dir, idx, candidate_record(
            {**c, "role": None}, status="completed",
            started_at=t_start, finished_at=_now()))

    # selecting: write roles into the selected runs
    emit("selecting", 0.75, "selecting pareto-optimal designs")
    sel_ids = []
    for cid, s in selected.items():
        idx = run_of.get(cid)
        if idx is None:
            continue
        write_model_result(runs_dir, idx, candidate_record(
            s, status="completed", started_at=t_start, finished_at=_now()))
        sel_ids.append({"run_id": f"run_{idx:04d}", "role": s.get("role"),
                        "role_label": s.get("role_label")})
    if not wait(STAGES[3][1]):
        return {"status": "cancelled"}

    # exporting: copy CAD files into selected runs
    emit("exporting", 0.9, "exporting CAD for selected designs")
    for cid, s in selected.items():
        idx = run_of.get(cid)
        if idx is None:
            continue
        ddir = bundle_dir / "designs" / s["design_id"]
        files = {}
        for key, fname, legacy_name in (
            ("stl", f"{s['design_id']}.stl", "design.stl"),
            ("step", f"{s['design_id']}.step", "design.step"),
            ("glb", f"{s['design_id']}.glb", "preview.glb"),
        ):
            p = ddir / fname
            if not p.exists():
                p = ddir / legacy_name
            if p.exists():
                files[key] = p.read_bytes()
        write_cad(runs_dir, idx, files)
    if not wait(STAGES[4][1]):
        return {"status": "cancelled"}

    emit("completed", 1.0, "campaign complete", selected=sel_ids)
    return {"status": "completed", "n_runs": n, "selected": sel_ids}

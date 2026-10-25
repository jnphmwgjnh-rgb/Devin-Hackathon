"""Write cfd-dashboard run folders (atomic; half-written files never appear)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

LABEL = "Low-fidelity reduced-order estimate — not validated CFD"


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # Windows refuses to replace a file another thread is reading (the dashboard
        # poller); retry briefly instead of failing the campaign.
        for attempt in range(80):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 79:
                    raise
                time.sleep(0.025)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def run_dir(runs_dir: Path, run_id: str) -> Path:
    return Path(runs_dir) / str(run_id)


def write_model_result(runs_dir, run_id, record: dict) -> Path:
    """record: {source, label, status, params, metrics, feasible, violations,
    role, candidate_id, design_id, started_at, finished_at}"""
    rec = {"source": "stage3-reduced-order-model", "label": LABEL, **record}
    p = run_dir(runs_dir, run_id) / "model_result.json"
    atomic_write(p, json.dumps(rec, indent=2).encode())
    return p


def write_cad(runs_dir, run_id, files: dict):
    """files: {step: bytes|None, stl: bytes|None, glb: bytes|None}."""
    d = run_dir(runs_dir, run_id)
    design_id = files.get("design_id") or run_id
    if files.get("step"):
        atomic_write(d / f"{design_id}.step", files["step"])
    if files.get("glb"):
        atomic_write(d / f"{design_id}.glb", files["glb"])
    if files.get("stl"):
        atomic_write(d / f"{design_id}.stl", files["stl"])


def candidate_record(candidate: dict, status: str = "completed",
                     started_at=None, finished_at=None) -> dict:
    """Build the model_result.json body from a results-doc candidate."""
    m = candidate["metrics"]
    p = candidate["params"]
    return {
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "params": {
            "fin_thickness_mm": p.get("fin_thickness_mm"),
            "channel_width_by_zone_mm": p.get("channel_width_by_zone_mm"),
            "fin_height_mm": p.get("fin_height_mm"),
            "n_channels": p.get("n_channels"),
        },
        "metrics": {
            "pressure_drop_pa": m["pressure_drop_kPa"] * 1000.0,
            "max_temperature_k": m["critical_temperature_C"] + 273.15,
            "mean_temperature_k": m.get("coolant_outlet_C", 0) + 273.15,
            "gpu_max_c": m.get("gpu_max_C"),
            "hbm_max_c": m.get("hbm_max_C"),
            "hbm_hottest": m.get("hbm_hottest"),
            "gpu_hottest": m.get("gpu_hottest"),
            "coolant_rise_k": m.get("coolant_rise_K"),
            "energy_residual": m.get("energy_residual"),
        },
        "feasible": candidate.get("feasible"),
        "violations": candidate.get("violations", []),
        "role": candidate.get("role"),
        "candidate_id": candidate.get("candidate_id"),
        "design_id": candidate.get("design_id"),
        "cad_error": candidate.get("cad_error"),
    }

"""Live end-to-end test: real Devin API parse of A through run_job.

Marked `live` - deselected by default. Consumes Devin quota.
Run: pytest -m live tests/test_e2e_live.py
Control cache via COLDPLATE_LIVE_USE_CACHE=1 (default 0 = fresh session).
"""

import json
import os
from pathlib import Path

import pytest

from coldplate.pipeline import run_job

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).parent / "fixtures"


def test_A_live_endtoend(tmp_path):
    doc = (FIXTURES / "A_canonical.md").read_text(encoding="utf-8")
    ref = {"power_map_W_per_mm2.csv":
           (FIXTURES / "reference_files/power_map_W_per_mm2.csv").read_bytes()}
    use_cache = os.environ.get("COLDPLATE_LIVE_USE_CACHE", "0") == "1"
    res = run_job(
        tmp_path / "liveA", doc, ref,
        {"parser": "devin", "prompt_version": "v2", "use_cache": use_cache,
         "pop_size": 24, "n_gen": 8, "seed": 42, "grid_mm": 0.5,
         "sweep": True, "export_cad": True},
    )
    interp = res["interpretation"]
    assert interp["provider"] == "devin"
    if not interp["cached"]:
        assert interp["devin_calls"] >= 1

    assert res["status"] == "completed"
    spec = res["normalized_inputs"]
    gpus = [s for s in spec["heat_sources"] if s["type"] == "GPU"]
    hbms = [s for s in spec["heat_sources"] if s["type"] == "HBM"]
    assert len(gpus) == 2 and len(hbms) == 8
    assert spec["total_power_W"] == pytest.approx(1492.8)
    assert spec["coolant"]["flow_rate_Lpm"]["value"] == 2.0
    assert spec["plate"]["length_x_mm"]["value"] == 78.0
    assert spec["plate"]["width_y_mm"]["value"] == 78.0
    assert spec["plate"]["thickness_mm"]["value"] == 2.0

    sel = res["selected_designs"]
    assert len(sel) <= 3
    assert len({json.dumps(s["params"], sort_keys=True) for s in sel}) == len(sel)
    for s in sel:
        for role in ("step", "stl", "glb"):
            p = tmp_path / "liveA" / s["files"][role]["path"]
            assert p.exists() and p.stat().st_size > 1000
        assert s["cad_check"]["valid"]
        assert s["cad_check"]["volume_rel_err"] < 1e-3

    feas = [c for c in res["candidates"] if c["feasible"]]
    xy = {c["candidate_id"]: (c["metrics"]["pressure_drop_kPa"],
                              c["metrics"]["critical_temperature_C"]) for c in feas}
    for cid in res["pareto"]["front_candidate_ids"]:
        a = xy[cid]
        assert not any(
            j != cid and xy[j][0] <= a[0] and xy[j][1] <= a[1]
            and (xy[j][0] < a[0] or xy[j][1] < a[1]) for j in xy)

    assert res["candidates"][0]["metrics"]["coolant_rise_K"] == pytest.approx(10.75, abs=0.01)
    assert any("21.49" in c for c in res["requirement_conflicts"])

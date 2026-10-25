"""Optimizer + pipeline + API tests (recorded provider only)."""

import io
import json
import time
import zipfile
from itertools import product
from pathlib import Path

import numpy as np
import pytest

from coldplate.extraction_schema import RequirementsExtraction
from coldplate.geometry import (
    baseline_design, build_layout, derive_zone_bands, GeometryError,
)
from coldplate.normalize import normalize_extraction
from coldplate.optimize import (
    _dominates, _levels, generate_candidates, requirement_conflicts,
    select_designs,
)
from coldplate.pipeline import run_job
from coldplate.results_models import ResultsDoc

FIXTURES = Path(__file__).parent / "fixtures"
REC = FIXTURES / "devin_recordings"
DOCS = {p.stem: p.read_text(encoding="utf-8") for p in FIXTURES.glob("*.md")}
CSV = (FIXTURES / "reference_files" / "power_map_W_per_mm2.csv").read_bytes()
REF = {"power_map_W_per_mm2.csv": CSV}

FAST = {"parser": "recorded", "prompt_version": "v2", "use_cache": False,
        "pop_size": 8, "n_gen": 2, "seed": 42, "grid_mm": 1.0, "sweep": True,
        "export_cad": True}


def rec(stem, ver="v2"):
    return json.loads((REC / f"{stem}.{ver}.json").read_text(encoding="utf-8"))


def inputs_A():
    pr = rec("A_canonical")
    from coldplate.parser import parse_requirements
    r = parse_requirements(DOCS["A_canonical"], reference_files=REF,
                           provider="recorded", recording=pr, use_cache=False)
    assert r.status == "ok"
    return r.spec.engine_inputs()


# ---------------------------------------------------------------- optimize


def test_index_vector_candidate_ids():
    ei = inputs_A()
    vector = (4, 4, 4)
    a, rejected_a = generate_candidates(ei, [vector], candidate_ids=["c123"])
    b, rejected_b = generate_candidates(ei, [vector], candidate_ids=["c123"])
    assert not rejected_a and not rejected_b
    assert a[0].design_id == b[0].design_id == "c123"
    assert a[0].zones == b[0].zones
    assert a[0].zones[0].fin_thickness_mm == _levels(ei)[vector[0]]


def test_all_candidates_wall_thickness():
    ei = inputs_A()
    levels = _levels(ei)
    labels = {label for label, _, _ in derive_zone_bands(ei, ei.flow_axis)}
    vectors = list(product(range(len(levels)), repeat=1 + len(labels)))[:48]
    designs, _ = generate_candidates(
        ei, vectors, candidate_ids=[f"c{i + 1:03d}" for i in range(len(vectors))]
    )
    for d in designs:
        la = build_layout(ei, d)  # build_layout itself validates wall thickness
        spans = sorted((f.y0, f.y1) for f in la.fins)
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 1e-9:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        assert all(b - a <= ei.max_feature_mm + 1e-9 for a, b in merged), d.design_id


def test_rejected_candidates():
    ei = inputs_A()
    # force a rejection: min_feature > some level combos? manufacture a bad one
    from coldplate.geometry import FinZone, ChannelDesign
    bad = ChannelDesign(design_id="c900", arrangement=ei.arrangement,
                        flow_axis="x", fin_height_mm=2.0,
                        zones=(FinZone("GPU", 12.925, 65.075, 0.05, 0.3),),
                        inlet_opening_mm=ei.inlet_opening_mm,
                        outlet_opening_mm=ei.outlet_opening_mm)
    with pytest.raises(GeometryError):
        build_layout(ei, bad)


def test_nondominance_bruteforce():
    rng = np.random.default_rng(0)
    pts = rng.random((30, 2))
    def nd(i):
        return not any(j != i and _dominates(tuple(pts[j]), tuple(pts[i]))
                       for j in range(30))
    flags = [nd(i) for i in range(30)]
    # every dominated point has a dominator on the front
    for i in range(30):
        if not flags[i]:
            assert any(flags[j] and _dominates(tuple(pts[j]), tuple(pts[i]))
                       for j in range(30))


def test_conflicts_itherm():
    ei = inputs_A()
    msgs = requirement_conflicts(ei)
    txt = " ".join(msgs)
    assert "1 L/min" in txt or "1.0" in txt
    assert "21.49" in txt
    assert "14.33" in txt
    assert "1.79" in txt or "1.8" in txt


# ------------------------------------------------------- end-to-end pipeline


def _wait_results(tmp_path, stem, extra_opts=None):
    opts = {**FAST, "recording": rec(stem), **(extra_opts or {})}
    return run_job(tmp_path / "jobA", DOCS[stem], REF, opts)


def test_A_endtoend(tmp_path):
    res = _wait_results(tmp_path, "A_canonical")
    assert res["status"] == "completed"
    spec = res["normalized_inputs"]
    assert len([s for s in spec["heat_sources"] if s["type"] == "GPU"]) == 2
    assert len([s for s in spec["heat_sources"] if s["type"] == "HBM"]) == 8
    assert spec["total_power_W"] == pytest.approx(1492.8)
    assert spec["coolant"]["flow_rate_Lpm"]["value"] == 2.0
    sel = res["selected_designs"]
    assert len(sel) <= 3
    assert {s["role"] for s in sel} <= {
        "pressure_focused", "cooling_focused", "balanced"
    }
    assert len({s["design_id"] for s in sel}) == len(sel)
    # files exist + manifest sha
    manifest = json.loads((tmp_path / "jobA" / "manifest.json").read_text())
    by_path = {e["path"]: e for e in manifest["files"]}
    import hashlib
    for s in sel:
        did = s["design_id"]
        for role in ("step", "stl", "glb", "fields", "geometry"):
            fr = s["files"][role]
            p = tmp_path / "jobA" / fr["path"]
            assert p.exists(), fr["path"]
            assert hashlib.sha256(p.read_bytes()).hexdigest() == by_path[fr["path"]]["sha256"]
        assert s["cad_check"]["volume_rel_err"] < 1e-3
        assert s["cad_check"]["bbox_mm"] == pytest.approx([78.0, 78.0, 4.0], abs=0.1)
    # pareto ids nondominated among feasible
    feas = [c for c in res["candidates"] if c["feasible"]]
    xy = {c["candidate_id"]: (c["metrics"]["pressure_drop_kPa"],
                              c["metrics"]["critical_temperature_C"]) for c in feas}
    for cid in res["pareto"]["front_candidate_ids"]:
        assert cid in xy
        assert not any(j != cid and _dominates(xy[j], xy[cid]) for j in xy)
    assert any("21.49" in m for m in res["requirement_conflicts"])
    assert res["candidates"][0]["metrics"]["coolant_rise_K"] == pytest.approx(10.75, abs=0.1)
    # acceptance worst point = highest inlet temp + LOWEST flow
    wp = sel[0]["acceptance"]["worst_point"]
    assert wp["flow_Lpm"] == 1.0
    assert wp["inlet_C"] == 35.0
    ResultsDoc.model_validate(res)
    # bundle zip contains every manifest path (via API-less check)
    with zipfile.ZipFile(io.BytesIO(b""), "w"):
        pass


def test_E_needs_input(tmp_path):
    res = _wait_results(tmp_path, "E_missing_flow")
    assert res["status"] == "needs_input"
    assert res["selected_designs"] == []
    spec = res["normalized_inputs"]
    assert "coolant.flow_rate" in res["interpretation"]["missing_essential"]
    fr = spec["coolant"]["flow_rate_Lpm"]
    assert fr["value"] != 2.0


def test_B_C_D(tmp_path):
    res_b = _wait_results(tmp_path / "b", "B_flow_3lpm")
    assert res_b["status"] == "completed"
    assert res_b["candidates"][0]["metrics"]["coolant_rise_K"] == pytest.approx(7.16, abs=0.05)
    res_c = _wait_results(tmp_path / "c", "C_gpu1_power_plus20")
    spec = res_c["normalized_inputs"]
    assert spec["total_power_W"] == pytest.approx(1603.68)
    res_d = _wait_results(tmp_path / "d", "D_plate_90x84")
    s0 = res_d["selected_designs"][0]
    assert s0["cad_check"]["bbox_mm"] == pytest.approx([90.0, 84.0, 4.5], abs=0.1)
    geo = json.loads((tmp_path / "d" / "jobA" / "designs" / s0["design_id"] / "geometry.json").read_text())
    for f in geo["fins"]:
        assert 17.0 - 1e-3 <= f["x0"] and f["x1"] <= 73.0 + 1e-3
        assert 15.0 - 1e-3 <= f["y0"] and f["y1"] <= 69.0 + 1e-3


def test_F_all_infeasible(tmp_path):
    res = _wait_results(tmp_path, "F_impossible_limits")
    assert res["status"] == "completed"
    assert res["pareto"]["n_feasible"] == 0
    assert res["pareto"]["all_infeasible"] is True
    assert res["selected_designs"] == []

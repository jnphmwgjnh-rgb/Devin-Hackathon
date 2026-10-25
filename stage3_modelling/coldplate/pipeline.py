"""run_job: parse -> normalize -> optimize -> sweep -> export -> results bundle."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from coldplate import __version__ as ENGINE_VERSION
from coldplate.cad import export_design
from coldplate.evaluate import SIMULATION_METADATA, evaluate
from coldplate.geometry import build_layout
from coldplate.optimize import (
    requirement_conflicts, run_optimization, select_designs, sweep_design,
)
from coldplate.parser import parse_requirements

LABEL = ("Low-fidelity reduced-order thermohydraulic estimates "
         "— not validated 3D CFD")

DEFAULT_OPTIONS = {
    "parser": "devin",
    "prompt_version": "v2",
    "use_cache": True,
    "pop_size": 24,
    "n_gen": 8,
    "seed": 42,
    "grid_mm": 0.5,
    "sweep": True,
    "export_cad": True,
}

_MIME = {".json": "application/json", ".md": "text/markdown", ".csv": "text/csv",
         ".step": "application/step", ".stp": "application/step",
         ".stl": "model/stl", ".glb": "model/gltf-binary"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Bundle:
    def __init__(self, job_dir: Path, job_id: str):
        self.dir = job_dir
        self.entries: list[dict] = []
        self.job_id = job_id

    def write(self, rel: str, data, role: str, design_id: Optional[str] = None) -> dict:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            raw = data.encode("utf-8")
        elif isinstance(data, bytes):
            raw = data
        else:
            raw = json.dumps(data, indent=2, default=str).encode("utf-8")
        p.write_bytes(raw)
        e = {"path": rel, "role": role,
             "media_type": _MIME.get(p.suffix.lower(), "application/octet-stream"),
             "bytes": len(raw), "sha256": _sha256(raw),
             "design_id": design_id}
        self.entries.append(e)
        return e

    def manifest(self) -> dict:
        return {"schema_version": "coldplate-manifest/1",
                "job_id": self.job_id, "files": self.entries}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_job(
    job_dir: Path,
    document: str,
    reference_files: dict,
    options: dict,
    progress: Optional[Callable] = None,
    on_interpretation: Optional[Callable] = None,
    on_candidate: Optional[Callable] = None,
    on_selected: Optional[Callable] = None,
    on_cad: Optional[Callable] = None,
) -> dict:
    """Run a full job; writes the bundle under job_dir; returns the results doc."""
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    job_dir = Path(job_dir)
    job_id = job_dir.name
    bundle = _Bundle(job_dir, job_id)
    t_created = _now()
    t0 = time.perf_counter()

    def emit(stage: str, frac: float, msg: str):
        if progress:
            progress(stage, frac, msg)

    results = {
        "schema_version": "coldplate-results/1", "job_id": job_id,
        "engine_version": ENGINE_VERSION, "created_at": t_created,
        "completed_at": None, "status": "running", "label": LABEL,
        "inputs": {}, "interpretation": {}, "normalized_inputs": None,
        "assumptions": [], "requirement_conflicts": [], "simulation": {},
        "objectives": None, "constraints": [], "candidates": [],
        "rejected_candidates": [], "pareto": {}, "optimizer": {},
        "selected_designs": [],
        "warnings": [], "links": {},
    }
    links = {
        "self": f"/api/results/{job_id}",
        "manifest": f"/api/results/{job_id}/manifest",
        "candidates_csv": f"/api/results/{job_id}/candidates.csv",
        "bundle": f"/api/results/{job_id}/bundle.zip",
    }
    results["links"] = links

    # ---- inputs -------------------------------------------------------------
    bundle.write("inputs/requirements.md", document, role="requirements")
    ref_paths = {}
    for name, data in (reference_files or {}).items():
        rel = f"inputs/reference/{Path(name).name}"
        bundle.write(rel, data, role="reference_file")
        ref_paths[name] = rel
    results["inputs"] = {
        "requirements": {"path": "inputs/requirements.md",
                         "sha256": _sha256(document.encode("utf-8"))},
        "reference_files": [
            {"name": n, "path": r, "sha256": _sha256(d if isinstance(d, bytes) else d.encode("utf-8"))}
            for n, (r, d) in
            ((n, (p, reference_files[n])) for n, p in ref_paths.items())
        ],
    }

    # ---- parse ---------------------------------------------------------------
    emit("parse", 0.0, "parsing requirements")
    provider = opts["parser"]
    client = None
    if provider == "devin":
        try:
            from coldplate.devin_client import DevinClient, DevinSettings
            client = DevinClient(DevinSettings.from_env())
        except Exception as e:  # DevinConfigError or missing env
            results["status"] = "failed"
            results["warnings"].append(f"Devin client not configured: {e}")
            _finish(bundle, results, job_id, t_created)
            return results
    on_poll = None
    if progress:
        def on_poll(body):
            sd = body.get("status_detail") or body.get("status") or "?"
            emit("parse", 0.1, f"Devin session {body.get('session_id', '?')} running ({sd})")
    pr = parse_requirements(
        document, reference_files=reference_files or {}, provider=provider,
        prompt_version=opts["prompt_version"], use_cache=opts["use_cache"],
        client=client, recording=opts.get("recording"),
        manual=opts.get("manual"), on_progress=on_poll,
    )
    interp = {
        "provider": provider, "provider_label": pr.provider_label,
        "api_version": getattr(pr, "api_version", None),
        "prompt_version": pr.prompt_version, "cached": pr.cached,
        "devin_calls": pr.devin_calls,
        "sessions": [s.model_dump() if hasattr(s, "model_dump") else s for s in pr.sessions],
        "repaired": pr.repaired,
        "warnings": pr.warnings,
        "missing_essential": pr.missing_essential,
        "reported_missing": [m.model_dump() for m in (getattr(pr.spec, "reported_missing", []) if pr.spec else [])],
        "unsupported_assumptions": pr.unsupported_assumptions,
        "evidence_misses": pr.evidence_misses,
    }
    results["interpretation"] = interp
    bundle.write("interpretation.json", interp, role="interpretation")
    if on_interpretation:
        first = (interp.get("sessions") or [{}])[0]
        on_interpretation({
            "provider": provider,
            "provider_label": interp.get("provider_label"),
            "session_id": first.get("session_id"),
            "session_url": first.get("url"),
            "cached": interp.get("cached"),
            "devin_calls": interp.get("devin_calls"),
            "api_version": interp.get("api_version"),
        })

    spec = pr.spec
    if spec is not None:
        spec_dict = spec.model_dump()
        results["normalized_inputs"] = spec_dict
        results["assumptions"] = [a.model_dump() for a in spec.assumptions]
        bundle.write("inputs/normalized_spec.json", spec_dict, role="normalized_spec")

    if pr.status in ("needs_input", "unsupported", "failed") or spec is None:
        results["status"] = pr.status if pr.status != "ok" else "failed"
        if pr.error:
            results["warnings"].append(pr.error)
        results["missing_essential"] = pr.missing_essential
        _finish(bundle, results, job_id, t_created)
        return results

    inputs = spec.engine_inputs()
    results["requirement_conflicts"] = requirement_conflicts(inputs)

    # ---- objectives/constraints metadata -------------------------------------
    crit = inputs.critical_type
    results["objectives"] = {
        "x": {"key": "pressure_drop_kPa", "label": "Pressure drop", "direction": "minimize"},
        "y": {"key": "critical_temperature_C",
              "label": f"Hottest {crit} temperature" if crit else "Peak component temperature",
              "component_type": crit},
        "alternatives": [{"key": "worst_margin_K", "label": "Worst hard-limit margin"}],
    }
    margin = inputs.temperature_margin_K or 0.0
    cons = []
    for tp, lim in (inputs.limits_C or {}).items():
        cons.append({
            "key": f"{tp.lower()}_max_C", "label": f"{tp} max temperature",
            "unit": "degC", "limit": lim,
            "target": (inputs.targets_C or {}).get(tp),
            "stretch": (inputs.stretch_C or {}).get(tp),
            "acceptance": lim - margin, "kind": "temperature",
        })
    if inputs.dp_hard_Pa is not None:
        cons.append({
            "key": "pressure_drop_kPa", "label": "Pressure drop", "unit": "kPa",
            "limit": inputs.dp_hard_Pa / 1000.0,
            "target": (inputs.dp_target_Pa or 0) / 1000.0,
            "stretch": (inputs.dp_stretch_Pa or 0) / 1000.0,
            "acceptance": (inputs.dp_acceptance_Pa or 0) / 1000.0,
            "kind": "pressure",
        })
    if inputs.max_outlet_rise_K is not None:
        cons.append({
            "key": "coolant_rise_K", "label": "Coolant outlet rise", "unit": "K",
            "limit": inputs.max_outlet_rise_K, "kind": "rise",
        })
    results["constraints"] = cons

    # ---- optimize ------------------------------------------------------------
    emit("evaluate", 0.2, "evaluating candidates")
    def ev_progress(k, n_, msg):
        frac = 0.2 + 0.5 * (k / max(n_, 1))
        emit("evaluate", frac, msg)

    optimization_started = time.perf_counter()
    opt = run_optimization(
        inputs, pop_size=opts["pop_size"], n_gen=opts["n_gen"], seed=opts["seed"],
        grid_mm=opts["grid_mm"], progress=ev_progress,
        workers=opts.get("workers"), on_candidate=on_candidate,
    )
    optimization_seconds = time.perf_counter() - optimization_started
    emit("select", 0.7, "selecting designs")
    selected = select_designs(opt)
    if on_selected:
        on_selected(selected)

    cand_rows = []
    zone_labels = []
    for c in opt.candidates:
        for lab in c.params.get("channel_width_by_zone_mm", {}):
            if lab not in zone_labels:
                zone_labels.append(lab)
    for c in opt.candidates:
        rec = {
            "candidate_id": c.candidate_id,
            "design_id": f"{job_id}-{c.candidate_id}",
            "is_baseline": c.candidate_id == "c000",
            "params": c.params, "metrics": c.metrics,
            "feasible": c.feasible, "violations": c.violations,
            "targets_met": c.targets_met, "worst_case": c.worst_case,
            "pareto": {"nondominated": c.nondominated, "rank": c.rank},
            "selected": c.selected, "role": c.role,
            "status_label": c.status_label,
        }
        cand_rows.append(rec)
    results["candidates"] = cand_rows
    results["rejected_candidates"] = opt.rejected
    results["pareto"] = {
        "front_candidate_ids": opt.front_candidate_ids,
        "n_evaluated": opt.n_evaluated, "n_feasible": opt.n_feasible,
        "n_nondominated": opt.n_nondominated,
        "all_infeasible": opt.all_infeasible,
    }
    results["optimizer"] = opt.algorithm
    results["warnings"].extend(opt.warnings)

    # ---- candidates.csv -------------------------------------------------------
    header = (["candidate_id", "design_id", "is_baseline", "selected", "role",
               "status_label", "feasible", "nondominated", "pareto_rank",
               "fin_thickness_mm", "fin_height_mm"] +
              [f"channel_width_{z}_mm" for z in zone_labels] +
              ["n_channels", "pressure_drop_kPa", "critical_temperature_C",
               "hbm_max_C", "hbm_hottest", "gpu_max_C", "gpu_hottest",
               "coolant_outlet_C", "coolant_rise_K", "pumping_power_W",
               "worst_margin_K", "worst_case_margin_ok", "violations"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for c, rec in zip(opt.candidates, cand_rows):
        m = c.metrics
        w.writerow([
            c.candidate_id, rec["design_id"], rec["is_baseline"], c.selected,
            c.role, c.status_label, c.feasible, c.nondominated, c.rank,
            c.params.get("fin_thickness_mm"), c.params.get("fin_height_mm"),
            *[c.params.get("channel_width_by_zone_mm", {}).get(z) for z in zone_labels],
            c.params.get("n_channels"), m["pressure_drop_kPa"],
            m["critical_temperature_C"], m["hbm_max_C"], m["hbm_hottest"],
            m["gpu_max_C"], m["gpu_hottest"], m["coolant_outlet_C"],
            m["coolant_rise_K"], m["pumping_power_W"], m["worst_margin_K"],
            c.worst_case.get("margin_ok"),
            ";".join(
                f"{v['constraint']}{':' + v['component'] if v.get('component') else ''} "
                f"{v['value']:.3g}>{v['limit']:.3g}"
                for v in c.violations),
        ])
    bundle.write("candidates.csv", buf.getvalue(), role="candidates_csv")

    # ---- export every Pareto design; sweep only selected designs --------------
    cad_files_by_candidate = {}
    cad_check_by_candidate = {}
    cad_candidates = {
        candidate.candidate_id: candidate
        for candidate in opt.candidates if candidate.nondominated
    }
    cad_candidates.update({candidate.candidate_id: candidate for candidate in selected})
    cad_export_started = time.perf_counter()
    for cid, candidate in sorted(
        cad_candidates.items(), key=lambda item: int(item[0][1:])
    ):
        did = f"{job_id}-{cid}"
        rec = next(row for row in cand_rows if row["candidate_id"] == cid)
        if not opts["export_cad"]:
            continue
        emit("export", 0.7, f"exporting CAD {did}")
        try:
            art = export_design(
                candidate.nominal_eval.layout,
                str(job_dir / "designs" / did),
                name=did,
            )
            file_refs = {}
            for role, path, manifest_role in (
                ("step", Path(art.step_path), "cad_step"),
                ("stl", Path(art.stl_path), "cad_stl"),
                ("glb", Path(art.glb_path), "cad_glb"),
            ):
                rel = f"designs/{did}/{path.name}"
                entry = bundle.write(
                    rel, path.read_bytes(), role=manifest_role, design_id=did
                )
                file_refs[role] = {
                    "url": f"/api/results/{job_id}/files/{rel}",
                    "path": entry["path"],
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                }
            file_refs["glb"]["convention"] = art.glb_convention
            cad_files_by_candidate[cid] = file_refs
            cad_check_by_candidate[cid] = {
                "bbox_mm": list(art.bbox_mm), "volume_mm3": art.volume_mm3,
                "model_volume_mm3": art.model_volume_mm3,
                "volume_rel_err": abs(art.volume_mm3 - art.model_volume_mm3)
                / art.model_volume_mm3,
                "fin_count": art.fin_count, "valid": art.is_valid,
            }
            rec["files"] = file_refs
            if on_cad:
                on_cad(did, {
                    "step": Path(art.step_path).read_bytes(),
                    "stl": Path(art.stl_path).read_bytes(),
                    "glb": Path(art.glb_path).read_bytes(),
                })
        except Exception as exc:
            rec["cad_error"] = str(exc)
    cad_export_seconds = time.perf_counter() - cad_export_started

    sel_docs = []
    n_sel = len(selected)
    for k, c in enumerate(selected):
        did = f"{job_id}-{c.candidate_id}"
        base_frac = 0.7 + 0.3 * (k / max(n_sel, 1))
        if opts["sweep"]:
            emit("sweep", base_frac, f"sweeping {did}")
            sw = sweep_design(inputs, c.design, grid_mm=opts["grid_mm"])
        else:
            sw = None
        emit("export", base_frac + 0.2 / max(n_sel, 1), f"exporting fields {did}")
        layout = c.nominal_eval.layout
        ddir = f"designs/{did}"
        files = {}
        if c.candidate_id in cad_files_by_candidate:
            files.update(cad_files_by_candidate[c.candidate_id])
            files["step"] = _file_ref(
                did, {"path": files["step"]["path"], "bytes": files["step"]["bytes"],
                      "sha256": files["step"]["sha256"]}, "cad?format=step"
            )
            files["stl"] = _file_ref(
                did, {"path": files["stl"]["path"], "bytes": files["stl"]["bytes"],
                      "sha256": files["stl"]["sha256"]}, "cad?format=stl"
            )
            files["glb"] = {
                **_file_ref(
                    did, {"path": files["glb"]["path"], "bytes": files["glb"]["bytes"],
                          "sha256": files["glb"]["sha256"]}, "cad?format=glb"
                ),
                "convention": cad_files_by_candidate[c.candidate_id]["glb"]["convention"],
            }
        ev_fields = evaluate(inputs, c.design, grid_mm=opts["grid_mm"], want_fields=True)
        fe = bundle.write(f"{ddir}/fields.json",
                          {"schema_version": "coldplate-fields/1", **ev_fields.fields},
                          role="fields", design_id=did)
        files["fields"] = _file_ref(did, fe, "fields")
        geo = {"schema_version": "coldplate-geometry/1",
               **layout.to_dict(),
               "plate": {"length_x_mm": inputs.plate_lx_mm,
                          "width_y_mm": inputs.plate_ly_mm,
                          "base_thickness_mm": inputs.base_thickness_mm},
               "fin_region": {"x0_mm": inputs.fin_region_x0_mm,
                              "y0_mm": inputs.fin_region_y0_mm,
                              "lx_mm": inputs.fin_region_lx_mm,
                              "ly_mm": inputs.fin_region_ly_mm}}
        ge = bundle.write(f"{ddir}/geometry.json", geo, role="geometry", design_id=did)
        files["geometry"] = _file_ref(did, ge, "geometry")

        ddoc = {
            **_cand_doc(rec := next(r for r in cand_rows if r["design_id"] == did)),
            "role_label": (c.role or "").replace("_", " "),
            "sweep": {"flows_Lpm": sw["flows_Lpm"],
                      "inlet_temperatures_C": sw["inlet_temperatures_C"],
                      "points": sw["points"]} if sw else None,
            "acceptance": sw["acceptance"] if sw else None,
            "files": files,
            "cad_check": cad_check_by_candidate.get(c.candidate_id),
        }
        sel_docs.append(ddoc)
        bundle.write(f"{ddir}/design.json", ddoc, role="design", design_id=did)
    results["selected_designs"] = sel_docs

    # ---- simulation metadata ---------------------------------------------------
    results["simulation"] = {
        **SIMULATION_METADATA,
        "nominal": {"flow_Lpm": inputs.flow_Lpm, "inlet_C": inputs.inlet_C},
        "grid_mm": opts["grid_mm"],
        "power_map_applied": spec.power_map.applied if spec.power_map else None,
        "n_candidates": opt.n_evaluated, "n_rejected": len(opt.rejected),
        "seed": opts["seed"],
        "runtimes_s": {
            "optimization": round(optimization_seconds, 3),
            "cad_export": round(cad_export_seconds, 3),
            "total": round(time.perf_counter() - t0, 3),
        },
    }
    results["status"] = "completed"
    _finish(bundle, results, job_id, t_created)
    return results


def _file_ref(did: str, entry: dict, api_tail: str) -> dict:
    return {"url": f"/api/designs/{did}/{api_tail}", "path": entry["path"],
            "bytes": entry["bytes"], "sha256": entry["sha256"]}


def _cand_doc(rec: dict) -> dict:
    return rec


def _finish(bundle: _Bundle, results: dict, job_id: str, created: str) -> None:
    results["completed_at"] = _now()
    bundle.write("results.json", results, role="results")
    bundle.write("manifest.json", bundle.manifest(), role="manifest")

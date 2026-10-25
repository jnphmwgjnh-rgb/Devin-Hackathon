"""Candidate generation, parallel evaluation, Pareto selection, sweeps."""

from __future__ import annotations

import math
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence
from types import SimpleNamespace

import numpy as np
import pymoo
from pymoo.core.callback import Callback
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.problem import Problem
from pymoo.optimize import minimize
from pymoo.operators.crossover.sbx import SBX
from pymoo.operators.mutation.pm import PM
from pymoo.operators.repair.rounding import RoundingRepair
from pymoo.operators.sampling.rnd import IntegerRandomSampling

from coldplate.evaluate import bulk_temperature_rise, evaluate
from coldplate.geometry import (
    ChannelDesign, FinZone, GeometryError, baseline_design,
    build_layout, derive_zone_bands,
)
from coldplate.spec import EngineInputs

_STEP_MM = 0.05


def default_worker_count() -> int:
    return min(8, os.cpu_count() or 1)


def _levels(inputs: EngineInputs, assumptions_out: Optional[list] = None) -> list[float]:
    lo = inputs.min_feature_mm
    hi = inputs.max_feature_mm
    if lo is None or hi is None:
        if assumptions_out is not None:
            assumptions_out.append(
                "manufacturing feature bounds missing; used default 0.1-1.0 mm "
                "candidate levels"
            )
        lo, hi = 0.1, 1.0
    n = int(round((hi - lo) / _STEP_MM)) + 1
    return [round(lo + i * _STEP_MM, 4) for i in range(max(n, 1))]


def generate_candidates(
    inputs: EngineInputs,
    index_vectors: Iterable[Sequence[int]],
    *,
    candidate_ids: Optional[Sequence[str]] = None,
    assumptions_out: Optional[list] = None,
) -> tuple[list[ChannelDesign], list[dict]]:
    """Build designs from integer level indices; return designs and rejections."""
    levels = _levels(inputs, assumptions_out)
    bands = derive_zone_bands(inputs, inputs.flow_axis)
    labels = []
    for label, _, _ in bands:
        if label not in labels:
            labels.append(label)
    d = 1 + len(labels)
    openings = dict(
        inlet_opening_mm=inputs.inlet_opening_mm,
        outlet_opening_mm=inputs.outlet_opening_mm,
    )
    H = inputs.fin_height_max_mm  # fins must touch the gasket; no bypass modelled

    designs: list[ChannelDesign] = []
    rejected: list[dict] = []
    vectors = [tuple(int(i) for i in row) for row in index_vectors]
    ids = list(candidate_ids) if candidate_ids is not None else [
        f"c{i + 1:03d}" for i in range(len(vectors))
    ]
    if len(ids) != len(vectors):
        raise ValueError("candidate_ids must match index_vectors")

    for idx, cid in zip(vectors, ids):
        if len(idx) != d or any(i < 0 or i >= len(levels) for i in idx):
            raise ValueError(f"invalid design index vector {idx!r}; expected {d} indices")
        t = levels[idx[0]]
        widths = [levels[i] for i in idx[1:]]
        zones = tuple(
            FinZone(label, v0, v1, t, widths[labels.index(label)])
            for label, v0, v1 in bands
        )
        d_ = ChannelDesign(
            design_id=cid, arrangement=inputs.arrangement,
            flow_axis=inputs.flow_axis, fin_height_mm=H, zones=zones,
            **openings,
        )
        try:
            build_layout(inputs, d_)
        except GeometryError as e:
            rejected.append({
                "candidate_id": cid,
                "params": {
                    "fin_thickness_mm": t,
                    "channel_width_by_zone_mm": dict(zip(labels, widths)),
                },
                "reason": str(e),
            })
        else:
            designs.append(d_)
    return designs, rejected


@dataclass
class CandidateEval:
    candidate_id: str
    design: ChannelDesign
    params: dict
    metrics: dict
    feasible: bool
    violations: list
    targets_met: dict
    worst_case: dict
    nondominated: bool = False
    rank: int = 0
    selected: bool = False
    role: Optional[str] = None
    status_label: str = "dominated"
    nominal_eval: object = None


@dataclass
class OptimizationResult:
    candidates: list
    rejected: list
    n_evaluated: int
    n_feasible: int
    n_nondominated: int
    front_candidate_ids: list
    all_infeasible: bool
    critical_type: Optional[str]
    seed: int
    warnings: list = field(default_factory=list)
    algorithm: dict = field(default_factory=dict)


def _violations(inputs: EngineInputs, ev) -> list:
    """One entry per offending component; keys = metric keys, metric units."""
    out = []
    for c in ev.thermal.components:
        hard = (inputs.limits_C or {}).get(c["type"])
        if hard is not None and c["max_C"] > hard:
            out.append({"constraint": f"{c['type'].lower()}_max_C",
                        "component": c["name"],
                        "value": round(c["max_C"], 3), "limit": hard})
    if inputs.dp_hard_Pa is not None and ev.hydraulics.dP_total_Pa > inputs.dp_hard_Pa:
        out.append({"constraint": "pressure_drop_kPa", "component": None,
                    "value": round(ev.hydraulics.dP_total_Pa / 1000.0, 3),
                    "limit": inputs.dp_hard_Pa / 1000.0})
    if inputs.max_outlet_rise_K is not None and ev.thermal.rise_K > inputs.max_outlet_rise_K:
        out.append({"constraint": "coolant_rise_K", "component": None,
                    "value": round(ev.thermal.rise_K, 3),
                    "limit": inputs.max_outlet_rise_K})
    return out


def _metrics(inputs: EngineInputs, ev, critical_type) -> dict:
    t = ev.thermal
    comps = {c["name"]: {"type": c["type"], "max_C": c["max_C"], "mean_C": c["mean_C"],
                         "R_th_K_per_W": c["R_th_K_per_W"], "power_W": c["power_W"]}
             for c in t.components}
    if critical_type:
        crit_vals = [c for c in t.components if c["type"] == critical_type]
        crit_T = max(c["max_C"] for c in crit_vals) if crit_vals else t.critical_max_C
    else:
        crit_T = t.critical_max_C
    gpus = {c["name"]: c["max_C"] for c in t.components if c["type"] == "GPU"}
    hbms = {c["name"]: c["max_C"] for c in t.components if c["type"] == "HBM"}
    margins = {c["name"]: (inputs.limits_C.get(c["type"]) - c["max_C"])
               for c in t.components if inputs.limits_C.get(c["type"]) is not None}
    worst_name = min(margins, key=margins.get) if margins else None
    return {
        "critical_temperature_C": crit_T,
        "hbm_max_C": max(hbms.values()) if hbms else None,
        "hbm_hottest": max(hbms, key=hbms.get) if hbms else None,
        "gpu_max_C": max(gpus.values()) if gpus else None,
        "gpu_hottest": max(gpus, key=gpus.get) if gpus else None,
        "pressure_drop_kPa": ev.hydraulics.dP_total_Pa / 1000.0,
        "pumping_power_W": ev.hydraulics.pumping_power_W,
        "coolant_outlet_C": t.outlet_C,
        "coolant_rise_K": t.rise_K,
        "worst_margin_K": margins.get(worst_name),
        "worst_margin_component": worst_name,
        "components": comps,
        "energy_residual": t.energy_residual,
    }


def _dominates(a: tuple, b: tuple) -> bool:
    return (a[0] <= b[0] and a[1] <= b[1]) and (a[0] < b[0] or a[1] < b[1])


def _constraint_values(inputs: EngineInputs, ev) -> list[float]:
    values = []
    for component in ev.thermal.components:
        limit = (inputs.limits_C or {}).get(component["type"])
        if limit is not None:
            values.append((component["max_C"] - limit) / limit)
    if inputs.dp_hard_Pa is not None:
        values.append((ev.hydraulics.dP_total_Pa - inputs.dp_hard_Pa) / inputs.dp_hard_Pa)
    if inputs.max_outlet_rise_K is not None:
        values.append((ev.thermal.rise_K - inputs.max_outlet_rise_K)
                      / inputs.max_outlet_rise_K)
    return values or [-1.0]


def _constraint_count(inputs: EngineInputs) -> int:
    count = sum(
        1 for source in inputs.sources
        if (inputs.limits_C or {}).get(source.type) is not None
    )
    count += int(inputs.dp_hard_Pa is not None)
    count += int(inputs.max_outlet_rise_K is not None)
    return max(count, 1)


def _evaluate_candidate_worker(
    inputs: EngineInputs, design: ChannelDesign, grid_mm: float
) -> tuple[CandidateEval, list[float]]:
    ev = evaluate(inputs, design, grid_mm=grid_mm)
    violations = _violations(inputs, ev)
    flows = list(inputs.flow_sweep_Lpm or [])
    wc_flow = min(flows) if flows else None
    inlets = list(inputs.inlet_sweep_C or [])
    wc_inlet = max(inlets) if inlets else None
    if wc_flow is not None and wc_flow != inputs.flow_Lpm:
        ev_wc = evaluate(inputs, design, flow_Lpm=wc_flow, grid_mm=grid_mm)
    else:
        ev_wc = ev
    inlet_delta = wc_inlet - inputs.inlet_C if wc_inlet is not None else 0.0
    wc = {
        "flow_Lpm": wc_flow if wc_flow is not None else inputs.flow_Lpm,
        "inlet_C": wc_inlet if wc_inlet is not None else inputs.inlet_C,
        "gpu_max_C": (ev_wc.thermal.gpu_max_C or 0) + inlet_delta,
        "hbm_max_C": (ev_wc.thermal.hbm_max_C or 0) + inlet_delta,
        "coolant_rise_K": ev_wc.thermal.rise_K,
        "margin_ok": True,
        "failures": [],
    }
    for component in ev_wc.thermal.components:
        limit = (inputs.limits_C or {}).get(component["type"])
        if limit is not None and component["max_C"] + inlet_delta > limit:
            wc["failures"].append({
                "component": component["name"],
                "max_C": component["max_C"] + inlet_delta,
                "limit_C": limit,
            })
    wc["margin_ok"] = not wc["failures"]
    targets = {}
    for component in ev.thermal.components:
        target = (inputs.targets_C or {}).get(component["type"])
        if target is not None:
            targets[component["name"]] = component["max_C"] <= target
    candidate = CandidateEval(
        candidate_id=design.design_id,
        design=design,
        params=_params(inputs, design),
        metrics=_metrics(inputs, ev, inputs.critical_type),
        feasible=not violations,
        violations=violations,
        targets_met=targets,
        worst_case=wc,
        nominal_eval=SimpleNamespace(layout=ev.layout),
    )
    return candidate, _constraint_values(inputs, ev)


class _OptimizationProblem(Problem):
    def __init__(
        self,
        inputs: EngineInputs,
        *,
        pop_size: int,
        n_gen: int,
        seed: int,
        grid_mm: float,
        levels: list[float],
        workers: int,
        progress: Optional[Callable],
        on_candidate: Optional[Callable],
        baseline: Optional[CandidateEval],
        baseline_rejection: Optional[dict],
    ):
        self.inputs = inputs
        self.pop_size = pop_size
        self.n_gen = n_gen
        self.seed = seed
        self.grid_mm = grid_mm
        self.levels = levels
        self.workers = workers
        self.progress = progress
        self.on_candidate = on_candidate
        self.candidates = [baseline] if baseline is not None else []
        self.rejected = [baseline_rejection] if baseline_rejection is not None else []
        self.index_cache: dict[tuple[int, ...], tuple[Optional[CandidateEval], list, list]] = {}
        self.next_candidate_number = 1
        self.search_evaluated = 0
        self.generation = 0
        self.executor = None
        labels = []
        for label, _, _ in derive_zone_bands(inputs, inputs.flow_axis):
            if label not in labels:
                labels.append(label)
        n_variables = 1 + len(labels)
        self.n_constraints = _constraint_count(inputs)
        super().__init__(
            n_var=n_variables,
            n_obj=2,
            n_ieq_constr=self.n_constraints,
            xl=np.zeros(n_variables, dtype=int),
            xu=np.full(n_variables, len(levels) - 1, dtype=int),
            vtype=int,
        )

    def _candidate_id(self) -> str:
        cid = f"c{self.next_candidate_number:03d}"
        self.next_candidate_number += 1
        return cid

    def _evaluate(self, X, out, *args, **kwargs):
        X = np.rint(np.asarray(X)).astype(int)
        rows = [tuple(int(i) for i in row) for row in X]
        new_rows = []
        new_seen = set()
        for key in rows:
            if key not in self.index_cache and key not in new_seen:
                new_seen.add(key)
                new_rows.append((key, self._candidate_id()))

        if new_rows:
            ids = [cid for _, cid in new_rows]
            designs, rejected = generate_candidates(
                self.inputs, [key for key, _ in new_rows], candidate_ids=ids
            )
            designs_by_id = {design.design_id: design for design in designs}
            rejected_by_id = {entry["candidate_id"]: entry for entry in rejected}
            self.rejected.extend(rejected)
            pending = []
            for key, cid in new_rows:
                if cid in rejected_by_id:
                    large_g = [1e6] * self.n_constraints
                    self.index_cache[key] = (None, [1e12, 1e12], large_g)
                else:
                    pending.append((key, designs_by_id[cid]))

            def record(key, candidate, constraints):
                objective = [
                    candidate.metrics["pressure_drop_kPa"],
                    candidate.metrics["critical_temperature_C"],
                ]
                if len(constraints) != self.n_constraints:
                    raise RuntimeError(
                        f"expected {self.n_constraints} constraints, got {len(constraints)}"
                    )
                self.index_cache[key] = (candidate, objective, constraints)
                self.candidates.append(candidate)
                self.search_evaluated += 1
                if self.on_candidate:
                    self.on_candidate(candidate)
                if self.progress:
                    planned = self.pop_size * self.n_gen
                    self.progress(
                        self.search_evaluated,
                        planned,
                        f"evaluated {self.search_evaluated}/{planned}",
                    )

            if self.executor is None:
                for key, design in pending:
                    candidate, constraints = _evaluate_candidate_worker(
                        self.inputs, design, self.grid_mm
                    )
                    record(key, candidate, constraints)
            else:
                futures = {
                    self.executor.submit(
                        _evaluate_candidate_worker, self.inputs, design, self.grid_mm
                    ): key
                    for key, design in pending
                }
                for future in as_completed(futures):
                    candidate, constraints = future.result()
                    record(futures[future], candidate, constraints)

        objectives = []
        constraints = []
        for key in rows:
            _candidate, objective, g = self.index_cache[key]
            objectives.append(objective)
            constraints.append(g)
        out["F"] = np.asarray(objectives, dtype=float)
        out["G"] = np.asarray(constraints, dtype=float)

def _nondominated_candidates(candidates: list[CandidateEval]) -> list[CandidateEval]:
    feasible = [candidate for candidate in candidates if candidate.feasible]
    front = [
        candidate for candidate in feasible
        if not any(
            other is not candidate
            and _dominates(
                (other.metrics["pressure_drop_kPa"], other.metrics["critical_temperature_C"]),
                (candidate.metrics["pressure_drop_kPa"], candidate.metrics["critical_temperature_C"]),
            )
            for other in feasible
        )
    ]
    return sorted(
        front,
        key=lambda candidate: (
            candidate.metrics["pressure_drop_kPa"], candidate.candidate_id
        ),
    )


class _GenerationCallback(Callback):
    def __init__(
        self,
        problem: _OptimizationProblem,
        callback: Optional[Callable],
        n_gen: int,
    ):
        super().__init__()
        self.problem = problem
        self.callback = callback
        self.n_gen = n_gen

    def notify(self, algorithm):
        if self.callback is None:
            return
        front = _nondominated_candidates(self.problem.candidates)
        self.callback(
            algorithm.n_gen,
            self.n_gen,
            len(self.problem.candidates),
            [candidate.candidate_id for candidate in front],
        )


def run_optimization(
    inputs: EngineInputs,
    *,
    pop_size: int = 24,
    n_gen: int = 8,
    seed: int = 42,
    grid_mm: float = 0.5,
    workers: Optional[int] = None,
    progress: Optional[Callable] = None,
    on_candidate: Optional[Callable] = None,
    on_generation: Optional[Callable] = None,
) -> OptimizationResult:
    """Run integer NSGA-II; progress excludes the separately evaluated baseline.

    The progress denominator is the planned population budget ``pop_size*n_gen``.
    Duplicate genotypes and geometry rejections can make actual simulations fewer.
    """
    if pop_size < 2:
        raise ValueError("pop_size must be at least 2")
    if n_gen < 1:
        raise ValueError("n_gen must be at least 1")
    if workers is not None and workers < 1:
        raise ValueError("workers must be positive")

    assumptions: list[str] = []
    levels = _levels(inputs, assumptions)
    baseline = baseline_design(inputs)
    baseline_candidate = None
    baseline_rejection = None
    try:
        build_layout(inputs, baseline)
    except GeometryError as exc:
        baseline_rejection = {
            "candidate_id": "c000",
            "params": {"baseline": True},
            "reason": str(exc),
        }
    else:
        baseline_candidate, _ = _evaluate_candidate_worker(inputs, baseline, grid_mm)
        baseline_candidate.candidate_id = "c000"
        if on_candidate:
            on_candidate(baseline_candidate)

    context = _OptimizationProblem(
        inputs,
        pop_size=pop_size,
        n_gen=n_gen,
        seed=seed,
        grid_mm=grid_mm,
        levels=levels,
        workers=workers if workers is not None else default_worker_count(),
        progress=progress,
        on_candidate=on_candidate,
        baseline=baseline_candidate,
        baseline_rejection=baseline_rejection,
    )
    if workers != 1:
        context.executor = ProcessPoolExecutor(
            max_workers=context.workers,
            mp_context=multiprocessing.get_context("spawn"),
        )
    algorithm = NSGA2(
        pop_size=pop_size,
        sampling=IntegerRandomSampling(),
        crossover=SBX(prob=0.9, eta=15, vtype=float, repair=RoundingRepair()),
        mutation=PM(eta=20, vtype=float, repair=RoundingRepair()),
        eliminate_duplicates=True,
    )
    try:
        minimize(
            context,
            algorithm,
            termination=("n_gen", n_gen),
            seed=seed,
            verbose=False,
            save_history=False,
            copy_algorithm=False,
            callback=_GenerationCallback(context, on_generation, n_gen),
        )
    finally:
        if context.executor is not None:
            context.executor.shutdown(wait=True)

    cands = sorted(
        context.candidates, key=lambda candidate: int(candidate.candidate_id[1:])
    )
    rejected = sorted(
        context.rejected, key=lambda item: int(item["candidate_id"][1:])
    )
    crit_type = inputs.critical_type
    feas = [candidate for candidate in cands if candidate.feasible]
    xy = [(c.metrics["pressure_drop_kPa"], c.metrics["critical_temperature_C"]) for c in feas]
    front = [i for i in range(len(feas))
             if not any(i != j and _dominates(xy[j], xy[i]) for j in range(len(feas)))]
    for i in front:
        feas[i].nondominated = True
    rank = 0
    remaining = set(range(len(feas)))
    while remaining:
        front = [i for i in remaining
                 if not any(j != i and j in remaining and _dominates(xy[j], xy[i])
                            for j in remaining)]
        for i in front:
            feas[i].rank = rank
        remaining -= set(front)
        rank += 1
    for c in cands:
        c.status_label = (
            "nondominated" if c.nondominated
            else ("dominated" if c.feasible else "infeasible")
        )
    front_ids = [candidate.candidate_id for candidate in _nondominated_candidates(cands)]
    return OptimizationResult(
        candidates=cands, rejected=rejected,
        n_evaluated=len(cands), n_feasible=len(feas),
        n_nondominated=len(front_ids), front_candidate_ids=front_ids,
        all_infeasible=not feas, critical_type=crit_type, seed=seed,
        warnings=assumptions,
        algorithm={
            "name": f"NSGA-II (pymoo {pymoo.__version__})",
            "pop_size": pop_size,
            "n_gen": n_gen,
            "seed": seed,
            "n_evaluated": len(cands),
            "n_geometry_rejected": len(rejected),
        },
    )


def _params(inputs: EngineInputs, des: ChannelDesign) -> dict:
    layout = build_layout(inputs, des)
    widths = {}
    for ch in layout.channels:
        widths.setdefault(ch["zone"], ch["width_mm"])
    return {
        "arrangement": des.arrangement, "flow_axis": des.flow_axis,
        "fin_height_mm": des.fin_height_mm,
        "fin_thickness_mm": des.zones[0].fin_thickness_mm if des.zones else None,
        "channel_width_by_zone_mm": {z.label: z.channel_width_mm for z in des.zones},
        "zones": [
            {"label": z.label, "v_start_mm": z.v_start_mm, "v_end_mm": z.v_end_mm,
             "channel_width_mm": z.channel_width_mm,
             "actual_channel_width_mm": widths.get(z.label),
             "fin_thickness_mm": z.fin_thickness_mm,
             "n_channels": sum(1 for c in layout.channels if c["zone_index"] == zi)}
            for zi, z in enumerate(des.zones)
        ],
        "n_channels": len(layout.channels), "n_fins": len(layout.fins),
        "inlet_opening_mm": des.inlet_opening_mm,
        "outlet_opening_mm": des.outlet_opening_mm,
    }


def select_designs(result: OptimizationResult) -> list:
    """Pick pressure, cooling and utopia-knee designs from the feasible front."""
    front = _nondominated_candidates(result.candidates)
    for candidate in result.candidates:
        candidate.selected = False
        candidate.role = None
    if not front:
        return []

    pressure = min(
        front,
        key=lambda c: (c.metrics["pressure_drop_kPa"], c.candidate_id),
    )
    cooling = min(
        front,
        key=lambda c: (c.metrics["critical_temperature_C"], c.candidate_id),
    )
    xs = np.array([c.metrics["pressure_drop_kPa"] for c in front], dtype=float)
    ys = np.array([c.metrics["critical_temperature_C"] for c in front], dtype=float)
    xn = (xs - xs.min()) / max(xs.max() - xs.min(), 1e-9)
    yn = (ys - ys.min()) / max(ys.max() - ys.min(), 1e-9)
    balanced = front[int(np.argmin(np.sqrt(xn**2 + yn**2)))]

    role_picks = (
        (pressure, "pressure_focused"),
        (cooling, "cooling_focused"),
        (balanced, "balanced"),
    )
    picked = []
    for candidate, role in role_picks:
        if candidate not in picked:
            candidate.selected = True
            candidate.role = role
            picked.append(candidate)
    return sorted(
        picked,
        key=lambda c: (c.metrics["pressure_drop_kPa"], c.candidate_id),
    )


def sweep_design(inputs: EngineInputs, design: ChannelDesign, grid_mm: float = 0.5) -> dict:
    """Sweeps per the contract: flows x inlet temperatures, acceptance block."""
    nominal_f = inputs.flow_Lpm
    nominal_t = inputs.inlet_C
    flows = sorted(set([nominal_f] + list(inputs.flow_sweep_Lpm or [])))
    inlets = sorted(set([nominal_t] + list(inputs.inlet_sweep_C or [])))
    P = sum(s.power_W for s in inputs.sources)
    points = []
    for f in flows:
        ev = evaluate(inputs, design, flow_Lpm=f, grid_mm=grid_mm)
        rise = bulk_temperature_rise(P, f, inputs.rho_kg_m3, inputs.cp_J_kgK)
        by_in = []
        for tin in inlets:
            shift = tin - nominal_t
            gpu = (ev.thermal.gpu_max_C or 0) + shift
            hbm = (ev.thermal.hbm_max_C or 0) + shift
            crit = max(
                [c["max_C"] + shift for c in ev.thermal.components
                 if inputs.critical_type is None or c["type"] == inputs.critical_type]
                or [0]
            )
            ok = True
            for c in ev.thermal.components:
                lim = (inputs.limits_C or {}).get(c["type"])
                if lim is not None and c["max_C"] + shift > lim:
                    ok = False
            by_in.append({
                "inlet_C": tin, "gpu_max_C": gpu, "hbm_max_C": hbm,
                "critical_temperature_C": crit, "hard_ok": ok,
                "margin_ok": ok,
            })
        points.append({
            "flow_Lpm": f,
            "pressure_drop_kPa": ev.hydraulics.dP_total_Pa / 1000.0,
            "pumping_power_W": ev.hydraulics.pumping_power_W,
            "coolant_rise_K": rise,
            "rise_ok": (inputs.max_outlet_rise_K is None
                        or rise <= inputs.max_outlet_rise_K),
            "by_inlet": by_in,
        })
    # acceptance
    margin = inputs.temperature_margin_K or 0.0
    thresholds = {tp: lim - margin for tp, lim in (inputs.limits_C or {}).items()}
    # document rule: worst sweep point = highest inlet temp, LOWEST flow
    worst_p, worst_bi = points[0], points[0]["by_inlet"][-1]
    failures = []
    for tp, thr in thresholds.items():
        key = f"{tp.lower()}_max_C"
        if worst_bi.get(key, worst_bi.get("critical_temperature_C", 0)) > thr:
            failures.append({"constraint": key, "limit_C": thr})
    hi = points[-1]
    dp_hi_ok = (inputs.dp_acceptance_Pa is None
                or hi["pressure_drop_kPa"] * 1000 <= inputs.dp_acceptance_Pa)
    rise_fails = [p["flow_Lpm"] for p in points if not p["rise_ok"]]
    acc = {
        "worst_point": {
            "flow_Lpm": worst_p["flow_Lpm"], "inlet_C": worst_bi["inlet_C"],
            "gpu_max_C": worst_bi["gpu_max_C"], "hbm_max_C": worst_bi["hbm_max_C"],
            "thresholds_C": thresholds, "failures": failures,
            "ok": not failures,
        },
        "pressure_at_highest_flow": {
            "flow_Lpm": hi["flow_Lpm"], "dP_kPa": hi["pressure_drop_kPa"],
            "limit_kPa": (inputs.dp_acceptance_Pa or 0) / 1000.0,
            "ok": dp_hi_ok,
        },
        "outlet_rise": {
            "by_flow_K": {str(p["flow_Lpm"]): p["coolant_rise_K"] for p in points},
            "limit_K": inputs.max_outlet_rise_K,
            "geometry_independent": True,
        },
    }
    acc["accepted"] = not failures and dp_hi_ok and not rise_fails
    if acc["accepted"]:
        acc["summary"] = "Accepted: all sweep points satisfy the stated rules."
    else:
        parts = []
        if rise_fails:
            parts.append(
                f"outlet rise exceeds {inputs.max_outlet_rise_K:g} K at "
                f"{' and '.join(f'{f:g}' for f in rise_fails)} L/min for any "
                f"geometry (energy balance)"
            )
        if failures:
            parts.append("worst-point temperature margin violated")
        else:
            parts.append("worst-point temperature margin OK")
        parts.append(
            f"ΔP at {hi['flow_Lpm']:g} L/min {hi['pressure_drop_kPa']:.2f} kPa "
            f"{'≤' if dp_hi_ok else '>'} "
            f"{((inputs.dp_acceptance_Pa or 0) / 1000.0):g} kPa"
        )
        acc["summary"] = "Not accepted under the stated rules: " + "; ".join(parts) + "."
    return {"flows_Lpm": flows, "inlet_temperatures_C": inlets,
            "points": points, "acceptance": acc}


def requirement_conflicts(inputs: EngineInputs) -> list[str]:
    """Geometry-independent impossibilities from the bulk energy balance."""
    out = []
    if inputs.max_outlet_rise_K is None:
        return out
    P = sum(s.power_W for s in inputs.sources)
    flows = sorted(set([inputs.flow_Lpm] + list(inputs.flow_sweep_Lpm or [])))
    for f in flows:
        rise = bulk_temperature_rise(P, f, inputs.rho_kg_m3, inputs.cp_J_kgK)
        if rise > inputs.max_outlet_rise_K + 1e-9:
            f_min = P / (inputs.rho_kg_m3 * inputs.cp_J_kgK * inputs.max_outlet_rise_K) * 60e3
            out.append(
                f"At {f:g} L/min the bulk coolant rise is {rise:.2f} K, above the "
                f"{inputs.max_outlet_rise_K:g} K limit, for any fin geometry "
                f"(energy balance P/(rho*cp*Q)); minimum compliant flow is "
                f"{f_min:.2f} L/min."
            )
    return out

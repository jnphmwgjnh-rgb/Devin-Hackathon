"""NSGA-II optimizer behavior and exhaustive search-quality checks."""

import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from itertools import product, repeat

import numpy as np
import pytest
from pymoo.indicators.hv import HV

from coldplate.geometry import derive_zone_bands
from coldplate.optimize import (
    CandidateEval,
    _dominates,
    _evaluate_candidate_worker,
    _levels,
    generate_candidates,
    run_optimization,
    select_designs,
)
from tests.test_api_stack import inputs_A


def _signature(result):
    return [
        (
            candidate.candidate_id,
            candidate.params,
            candidate.metrics,
            candidate.feasible,
            candidate.violations,
        )
        for candidate in result.candidates
    ]


def test_seed_and_worker_count_determinism():
    inputs = inputs_A()
    options = {"pop_size": 6, "n_gen": 2, "seed": 42, "grid_mm": 1.0}
    serial_a = run_optimization(inputs, **options, workers=1)
    serial_b = run_optimization(inputs, **options, workers=1)
    parallel = run_optimization(inputs, **options, workers=4)

    assert _signature(serial_a) == _signature(serial_b)
    assert _signature(serial_a) == _signature(parallel)


def test_generation_callback_runs_after_each_generation():
    events = []
    progress = []
    result = run_optimization(
        inputs_A(),
        pop_size=4,
        n_gen=2,
        seed=42,
        grid_mm=1.0,
        workers=1,
        progress=lambda evaluated, planned, message: progress.append(
            (evaluated, planned, message)
        ),
        on_generation=lambda *event: events.append(event),
    )

    assert [event[0] for event in events] == [1, 2]
    assert all(event[1] == 2 for event in events)
    assert all(event[2] <= result.n_evaluated for event in events)
    assert set(events[-1][3]) <= {c.candidate_id for c in result.candidates}
    assert progress
    assert all(planned == 8 and 0 < evaluated <= planned
               for evaluated, planned, _ in progress)
    assert progress[-1][0] == result.n_evaluated - 1


def test_pareto_flags_cover_all_feasible_evaluations():
    result = run_optimization(
        inputs_A(), pop_size=6, n_gen=2, seed=42, grid_mm=1.0, workers=1
    )
    feasible = [candidate for candidate in result.candidates if candidate.feasible]
    assert feasible
    for candidate in result.candidates:
        dominators = [
            other for other in feasible
            if other is not candidate and _dominates(
                (
                    other.metrics["pressure_drop_kPa"],
                    other.metrics["critical_temperature_C"],
                ),
                (
                    candidate.metrics["pressure_drop_kPa"],
                    candidate.metrics["critical_temperature_C"],
                ),
            )
        ]
        if candidate.feasible:
            assert candidate.nondominated == (not dominators)
        else:
            assert not candidate.nondominated


def test_selection_roles_match_independent_front_calculation():
    points = [
        CandidateEval(
            candidate_id=cid,
            design=None,
            params={},
            metrics={
                "pressure_drop_kPa": pressure,
                "critical_temperature_C": temperature,
            },
            feasible=True,
            violations=[],
            targets_met={},
            worst_case={},
        )
        for cid, pressure, temperature in (
            ("c001", 1.0, 5.0),
            ("c002", 2.0, 3.0),
            ("c003", 5.0, 1.0),
            ("c004", 3.0, 4.0),
        )
    ]
    result = type("Result", (), {"candidates": points})()
    selected = select_designs(result)
    front = [
        candidate for candidate in points
        if not any(
            other is not candidate
            and _dominates(
                (
                    other.metrics["pressure_drop_kPa"],
                    other.metrics["critical_temperature_C"],
                ),
                (
                    candidate.metrics["pressure_drop_kPa"],
                    candidate.metrics["critical_temperature_C"],
                ),
            )
            for other in points
        )
    ]
    xs = np.array([candidate.metrics["pressure_drop_kPa"] for candidate in front])
    ys = np.array([candidate.metrics["critical_temperature_C"] for candidate in front])
    distances = np.hypot(
        (xs - xs.min()) / (xs.max() - xs.min()),
        (ys - ys.min()) / (ys.max() - ys.min()),
    )
    expected = {
        "pressure_focused": min(
            front, key=lambda c: (c.metrics["pressure_drop_kPa"], c.candidate_id)
        ).candidate_id,
        "cooling_focused": min(
            front, key=lambda c: (c.metrics["critical_temperature_C"], c.candidate_id)
        ).candidate_id,
        "balanced": front[int(np.argmin(distances))].candidate_id,
    }
    actual = {candidate.role: candidate.candidate_id for candidate in selected}

    assert len(front) == 3
    assert actual == expected
    assert all(candidate in front for candidate in selected)


def test_geometry_rejections_are_not_evaluated():
    inputs = replace(inputs_A(), fin_region_lx_mm=0.0)
    result = run_optimization(
        inputs, pop_size=4, n_gen=1, seed=42, grid_mm=1.0, workers=1
    )

    assert result.candidates == []
    assert result.n_evaluated == 0
    assert result.rejected
    assert all(entry["reason"] for entry in result.rejected)
    rejected_ids = {entry["candidate_id"] for entry in result.rejected}
    assert rejected_ids.isdisjoint(
        candidate.candidate_id for candidate in result.candidates
    )


@pytest.mark.slow
def test_nsga2_front_hypervolume_is_close_to_exhaustive():
    inputs = inputs_A()
    levels = _levels(inputs)
    labels = {label for label, _, _ in derive_zone_bands(inputs, inputs.flow_axis)}
    vectors = list(product(range(len(levels)), repeat=1 + len(labels)))
    design_ids = [f"e{i:04d}" for i in range(len(vectors))]
    start = time.perf_counter()
    designs, rejected = generate_candidates(
        inputs, vectors, candidate_ids=design_ids
    )
    with ProcessPoolExecutor(max_workers=4) as executor:
        exhaustive = [
            evaluated[0] for evaluated in executor.map(
                _evaluate_candidate_worker,
                repeat(inputs),
                designs,
                repeat(1.0),
                chunksize=4,
            )
        ]
    exhaustive_seconds = time.perf_counter() - start
    start = time.perf_counter()
    optimized = run_optimization(
        inputs, pop_size=24, n_gen=8, seed=42, grid_mm=1.0, workers=4
    )
    search_seconds = time.perf_counter() - start

    feasible = [candidate for candidate in exhaustive if candidate.feasible]
    exhaustive_front = [
        candidate for candidate in feasible
        if not any(
            other is not candidate
            and _dominates(
                (
                    other.metrics["pressure_drop_kPa"],
                    other.metrics["critical_temperature_C"],
                ),
                (
                    candidate.metrics["pressure_drop_kPa"],
                    candidate.metrics["critical_temperature_C"],
                ),
            )
            for other in feasible
        )
    ]
    search_front = [candidate for candidate in optimized.candidates if candidate.nondominated]
    exhaustive_xy = np.array([
        (candidate.metrics["pressure_drop_kPa"],
         candidate.metrics["critical_temperature_C"])
        for candidate in exhaustive_front
    ])
    search_xy = np.array([
        (candidate.metrics["pressure_drop_kPa"],
         candidate.metrics["critical_temperature_C"])
        for candidate in search_front
    ])
    reference = exhaustive_xy.max(axis=0) * 1.05
    true_hv = HV(ref_point=reference)(exhaustive_xy)
    search_hv = HV(ref_point=reference)(search_xy)
    ratio = search_hv / true_hv

    print(
        "HV quality: "
        f"ratio={ratio:.5f}, exhaustive_valid={len(designs)}, "
        f"exhaustive_rejected={len(rejected)}, "
        f"exhaustive_feasible={len(feasible)}, "
        f"exhaustive_front={len(exhaustive_front)}, "
        f"search_evaluated={optimized.n_evaluated}, "
        f"search_front={len(search_front)}, "
        f"exhaustive_seconds={exhaustive_seconds:.2f}, "
        f"search_seconds={search_seconds:.2f}"
    )
    assert ratio >= 0.95

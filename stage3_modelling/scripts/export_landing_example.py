#!/usr/bin/env python3
"""Export a real recorded A_canonical NSGA-II result for the landing chart."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "stage3_modelling"))

from coldplate.optimize import run_optimization, select_designs
from coldplate.parser import parse_requirements


FIXTURES = ROOT / "stage3_modelling" / "tests" / "fixtures"
RECORDING = FIXTURES / "devin_recordings" / "A_canonical.v2.json"
DOCUMENT = FIXTURES / "A_canonical.md"
REFERENCE = FIXTURES / "reference_files" / "power_map_W_per_mm2.csv"


def main() -> None:
    recording = json.loads(RECORDING.read_text(encoding="utf-8"))
    parsed = parse_requirements(
        DOCUMENT.read_text(encoding="utf-8"),
        reference_files={REFERENCE.name: REFERENCE.read_bytes()},
        provider="recorded",
        recording=recording,
        use_cache=False,
    )
    if parsed.spec is None or parsed.status != "ok":
        raise RuntimeError(f"Recorded A_canonical parse failed: {parsed.error}")

    result = run_optimization(parsed.spec.engine_inputs())
    selected = select_designs(result)
    roles = {candidate.candidate_id: candidate.role for candidate in selected}
    output = {
        "algorithm": result.algorithm,
        "n_evaluated": result.n_evaluated,
        "front_candidate_ids": result.front_candidate_ids,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "pressure_drop_kPa": candidate.metrics["pressure_drop_kPa"],
                "critical_temperature_C": candidate.metrics["critical_temperature_C"],
                "feasible": candidate.feasible,
                "nondominated": candidate.nondominated,
                "role": roles.get(candidate.candidate_id),
            }
            for candidate in result.candidates
        ],
    }
    target = ROOT / "landing" / "data" / "example_results.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target} ({len(result.candidates)} candidates, {result.n_nondominated} Pareto)")


if __name__ == "__main__":
    main()

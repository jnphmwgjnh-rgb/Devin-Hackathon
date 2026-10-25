"""ID-based CAD exports and Pareto artifact coverage."""

import json
import struct
from itertools import product
from pathlib import Path

import numpy as np
import pytest

from coldplate.cad import export_design
from coldplate.geometry import build_layout, derive_zone_bands
from coldplate.optimize import _levels, generate_candidates
from coldplate.pipeline import run_job
from tests.test_api_stack import DOCS, REF, inputs_A, rec


def _stl_triangles(path):
    data = Path(path).read_bytes()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 == len(data):
            triangles = []
            for index in range(count):
                values = struct.unpack_from("<12fH", data, 84 + index * 50)
                triangles.append(np.array(values[3:12], dtype=float).reshape(3, 3))
            return triangles

    vertices = []
    for line in data.decode("ascii", errors="ignore").splitlines():
        fields = line.strip().split()
        if fields and fields[0] == "vertex":
            vertices.append(tuple(float(value) for value in fields[1:4]))
    assert vertices and len(vertices) % 3 == 0
    return [np.asarray(vertices[i:i + 3], dtype=float) for i in range(0, len(vertices), 3)]


def _mesh_volume_and_closed_edges(triangles):
    edges = {}
    signed_volume = 0.0
    for triangle in triangles:
        vertices = [tuple(round(float(value), 5) for value in point) for point in triangle]
        for start, end in zip(vertices, vertices[1:] + vertices[:1]):
            edge = tuple(sorted((start, end)))
            edges[edge] = edges.get(edge, 0) + 1
        signed_volume += np.dot(
            triangle[0], np.cross(triangle[1], triangle[2])
        ) / 6.0
    assert edges and set(edges.values()) == {2}
    return abs(signed_volume)


@pytest.mark.slow
def test_step_stl_and_glb_exports_are_named_and_valid(tmp_path):
    import cadquery as cq

    inputs = inputs_A()
    levels = _levels(inputs)
    labels = {label for label, _, _ in derive_zone_bands(inputs, inputs.flow_axis)}
    vectors = list(product(range(len(levels)), repeat=1 + len(labels)))
    ids = [f"cad{i:04d}" for i in range(len(vectors))]
    designs, _ = generate_candidates(inputs, vectors, candidate_ids=ids)
    layouts = [(design, build_layout(inputs, design)) for design in designs]
    layouts.sort(key=lambda item: (len(item[1].fins), item[0].design_id))
    first_design, first_layout = layouts[0]
    first_volume = first_layout.solid_volume_mm3()
    second_design, second_layout = next(
        (design, layout) for design, layout in layouts[1:]
        if not np.isclose(layout.solid_volume_mm3(), first_volume, rtol=0, atol=1e-6)
    )

    artifacts = []
    for design, layout in ((first_design, first_layout), (second_design, second_layout)):
        design_id = f"job-{design.design_id}"
        artifact = export_design(layout, str(tmp_path / design_id), name=design_id)
        assert Path(artifact.step_path).name == f"{design_id}.step"
        assert Path(artifact.stl_path).name == f"{design_id}.stl"
        assert Path(artifact.glb_path).name == f"{design_id}.glb"
        assert all(Path(path).is_file() for path in (
            artifact.step_path, artifact.stl_path, artifact.glb_path
        ))

        imported = cq.importers.importStep(artifact.step_path).val()
        assert imported.isValid()
        assert len(imported.Solids()) == 1
        assert imported.Volume() > 0
        bounds = imported.BoundingBox()
        assert (bounds.xlen, bounds.ylen, bounds.zlen) == pytest.approx(
            (
                inputs.plate_lx_mm,
                inputs.plate_ly_mm,
                inputs.base_thickness_mm + design.fin_height_mm,
            ),
            abs=0.05,
        )

        triangles = _stl_triangles(artifact.stl_path)
        stl_volume = _mesh_volume_and_closed_edges(triangles)
        assert abs(stl_volume - imported.Volume()) / imported.Volume() <= 0.01
        artifacts.append(artifact)

    assert not np.isclose(
        artifacts[0].volume_mm3, artifacts[1].volume_mm3, rtol=0, atol=1e-6
    )


@pytest.mark.slow
def test_recorded_pipeline_exports_every_pareto_design(tmp_path):
    job_dir = tmp_path / "recorded-job"
    result = run_job(
        job_dir,
        DOCS["A_canonical"],
        REF,
        {
            "parser": "recorded",
            "recording": rec("A_canonical"),
            "prompt_version": "v2",
            "use_cache": False,
            "pop_size": 12,
            "n_gen": 4,
            "seed": 42,
            "grid_mm": 1.0,
            "sweep": False,
            "export_cad": True,
        },
    )

    saved = json.loads((job_dir / "results.json").read_text(encoding="utf-8"))
    assert saved["optimizer"]["name"].startswith("NSGA-II (pymoo ")
    assert saved["optimizer"]["pop_size"] == 12
    assert saved["optimizer"]["n_gen"] == 4
    assert result["optimizer"] == saved["optimizer"]
    assert {design["role"] for design in saved["selected_designs"]} == {
        "pressure_focused", "cooling_focused", "balanced"
    }

    nondominated = [
        candidate for candidate in saved["candidates"]
        if candidate["pareto"]["nondominated"]
    ]
    assert nondominated
    for candidate in nondominated:
        design_id = candidate["design_id"]
        for extension in ("step", "stl"):
            ref = candidate["files"][extension]
            artifact_path = job_dir / ref["path"]
            assert artifact_path.is_file()
            assert artifact_path.name == f"{design_id}.{extension}"

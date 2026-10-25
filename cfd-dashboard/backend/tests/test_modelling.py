import json
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import AppConfig
from app.main import create_app
from app import modelling

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "stage3_modelling" / "tests" / "fixtures" / "devin_recordings"


def _wait_finished(deadline=15.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        state = modelling.status()
        if state["status"] in ("completed", "failed"):
            return state
        time.sleep(0.05)
    raise AssertionError("campaign did not finish")


def test_candidate_id_campaign_and_cad_writes(tmp_path, monkeypatch):
    import coldplate.pipeline

    recording = json.loads(
        (FIXTURES / "A_canonical.v2.json").read_text(encoding="utf-8")
    )
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    app = create_app(AppConfig(runs_dir=runs_dir))
    monkeypatch.setattr(
        coldplate.pipeline,
        "run_job",
        lambda *args, **kwargs: _fake_job(kwargs, args[0].name),
    )
    modelling._set(status="idle", error=None)

    with TestClient(app) as client:
        intake = client.post(
            "/api/intake/external",
            json={"json_text": "```json\n" + json.dumps(recording["structured_output"]) + "\n```"},
        ).json()
        assert intake["status"] == "needs_input"
        answered = client.post(
            f'/api/intake/{intake["intake_id"]}/answers',
            json={"answers": {
                "coolant.flow_rate_sweep": {"value": [2, 2.5, 3], "unit": "L/min"}
            }},
        )
        assert answered.status_code == 200
        assert answered.json()["status"] == "ok"
        started = client.post(
            "/api/modelling/start", json={"intake_id": intake["intake_id"]}
        )
        assert started.status_code == 200
        final = _wait_finished()
        assert final["status"] == "completed"
        assert final["stage"] == "done"
        assert final["planned"] == 24 * 8 + 1
        assert (runs_dir / "c014" / f'{final["campaign_id"]}-c014.stl').is_file()
        client.app.state.store.scan()
        assert any(run["id"] == "c014" for run in client.app.state.store.list_runs())


def _fake_job(callbacks, job_id):
    candidate = SimpleNamespace(
        candidate_id="c014",
        params={
            "fin_thickness_mm": 0.3,
            "channel_width_by_zone_mm": {"GPU": 0.4, "HBM": 0.45},
            "fin_height_mm": 2.0,
            "n_channels": 20,
        },
        metrics={
            "pressure_drop_kPa": 1.0,
            "critical_temperature_C": 65.0,
            "hbm_max_C": 60.0,
            "gpu_max_C": 65.0,
            "hbm_hottest": "H1",
            "gpu_hottest": "GPU1",
            "coolant_rise_K": 8.0,
            "coolant_outlet_C": 40.0,
            "energy_residual": 1e-12,
        },
        feasible=True,
        violations=[],
        role="pressure_focused",
    )
    callbacks["on_candidate"](candidate)
    callbacks["on_generation"](1, 8, 1, ["c014"])
    callbacks["on_selected"]([candidate])
    callbacks["on_cad"](
        "c014", f"{job_id}-c014",
        {"step": b"step", "stl": b"stl", "glb": b"glb"}, None,
    )
    return {"status": "completed"}

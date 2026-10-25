import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AppConfig
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "stage3_modelling" / "tests" / "fixtures" / "devin_recordings"


def _recording(name):
    return json.loads((FIXTURES / f"{name}.v2.json").read_text(encoding="utf-8"))


def _pasted(name):
    return json.dumps(_recording(name)["structured_output"])


def test_prompt_and_external_validation_errors(tmp_path):
    with TestClient(create_app(AppConfig(runs_dir=tmp_path / "runs"))) as client:
        prompt = client.get("/api/intake/prompt").json()["prompt"]
        assert "ONE ```json fenced block" in prompt
        bad = client.post(
            "/api/intake/external", json={"json_text": "```json\n{\"x\": }\n```"}
        )
        assert bad.status_code == 422
        assert "line" in bad.json()["errors"][0]
        good = client.post(
            "/api/intake/external",
            json={"json_text": "```json\n" + _pasted("A_canonical") + "\n```"},
        )
        assert good.status_code == 200
        assert good.json()["status"] in {"ok", "needs_input"}


def test_answer_flow_and_incomplete_start_rejection(tmp_path):
    with TestClient(create_app(AppConfig(runs_dir=tmp_path / "runs"))) as client:
        response = client.post(
            "/api/intake/external",
            json={"json_text": "```json\n" + _pasted("E_missing_flow") + "\n```"},
        )
        assert response.status_code == 200
        initial = response.json()
        assert initial["status"] == "needs_input"
        assert client.post(
            "/api/modelling/start", json={"intake_id": initial["intake_id"]}
        ).status_code == 409

        answered = client.post(
            f"/api/intake/{initial['intake_id']}/answers",
            json={"answers": {"coolant.flow_rate": {"value": 2, "unit": "L/min"}}},
        )
        assert answered.status_code == 200
        assert answered.json()["status"] == "ok"
        assert answered.json()["questions"] == []


def test_cad_download_uses_design_id_filename(tmp_path):
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "c014"
    run_dir.mkdir(parents=True)
    design_id = "j20260401T120000-c014"
    (run_dir / "model_result.json").write_text(json.dumps({
        "status": "completed",
        "design_id": design_id,
        "metrics": {"pressure_drop_pa": 1000, "max_temperature_k": 330},
        "params": {},
        "feasible": True,
    }), encoding="utf-8")
    (run_dir / f"{design_id}.step").write_bytes(b"step")

    with TestClient(create_app(AppConfig(runs_dir=runs_dir))) as client:
        response = client.get("/api/runs/c014/cad?format=step")
        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            f'attachment; filename="{design_id}.step"'
        )

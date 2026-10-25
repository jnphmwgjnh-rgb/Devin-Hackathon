from fastapi.testclient import TestClient

from app.config import AppConfig
from app.main import create_app
from mock.generate_runs import write_complete_case


def test_runs_api_and_geometry(tmp_path):
    runs_dir = tmp_path / "runs"
    case = runs_dir / "run_0007"
    write_complete_case(case, index=7, seed=23)
    with TestClient(create_app(AppConfig(runs_dir=runs_dir))) as client:
        response = client.get("/api/runs")
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["total"] == 1
        run = payload["runs"][0]
        assert run["id"] == "run_0007"
        assert run["index"] == 7
        assert run["status"] == "completed"
        assert set(run) == {
            "id",
            "index",
            "status",
            "params",
            "metrics",
            "solver",
            "mesh_cells",
            "iteration",
            "end_iteration",
            "started_at",
            "finished_at",
            "runtime_s",
            "converged",
            "on_pareto",
            "error",
            "has_geometry",
            "details",
        }

        details = client.get("/api/runs/run_0007")
        assert details.status_code == 200
        assert details.json()["residuals"]["iterations"]
        geometry = client.get("/api/runs/run_0007/cad?format=stl")
        assert geometry.status_code == 404

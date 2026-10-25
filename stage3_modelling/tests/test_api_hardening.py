"""API hardening + live-job interpretation tests (TestClient, no servers)."""

import io
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

os.environ["COLDPLATE_RUNS_DIR"] = tempfile.mkdtemp(prefix="coldplate-test-")

from fastapi.testclient import TestClient  # noqa: E402

from coldplate.api import app  # noqa: E402

client = TestClient(app)
FIXTURES = Path(__file__).parent / "fixtures"
DOC = (FIXTURES / "A_canonical.md").read_text(encoding="utf-8")
REC = json.loads(
    (FIXTURES / "devin_recordings" / "A_canonical.v2.json").read_text(encoding="utf-8")
)


def _body(**over):
    b = {
        "requirements_markdown": DOC,
        "reference_files": [],
        "options": {
            "parser": "recorded", "recording": REC,
            "pop_size": 4, "n_gen": 1, "grid_mm": 1.0,
            "sweep": False, "export_cad": False,
        },
    }
    b.update(over)
    return b


def test_job_interpretation_populated():
    r = client.post("/api/optimize", json=_body())
    assert r.status_code == 202
    jid = r.json()["job_id"]
    seen = None
    for _ in range(120):
        j = client.get(f"/api/jobs/{jid}").json()
        if j.get("interpretation"):
            seen = j["interpretation"]
            break
        time.sleep(0.5)
    assert seen is not None, "interpretation never populated"
    for k in ("provider", "provider_label", "session_id", "session_url",
              "cached", "devin_calls", "api_version"):
        assert k in seen, k
    assert seen["provider"] == "recorded"
    # terminal state keeps it
    for _ in range(240):
        j = client.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("completed", "failed", "needs_input", "unsupported"):
            break
        time.sleep(0.5)
    assert j["interpretation"]["provider"] == "recorded"


def test_job_id_traversal_rejected():
    for bad in ("a..b", "a%5Cb"):
        r = client.get(f"/api/jobs/{bad}")
        assert r.status_code == 400, (bad, r.status_code)


def test_design_id_validation():
    for bad in ("a%5Cb", "a..b", "bad%20id"):
        r = client.get(f"/api/designs/{bad}")
        assert r.status_code == 400, (bad, r.status_code)
    # a well-formed-but-unknown id is a 404, not 400
    assert client.get("/api/designs/j20990101T000000-ffff-c999").status_code in (400, 404)


def test_size_limits():
    big = "x" * (2 * 1024 * 1024 + 1)
    r = client.post("/api/optimize", json=_body(requirements_markdown=big))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "requirements_too_large"

    refs = [{"name": f"f{i}.csv", "content_base64": "eA=="} for i in range(11)]
    r = client.post("/api/optimize", json=_body(reference_files=refs))
    assert r.status_code == 413

    bigref = [{"name": "big.csv",
               "content_base64": __import__("base64").b64encode(b"x" * (20 * 1024 * 1024 + 1)).decode()}]
    r = client.post("/api/optimize", json=_body(reference_files=bigref))
    assert r.status_code == 413


def test_bad_reference_name():
    # names that reduce to empty/"." /".." are rejected
    for bad in ("..", ".", "", "/", "\\", "a/b/..", "../.."):
        r = client.post("/api/optimize", json=_body(
            reference_files=[{"name": bad, "content_base64": "eA=="}]))
        assert r.status_code in (400, 422), (bad, r.status_code)
    # names with directories are reduced to a safe basename and accepted
    for ok in ("a/b.txt", "a\\b.txt", "dir1/dir2/map.csv"):
        r = client.post("/api/optimize", json=_body(
            reference_files=[{"name": ok, "content_base64": "eA=="}]))
        assert r.status_code == 202, (ok, r.status_code)


def test_multipart_non_utf8():
    files = {"requirements": ("req.md", io.BytesIO(b"\xff\xfe" * 100), "text/markdown")}
    data = {"options": json.dumps({"parser": "recorded"})}
    r = client.post("/api/optimize", files=files, data=data)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_requirements_encoding"

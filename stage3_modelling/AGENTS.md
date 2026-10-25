# AGENTS

Python 3.12 engine + React/TS Pareto harness. Branch `feat/coldplate-engine`.

## Commands

- Setup: `uv venv --python 3.12 .venv` then `uv pip install -e ".[dev]" --python ../.venv/Scripts/python.exe`
- Tests (offline): `../.venv/Scripts/python.exe -m pytest -q`
- Live e2e (spends Devin quota): `../.venv/Scripts/python.exe -m pytest -m live tests/test_e2e_live.py`
- API: `../.venv/Scripts/python.exe -m uvicorn coldplate.api:app --port 8000`
- App: `../.venv/bin/python run_app.py` from the repository root
- Frontend: `cd stage3_modelling/frontend && npm ci && npm run dev|build|test`
- Results schema: `../.venv/Scripts/python.exe scripts/export_results_schema.py`

## Rules

- NEVER read/print/commit `.env` (real Devin token). Tests must not need it.
- Tests replay committed recordings (`tests/fixtures/devin_recordings/`) or
  mock transports — no live Devin calls; `live` marker spends real quota.
- Do not commit or push without being asked.
- Contract for dashboard output is `docs/API_CONTRACT.md`; keep it in sync.
- `runs/` is git-ignored (job bundles, parse cache, scratch).

# Devin-Hackathon: ITherm 2027 cold-plate tools

| Component | Path | Purpose |
|---|---|---|
| Cold-plate optimization engine | `stage3_modelling/` (`coldplate/`, `frontend/`, `prompts/`, `tests/`) | Requirements markdown → Devin API → CAD → low-fidelity thermal/hydraulic model → Pareto results, API and dashboard bundle |
| Requirements intake chatbot | `stage1_intake/` | Gradio chatbot that collects and validates simulation inputs |
| Stage-2 straight-fin optimizer | `stage2_optimizer/` | Reduced-order straight-fin optimizer with CAD export |
| CFD dashboard prototype | `cfd-dashboard/` | Live CFD optimization dashboard (see `cfd-dashboard/README.md`) |

## Cold-plate optimization engine (stage 3)

> Part of the demo stack — see the root `README.md` for the one-command
> chat → dashboard demo (`python run_demo_stack.py`).

Hackathon engine that turns a requirements document into Pareto-optimal
cold-plate channel designs: **requirements.md → Devin API → structured inputs →
CAD → low-fidelity thermal/hydraulic model → Pareto front**.

All numeric results are **low-fidelity reduced-order estimates — not validated
3D CFD**. Every output carries that label.

### Setup

Prereqs: Python 3.12, `uv`, Node 24 / npm 11 (frontend only).

```bash
# Git Bash or PowerShell
uv venv --python 3.12 .venv
uv pip install -e "stage3_modelling[dev]" --python .venv/Scripts/python.exe
```

### Devin credentials

The engine's only AI provider is our Devin subscription, called from the backend.
Copy the template and fill it in. `.env` is git-ignored; never commit it.

```bash
cp .env.example .env         # PowerShell: Copy-Item .env.example .env
```

```
DEVIN_API_KEY=cog_...        # v3 Personal Access Token (preferred): Settings -> Devin API -> PATs
DEVIN_ORG_ID=org-...         # required for v3, shown at the top of the same page
DEVIN_MODE=                  # optional v3 agent mode, e.g. lite
DEVIN_MAX_ACU_LIMIT=         # optional per-session ACU cap
```

Legacy `apk_user_` keys fall back to the deprecated v1 API (no org id, no ACU
reporting). Verify the credentials without spending quota:

```bash
.venv/Scripts/python.exe stage3_modelling/scripts/devin_smoke_test.py --dry-run   # GET only, no session
.venv/Scripts/python.exe stage3_modelling/scripts/devin_smoke_test.py             # one minimal session
```

`docs/DEVIN_API.md` covers the endpoints used, the smoke-test record, and how API
sessions draw on the plan's quota (no separate API allowance). It also lists how
the engine limits consumption: a parse cache keyed by document hash, at most one
repair message, and sessions terminated as soon as a result is accepted.

### Run the API

```bash
# Git Bash / PowerShell
.venv/Scripts/python.exe -m uvicorn coldplate.api:app --port 8000
```

Endpoints per `docs/API_CONTRACT.md` (`POST /api/optimize`, `GET /api/jobs/{id}`,
`GET /api/results/{id}`, designs/files/schema/health).

### Run the frontend harness

```bash
cd stage3_modelling/frontend
npm ci
npm run dev        # http://localhost:5173, /api proxied to :8000
npm run build      # tsc + vite build
npm test           # vitest (buildParetoFigure on the real sample bundle)
```

In the harness you can upload a requirements `.md` (plus optional reference files)
to start a job, load an existing job by id, or open `public/sample-results.json`.
Clicking a point or one of the five design buttons selects that design and shows
its metrics, acceptance verdict and STEP/STL/GLB/fields links. This is a test
harness only. The production Pareto-front dashboard is a separate component that
consumes the API/bundle described below.

### Run the demo

```bash
# recorded replay (no Devin quota):
.venv/Scripts/python.exe stage3_modelling/scripts/run_demo.py --parser recorded
# live Devin parse:
.venv/Scripts/python.exe stage3_modelling/scripts/run_demo.py --parser devin
# refresh the frontend sample bundle:
.venv/Scripts/python.exe stage3_modelling/scripts/run_demo.py --parser recorded --export-sample
```

### Tests

```bash
cd stage3_modelling
../.venv/Scripts/python.exe -m pytest -q                 # offline, ~4 min
../.venv/Scripts/python.exe -m pytest -m live tests/test_e2e_live.py   # real Devin API, spends quota
.venv/Scripts/python.exe stage3_modelling/scripts/eval_prompts.py --provider recorded   # prompt eval vs fixtures
```

Prompt eval reports land in `prompts/requirements_parser/eval/`; see
`prompts/requirements_parser/EVAL.md`.

### Outputs for the dashboard team

Each job writes a self-contained bundle under `runs/jobs/<job_id>/`
(manifest.json, results.json, candidates.csv, interpretation.json, inputs/,
designs/<id>/{design.json,<id>.step,<id>.stl,<id>.glb,fields.json,geometry.json}).
Contract: `docs/API_CONTRACT.md`; results JSON Schema: `docs/results.schema.json`
(regenerate via `stage3_modelling/scripts/export_results_schema.py`). The reusable React/Plotly
Pareto component lives in `stage3_modelling/frontend/src/pareto/` and is exported from
`frontend/src/index.ts`.

### Model and limitations

`coldplate-rom/1` is a reduced-order model. Results are labelled low-fidelity everywhere
and are not validated 3D CFD.

- **Hydraulics:** parallel straight channels between inlet and outlet manifolds, with flow
  distribution solved per zone. Laminar friction uses Shah & London f·Re plus the Hagenbach
  developing-flow term; turbulent friction uses Petukhov. Manifold-turn and opening losses
  are lumped with K = 1.
- **Thermal:** 2-D finite-volume conduction in the copper base, coupled cell by cell to
  well-mixed coolant cells. Convection uses Shah & London laminar Nu with a developing-flow
  correction (Gnielinski when turbulent), and fin efficiency tanh(mH)/(mH). The chip-plane
  temperature includes the TIM. Energy is conserved to about 1e-14.
- **Checks reproduced:** a bulk coolant rise of 10.75 K at 2 L/min and 21.49 K at 1 L/min
  for 1492.8 W. The 1 L/min sweep point therefore breaks the 12 K outlet-rise rule for any
  fin geometry; the minimum compliant flow is 1.79 L/min.
- **Assumptions** (all reported in `results.json` → `assumptions` and `simulation`):
  - constant coolant properties;
  - adiabatic fin tips (gasket) and plate edges;
  - fins fill the 2 mm design-space height (no bypass);
  - the flow axis is taken as x when the text does not state it;
  - manifold opening widths default to the competition flow-path figure's proportions
    (11.06 mm edge inlets, 6.0 mm center outlet).
- **ITherm power map:** `tests/fixtures/reference_files/power_map_W_per_mm2.csv` is
  transcribed from the competition package figure (2 mm cells in watts; per-component sums
  check out exactly: 554.4 W per GPU, 48.0 W per HBM). It replaces the markdown's
  "relative levels / ~134 W/cm²" guess. The real map peaks at 200 W/cm² under the center
  outlet, and HBM rows are graded at 30–50 W/cm². The engine warns about both discrepancies
  whenever the file is supplied. `scripts/build_itherm_power_map.py` regenerates it.

### Reproducible demonstration

Live end-to-end (verified 2026-10-24). With the API running, `run_demo.py --parser devin
--no-cache` uploaded `tests/fixtures/A_canonical.md` plus the reference CSV. A fresh Devin
session (one call, no repair) interpreted the document, and the engine evaluated 48
candidates and exported five CAD designs in about 4.5 minutes in total.
`pytest -m live tests/test_e2e_live.py` passed (one fresh Devin session, 288 s).

Offline replay of the same document (recorded v2 Devin output, n=48, seed 42, grid 0.5 mm)
reproduces these numbers without spending quota:

- 48 evaluated, 32 feasible, 8 nondominated, ~200 s end-to-end
- selected: pressure_focused 0.15/0.5-0.4 mm, 0.37 kPa (HBM 61.2 °C H6, GPU 96.8 °C);
  balanced knee 0.1/0.2-0.15, 1.85 kPa (HBM 50.7, GPU 79.4);
  cooling_focused 0.4/0.1-0.1, 13.2 kPa (HBM 50.0, GPU 76.5)
- baseline 0.3/0.3: 0.91 kPa, GPU 89.3, HBM 56.7, rise 10.75 K
- `requirement_conflicts`: bulk rise exceeds 12 K at 1.0 L/min (21.49 K) for any
  geometry; minimum compliant flow 1.79 L/min

### Repository layout

```
coldplate/       engine: devin_client, parser, normalize, units, power_map,
                 spec, geometry, cad, hydraulics, thermal, evaluate,
                 optimize, pipeline, jobs, api, results_models
scripts/         devin_smoke_test, eval_prompts, run_demo, export_results_schema
prompts/         requirements_parser prompt versions + eval reports
tests/           offline suite (+ fixtures, golden extraction, Devin recordings)
frontend/        Vite+React+TS Pareto harness (reusable ParetoChart component)
docs/            API_CONTRACT.md, results.schema.json, DEVIN_API.md
runs/            git-ignored: jobs bundles, parse cache, scratch, smoke records
```

## Requirements intake chatbot (`stage1_intake/`)

`stage1_intake/` is a Gradio chatbot that collects and validates the inputs for a
cold-plate simulation. It writes `requirements_final.json` for a later optimizer
stage, which is not implemented here.

- Python owns the field list, unit conversion, validation and checks C1–C8; the LLM
  only phrases questions and extracts values via tool calls.
- Defaults are never confirmed silently: each group is confirmed explicitly, and
  warnings (e.g. C5 coolant rise) must be acknowledged before finalizing.
- Each session writes `sessions/<id>/requirements.json` and `notes.txt` (with the
  user's exact quotes) as it goes, and `requirements_final.json` once finalized.

### Run

```bash
cd stage1_intake
uv venv --python 3.11 .venv          # or: python3.11 -m venv .venv
uv pip install -r requirements.txt   # or: .venv/bin/pip install -r requirements.txt
cp .env.example .env                 # then paste your GEMINI_API_KEY into .env
.venv/bin/python app.py              # open http://127.0.0.1:7860
```

Without a key (or with `--provider mock`) it runs a scripted mock bot that only
understands "yes" and choice names.

### Demo script

1. Click **Load ITherm 2027 preset**.
2. `Yes those limits are right, but over 4 psi is a penalty, not disqualification.`
3. `Use 2 L/min nominal, 30 C inlet, sweep flow only at 30 C.`
4. `Actually make the TIM 0.08 cm2K/W.`
5. `Yes, use those.` for each remaining group.
6. Acknowledge the C5 coolant-rise warning, then `yes` on the final summary.

### Tests

```bash
cd stage1_intake
.venv/bin/python -m pytest -q                  # offline, mock LLM
RUN_LIVE=1 .venv/bin/python -m pytest -q tests/test_live_gemini.py   # real Gemini
```

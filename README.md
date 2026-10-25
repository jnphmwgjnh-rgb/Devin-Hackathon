# ColdShock AI cold-plate design tool

This repository contains a cold-plate intake flow, a discrete NSGA-II design
search, a reduced-order thermal/hydraulic model, CAD export, a landing page, and
a candidate dashboard. The thermal results are estimates from a **reduced-order
model (2-D conjugate conduction + duct-flow ΔP), not CFD**. They are not
validated 3-D CFD predictions.

## Run locally

From the repository root, run:

```bash
./.venv/bin/python run_app.py
```

The launcher builds the landing page when `npm` is available and `landing/dist`
does not exist, then serves the app on `http://127.0.0.1:8080`. Open `/` for
the landing page, `/intake` to provide requirements, and `/dashboard` to view
the candidate search. If the landing page has not been built and npm is absent,
`/` redirects to intake.

The **Use another AI** intake path requires no API key: copy the prompt, attach
one diagram or paste one requirements document into an external chat model, and
paste its single JSON block back into the app. The optional built-in text parser
uses the existing Devin parser and is disabled unless `DEVIN_API_KEY` is
configured in the server environment. Tests do not make live Devin calls.

## Model scope

The engine searches integer-indexed fin/channel geometry candidates with
NSGA-II, computes thermal and pressure-drop estimates, identifies the feasible
Pareto front, and exports STEP/STL/GLB for nondominated designs. It does not
mesh geometry or run a CFD solver. CAD export errors are recorded per design;
the dashboard only offers CAD for designs that were exported.

The landing-page Pareto chart is generated from the recorded A_canonical
reference package using the default optimizer settings. It is an actual engine
run, not a replay or placeholder chart.

See [stage3_modelling/docs/API_CONTRACT.md](stage3_modelling/docs/API_CONTRACT.md)
for the engine results and dashboard API contracts.

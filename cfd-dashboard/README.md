# CFD Optimization Dashboard

This prototype tracks multi-objective OpenFOAM heat-sink design runs, minimizing pressure drop and maximum chip temperature. As each case writes solver logs and function-object output, the dashboard updates its Pareto front, reports run status, and links each design to its parameters, residual history, metrics, and STL geometry. The demo generator creates synthetic cases in the same filesystem layout; the backend only reads those files, so it can also monitor real runs.

## Quick start

```bash
cd cfd-dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
python -m app --runs-dir ../runs --budget 40 --demo
```

Open <http://localhost:8000>. The demo uses two concurrent mock runs by default; `--demo-run-seconds 20` changes their approximate wall time. To monitor existing cases instead, omit `--demo`.

## Using real OpenFOAM cases

Run the dashboard with `--runs-dir` pointing at a directory containing one subdirectory per design. The parser reads solver and function-object output as it is written. A typical case looks like:

```text
runs/
└── run_0001/
    ├── params.json                         # optimizer parameters; optional to parse
    ├── system/
    │   └── controlDict                    # application and endTime
    ├── constant/
    │   └── triSurface/
    │       └── heatsink.stl               # optional geometry
    ├── log.chtMultiRegionFoam
    ├── log.checkMesh                      # optional mesh cell count
    └── postProcessing/
        ├── inletPressure/0/surfaceFieldValue.dat
        ├── outletPressure/0/surfaceFieldValue.dat
        ├── solidTemperature/0/fieldMinMax.dat
        └── solidTemperatureAvg/0/volFieldValue.dat
```

For a coupled solid/fluid case, add function objects like these to `system/controlDict`. Use your actual patch and region names. The object names below are the dashboard defaults.

```foam
functions
{
    inletPressure
    {
        type            surfaceFieldValue;
        libs            (fieldFunctionObjects);
        region          fluid;
        regionType      patch;
        name            inlet;
        operation       areaAverage;
        fields          (p);
        writeFields     false;
        writeControl    timeStep;
        writeInterval   10;
    }

    outletPressure
    {
        type            surfaceFieldValue;
        libs            (fieldFunctionObjects);
        region          fluid;
        regionType      patch;
        name            outlet;
        operation       areaAverage;
        fields          (p);
        writeFields     false;
        writeControl    timeStep;
        writeInterval   10;
    }

    solidTemperature
    {
        type            fieldMinMax;
        libs            (fieldFunctionObjects);
        region          solid;
        fields          (T);
        writeControl    timeStep;
        writeInterval   10;
    }

    solidTemperatureAvg
    {
        type            volFieldValue;
        libs            (fieldFunctionObjects);
        region          solid;
        operation       volAverage;
        fields          (T);
        writeFields     false;
        writeControl    timeStep;
        writeInterval   10;
    }
}
```

The two pressure objects are needed for `pressure_drop_pa`; `solidTemperature` and `solidTemperatureAvg` supply the max and mean temperature metrics. If a function object is absent, its metric remains `null`. `params.json` and the STL are optional to parsing and are only needed for parameter display and geometry viewing.

The optimizer should write a `params.json` object in each case directory, for example:

```json
{
  "fin_count": 24,
  "fin_height_mm": 18.5,
  "fin_thickness_mm": 1.2,
  "base_thickness_mm": 3.0,
  "inlet_velocity_m_s": 3.5
}
```

The backend detects a run as **pending** until a solver log appears, **running** while the log is active, **completed** when its last non-empty line is `End`, and **failed** if it contains `FOAM FATAL`. Start time is read from the OpenFOAM `Date`/`Time` banner when available; otherwise the log's first observed modification time is used. Output from the latest numeric function-object time directory is used, including the `postProcessing/<region>/<functionObject>/...` layout.

Function-object names can be changed with CLI options. For example, if your names are `inletP`, `outletP`, `chipTmax`, and `chipTmean`:

```bash
python -m app --runs-dir ../runs --fo-inlet inletP --fo-outlet outletP \
  --fo-tmax chipTmax --fo-tmean chipTmean
```

The flags are `--fo-inlet`, `--fo-outlet`, `--fo-tmax`, and `--fo-tmean`; their defaults are `inletPressure`, `outletPressure`, `solidTemperature`, and `solidTemperatureAvg`.

### Cluster use

Run the dashboard on a machine that can see the case directory, or periodically `rsync` completed and in-progress cases to a visible directory. To view a dashboard running on a cluster login node from your workstation, forward its port:

```bash
ssh -L 8000:localhost:8000 login-node
```

Then open <http://localhost:8000> locally. The frontend loads Plotly and three.js from the jsDelivr CDN, so the browser needs internet access.

## API

| Route | Purpose |
| --- | --- |
| `GET /api/runs` | Runs sorted by index and campaign summary |
| `GET /api/runs/{id}` | Run details and residual history (at most 500 points) |
| `GET /api/runs/{candidate_id}/cad?format=step\|stl\|glb` | Candidate CAD export, when available |
| `WS /ws` | Initial snapshot, then run/summary updates; completion and failure events |

WebSocket event types are `snapshot`, `run_updated`, `run_completed` (includes `joined_pareto`), and `run_failed`. Pareto membership is calculated from completed runs with both objective metrics.

## Project layout

```text
cfd-dashboard/
├── backend/
│   ├── app/          # API, filesystem watcher, parser, Pareto calculation
│   ├── mock/         # Progressive OpenFOAM-style demo case generator
│   ├── tests/
│   └── requirements.txt
├── frontend/         # Static HTML, JavaScript, and CSS
└── runs/             # Case directories (ignored by git)
```

Run the backend test suite from `cfd-dashboard/backend`:

```bash
pytest -q
```

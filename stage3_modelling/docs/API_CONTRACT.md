# Engine output contract (API + results bundle)

Audience: the separate Pareto-front dashboard, which ingests our data and models.
Everything here is produced by the FastAPI backend (`coldplate/api.py`) and written to
disk as a self-contained **results bundle** per job. All numeric results are
**low-fidelity reduced-order estimates, not validated 3D CFD**. Every results document
carries that label in `label`, and dashboards should display it.

Schema versions: results `coldplate-results/1`, manifest `coldplate-manifest/1`,
fields `coldplate-fields/1`, geometry `coldplate-geometry/1`. A breaking change bumps the suffix.

## Units and frames

- Lengths in **mm**, temperatures in **°C** (differences in **K**), flow in **L/min**,
  pressure drop in **kPa** in metrics (Pa inside `normalized_inputs`), power in **W**,
  heat flux in **W/cm²** in fields.
- **Plate frame:** origin at the base plate's lower-left corner, x to the right, y up, z up
  from the plate's bottom (chip/TIM side, z = 0). Fins occupy z ∈ [base thickness, base + fin height].
- The requirements document gives heat-source coordinates in its own frame (ITherm: the
  die region). `normalized_inputs.heat_sources[]` holds both `x0_mm/y0_mm` (document
  frame) and `x0_plate_mm/y0_plate_mm` (plate frame).
- Field arrays: row index = increasing y, column index = increasing x (plate frame); cell
  centres are listed in `grid.x_centers_mm` / `grid.y_centers_mm`. `null` means "not defined
  here" (e.g. coolant outside the fin region).
- CAD: STEP/STL in mm, plate frame, Z-up. The GLB preview's unit/axis convention is
  stated per file in `files.glb.convention`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/optimize` | Submit requirements Markdown (+ optional reference files); starts an async job; `202` |
| GET | `/api/jobs/{job_id}` | Job status, stage, progress 0–1, events, error |
| GET | `/api/results/{job_id}` | Full results document (`409` while still running) |
| GET | `/api/results/{job_id}/manifest` | File index of the bundle |
| GET | `/api/results/{job_id}/candidates.csv` | One row per evaluated candidate |
| GET | `/api/results/{job_id}/bundle.zip` | The whole bundle (offline ingestion) |
| GET | `/api/results/{job_id}/files/{path}` | Any file listed in the manifest |
| GET | `/api/designs/{design_id}` | One selected design: params, metrics, sweep, acceptance, file links |
| GET | `/api/designs/{design_id}/cad?format=step\|stl\|glb` | Download geometry (default `step`) |
| GET | `/api/designs/{design_id}/fields` | Temperature / velocity / heat-flux fields |
| GET | `/api/designs/{design_id}/geometry` | Exact fin/channel/opening layout used by CAD *and* physics |
| GET | `/api/schema/results` | JSON Schema of the results document |
| GET | `/api/health` | Liveness, engine version, whether Devin credentials are configured |

`design_id` = `{job_id}-{candidate_id}`, e.g. `j20261024T181502-4f2a-c017`.
Errors use `{"error": {"code": "...", "message": "...", "details": {...}}}` with an
appropriate HTTP status. CORS allows the origins in `COLDPLATE_CORS_ORIGINS`
(comma-separated; default: localhost on any port).

### POST /api/optimize

JSON body, or `multipart/form-data` with fields `requirements` (the .md file),
`reference_files` (0..n files) and `options` (JSON string):

```json
{
  "requirements_markdown": "# ITherm 2027 Cold Plate ...",
  "reference_files": [{"name": "power_map_W_per_mm2.csv", "content_base64": "..."}],
  "options": {
    "parser": "devin",          // "devin" (live, default) | "recorded" | "manual"
    "prompt_version": "v2",
    "use_cache": true,          // identical documents do not re-consume Devin quota
    "pop_size": 24,
    "n_gen": 8,
    "seed": 42,
    "grid_mm": 0.5,
    "sweep": true,
    "export_cad": true
  }
}
```

Response `202`: `{"job_id": "...", "status": "queued", "links": {"job": "...", "results": "..."}}`.

The optimizer is integer-coded NSGA-II. Its search evaluation budget is
`pop_size * n_gen`; baseline `c000` is evaluated separately. Duplicate genotypes
and geometry rejections reduce actual search simulations, so evaluation progress
is based on completed unique simulations and can finish below the planned budget.
`optimizer.n_evaluated` counts valid unique evaluations including the baseline.

### Job lifecycle (`GET /api/jobs/{id}`)

`status` ∈ `queued → parsing → evaluating → selecting → sweeping → exporting → completed`,
or a terminal `needs_input` (essential requirement missing: no designs are produced and
`missing_essential` lists the fields), `unsupported` (e.g. a serpentine or radial
arrangement) or `failed` (`error` set). Fields: `job_id, status, stage, progress,
message, events[{at, stage, message}], interpretation{provider_label, session_url, cached},
error, created_at, updated_at`.

## Results bundle (one directory per job)

```
manifest.json                      index: path, role, media_type, bytes, sha256, design_id
results.json                       results document (below)
candidates.csv                     tabular view of every evaluated candidate
interpretation.json                Devin parse record: sessions, prompt version, warnings
inputs/requirements.md             uploaded document, verbatim
inputs/reference/<name>            uploaded reference files, verbatim
inputs/normalized_spec.json        normalized inputs with per-field provenance
designs/<design_id>/design.json    params, metrics, sweep, acceptance, cad_check, file refs
designs/<design_id>/<design_id>.step  CAD (mm, plate frame)
designs/<design_id>/<design_id>.stl   CAD mesh
designs/<design_id>/<design_id>.glb   lightweight preview mesh
designs/<design_id>/fields.json    chip-plane temperature, velocity, heat-flux maps
designs/<design_id>/geometry.json  fin/channel/opening layout
```

STEP/STL/GLB files are exported for every feasible nondominated candidate and any
other selected design; filenames use the full design ID. `design.json`, fields,
geometry and sweeps are produced only for selected designs. An individual CAD
export failure is recorded as `cad_error` on that candidate and does not fail the job.

## Results document (`results.json`)

| Key | Content |
|---|---|
| `schema_version`, `job_id`, `engine_version`, `created_at`, `completed_at`, `status` | identity and outcome |
| `label` | `"Low-fidelity reduced-order thermohydraulic estimates — not validated 3D CFD"` |
| `inputs` | uploaded file names, bundle paths, sha256 |
| `interpretation` | `provider` (`devin`/`recorded`/`manual`), `provider_label` (display verbatim; non-Devin parses are marked RECORDED/MANUAL), `api_version`, `prompt_version`, `cached`, `devin_calls`, `sessions[{session_id, url, kind, status, acus_consumed, elapsed_s}]`, `repaired`, `warnings`, `missing_essential`, `reported_missing`, `unsupported_assumptions`, `evidence_misses` |
| `normalized_inputs` | the normalized spec. Every value is `{value, unit, source, evidence, note, original}`, with `source` ∈ `input`, `input_flagged_assumption`, `assumed`, `derived`, `engine_default`, `missing` |
| `assumptions` | `[{field, statement, origin: document\|devin\|engine}]`: everything not stated outright |
| `requirement_conflicts` | geometry-independent impossibilities, e.g. the 1.0 L/min sweep point exceeding the 12 K outlet-rise rule |
| `simulation` | model id/label, nominal condition, grid, power map applied, correlations, assumptions, candidate/rejected counts, seed, runtimes (`optimization`, `cad_export`, `total`) |
| `optimizer` | `{name, pop_size, n_gen, seed, n_evaluated, n_geometry_rejected}` |
| `objectives` | `x` = `{key: "pressure_drop_kPa", label, direction}`, `y` = `{key: "critical_temperature_C", label, component_type}` (ITherm: hottest HBM), `alternatives` (e.g. `worst_margin_K`) |
| `constraints` | `[{key, label, unit, limit, target, stretch, acceptance, kind}]` used for limit lines |
| `candidates[]` | every evaluated design (schema below) |
| `rejected_candidates[]` | `{candidate_id, params, reason}`: geometry that violated manufacturing limits and was never simulated |
| `pareto` | `front_candidate_ids` (feasible nondominated, sorted by x), `n_evaluated`, `n_feasible`, `n_nondominated`, `all_infeasible` |
| `selected_designs[]` | up to three distinct Pareto-role designs, ordered by pressure drop |
| `warnings`, `links` | free-text warnings; API links (`self`, `manifest`, `candidates_csv`, `bundle`) |

**Candidate** (`candidates[i]`):
`candidate_id`, `design_id`, `is_baseline`, `params`, `metrics`, `feasible`, `violations[{constraint, value, limit}]`,
`targets_met{...}`, `worst_case{flow_Lpm, inlet_C, gpu_max_C, hbm_max_C, coolant_rise_K, margin_ok, failures}`,
`pareto{nondominated, rank}`, `selected`, `role`, `status_label`, optional
`files{step, stl, glb}` and `cad_error`.
- `params`: `arrangement` (`split_edges_to_center` …), `flow_axis`, `fin_height_mm`, `fin_thickness_mm`,
  `channel_width_by_zone_mm{zone: w}`, `zones[{label, v_start_mm, v_end_mm, channel_width_mm,
  actual_channel_width_mm, fin_thickness_mm, n_channels}]`, `n_channels`, `n_fins`, `inlet_opening_mm`,
  `outlet_opening_mm`.
- `metrics`: `critical_temperature_C`, `hbm_max_C`, `hbm_hottest`, `gpu_max_C`, `gpu_hottest`,
  `pressure_drop_kPa`, `pumping_power_W`, `coolant_outlet_C`, `coolant_rise_K`, `worst_margin_K`,
  `worst_margin_component`, `components{name: {type, max_C, mean_C, R_th_K_per_W, power_W}}`, `energy_residual`.
- `status_label` ∈ `nondominated` (feasible and on the front), `dominated` (feasible, not on the front),
  `infeasible` (violates a hard limit at the nominal condition).

**Selected design** (`selected_designs[i]`): every candidate field, plus
`role` ∈ `pressure_focused | balanced | cooling_focused`, `role_label`,
and `dominated`/`feasible` flags. Roles are selected only from the feasible
nondominated front: minimum pressure drop, minimum critical temperature, and
minimum normalized Euclidean distance to the utopia point. If there are fewer
than three distinct role picks, fewer designs are returned; dominated or
infeasible candidates are not substituted.
Also included:
- `sweep{flows_Lpm, inlet_temperatures_C, points[{flow_Lpm, pressure_drop_kPa, pumping_power_W,
  coolant_rise_K, rise_ok, by_inlet[{inlet_C, gpu_max_C, hbm_max_C, critical_temperature_C,
  hard_ok, margin_ok}]}]}`
- `acceptance{worst_point, pressure_at_highest_flow, outlet_rise, accepted, summary}`
- `files{step, stl, glb, fields, geometry}`: each `{url, path, bytes, sha256}`; `glb` also carries `convention`
- `cad_check{bbox_mm, volume_mm3, model_volume_mm3, volume_rel_err, fin_count, valid}`

## Fields (`fields.json`, `coldplate-fields/1`)

`frame: "plate"`, `units`, `flow_axis`, `grid{dx_mm, nx, ny, x_centers_mm[], y_centers_mm[]}`,
`chip_temperature_C[ny][nx]` (heater/chip plane, includes the TIM), `plate_temperature_C`,
`fluid_temperature_C` (null outside the fin region), `velocity_u_m_s` (signed mean channel
velocity along `flow_axis`, null outside the fin region), `heat_flux_W_cm2`,
`outlines{plate, fin_region, heat_sources[{name, type, x0, y0, x1, y1}], inlet_openings[], outlet_openings[]}`.
Fields are computed at the nominal condition.

## Geometry (`geometry.json`, `coldplate-geometry/1`)

`design` (the `ChannelDesign`: arrangement, flow axis, fin height, zones, opening widths),
`plate`, `fin_region`, `fins[{x0, y0, x1, y1, z0, z1}]`, `channels[{index, zone, v_lo, v_hi, width}]`,
`openings{inlet[], outlet[]}`, derived numbers (`n_fins`, `n_channels`, `solid_volume_mm3`,
`open_area_fraction`). The thermal/hydraulic model and the CAD generator consume this same layout.

## candidates.csv

Header: `candidate_id, design_id, is_baseline, selected, role, status_label, feasible,
nondominated, pareto_rank, fin_thickness_mm, fin_height_mm, channel_width_<zone>_mm…,
n_channels, pressure_drop_kPa, critical_temperature_C, hbm_max_C, hbm_hottest, gpu_max_C,
gpu_hottest, coolant_outlet_C, coolant_rise_K, pumping_power_W, worst_margin_K,
worst_case_margin_ok, violations` (semicolon-joined).

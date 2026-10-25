import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Run:
    id: str
    index: int
    status: str
    params: dict
    metrics: dict
    solver: str | None
    mesh_cells: int | None
    iteration: int | None
    end_iteration: int | None
    started_at: str | None
    finished_at: str | None
    runtime_s: float | None
    converged: bool | None
    on_pareto: bool
    error: str | None
    has_geometry: bool
    residuals: dict = field(default_factory=lambda: {"iterations": [], "fields": {}})
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "status": self.status,
            "params": self.params,
            "metrics": self.metrics,
            "solver": self.solver,
            "mesh_cells": self.mesh_cells,
            "iteration": self.iteration,
            "end_iteration": self.end_iteration,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "runtime_s": self.runtime_s,
            "converged": self.converged,
            "on_pareto": self.on_pareto,
            "error": self.error,
            "has_geometry": self.has_geometry,
            "details": self.details,
        }


def _iso_timestamp(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _log_start_timestamp(log_text: str) -> str | None:
    date_match = re.search(r"^\s*Date\s*:\s*(.+?)\s*$", log_text, re.MULTILINE)
    time_match = re.search(r"^\s*Time\s*:\s*(\d{2}:\d{2}:\d{2})\s*$", log_text, re.MULTILINE)
    if not date_match or not time_match:
        return None
    try:
        local_start = datetime.strptime(
            f"{date_match.group(1)} {time_match.group(1)}", "%b %d %Y %H:%M:%S"
        ).astimezone()
    except ValueError:
        return None
    return local_start.astimezone(timezone.utc).isoformat()


def _tokens(line: str) -> list[str]:
    return re.findall(r"\([^)]*\)|\S+", line.strip())


def _read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    header = None
    rows = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return [], []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            candidate = _tokens(stripped.lstrip("#").strip())
            if candidate and candidate[0].lower() == "time":
                header = candidate
            continue
        tokens = _tokens(stripped)
        if tokens and _is_number(tokens[0]):
            rows.append(tokens)
    return header or [], rows


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _latest_function_file(
    case_dir: Path, function_name: str, filename: str
) -> Path | None:
    root = case_dir / "postProcessing"
    if not root.is_dir():
        return None
    roots = [root]
    roots.extend(path for path in root.iterdir() if path.is_dir())
    matches = []
    for parent in roots:
        function_dir = parent / function_name
        if not function_dir.is_dir():
            continue
        for time_dir in function_dir.iterdir():
            if not time_dir.is_dir() or not _is_number(time_dir.name):
                continue
            file_path = time_dir / filename
            if file_path.is_file():
                try:
                    _, rows = _read_table(file_path)
                    if rows:
                        matches.append((float(time_dir.name), file_path))
                except OSError:
                    continue
    return max(matches, key=lambda match: match[0])[1] if matches else None


def _last_column_value(
    case_dir: Path, function_name: str, filename: str, column_name: str
) -> float | None:
    path = _latest_function_file(case_dir, function_name, filename)
    if path is None:
        return None
    header, rows = _read_table(path)
    if not rows:
        return None
    matches = [index for index, token in enumerate(header) if token == column_name]
    if not matches:
        return None
    column_index = matches[-1]
    try:
        return float(rows[-1][column_index])
    except (IndexError, ValueError):
        return None


def _last_temperature_max(case_dir: Path, function_name: str) -> float | None:
    path = _latest_function_file(case_dir, function_name, "fieldMinMax.dat")
    if path is None:
        return None
    header, rows = _read_table(path)
    if not rows:
        return None
    try:
        field_column = header.index("field")
        max_column = header.index("max")
    except ValueError:
        return None
    for row in reversed(rows):
        try:
            if row[field_column] == "T":
                return float(row[max_column])
        except (IndexError, ValueError):
            continue
    return None


def parse_residuals(log_text: str) -> dict:
    by_iteration: dict[int, dict[str, float]] = {}
    current_iteration = None
    for line in log_text.splitlines():
        time_match = re.search(r"\bTime\s*=\s*([0-9.eE+-]+)", line)
        if time_match:
            try:
                current_iteration = int(float(time_match.group(1)))
            except ValueError:
                current_iteration = None
            if current_iteration is not None:
                by_iteration.setdefault(current_iteration, {})
            continue
        if current_iteration is None:
            continue
        solve_match = re.search(
            r"Solving for\s+([A-Za-z0-9_]+),\s*Initial residual\s*=\s*"
            r"([0-9.eE+-]+)",
            line,
        )
        if solve_match:
            field_name, raw_value = solve_match.groups()
            by_iteration[current_iteration].setdefault(field_name, float(raw_value))

    iterations = sorted(by_iteration)
    fields = sorted({name for values in by_iteration.values() for name in values})
    return {
        "iterations": iterations,
        "fields": {
            name: [by_iteration[index].get(name) for index in iterations]
            for name in fields
        },
    }


def _solver_log(case_dir: Path, application: str | None) -> Path | None:
    if application:
        named_log = case_dir / f"log.{application}"
        if named_log.is_file():
            return named_log
    excluded = {"log.checkMesh", "log.blockMesh", "log.snappyHexMesh"}
    candidates = sorted(
        path
        for path in case_dir.glob("log.*")
        if path.is_file() and path.name not in excluded
    )
    return candidates[0] if candidates else None


def _application(case_dir: Path) -> str | None:
    control_dict = case_dir / "system" / "controlDict"
    if not control_dict.is_file():
        return None
    text = control_dict.read_text(errors="replace")
    match = re.search(r"^\s*application\s+([^;\s]+)\s*;", text, re.MULTILINE)
    return match.group(1) if match else None


def _end_iteration(case_dir: Path) -> int | None:
    control_dict = case_dir / "system" / "controlDict"
    if not control_dict.is_file():
        return None
    text = control_dict.read_text(errors="replace")
    match = re.search(r"^\s*endTime\s+([0-9.eE+-]+)\s*;", text, re.MULTILINE)
    if not match:
        return None
    try:
        return int(float(match.group(1)))
    except ValueError:
        return None


def parse_case(case_dir: Path, cfg) -> Run:
    case_dir = Path(case_dir)
    run_id = case_dir.name
    index_match = re.search(r"(\d+)$", run_id)
    index = int(index_match.group(1)) if index_match else 0
    mr_path = case_dir / "model_result.json"
    if mr_path.is_file():
        try:
            mr = json.loads(mr_path.read_text())
        except (OSError, json.JSONDecodeError):
            mr = None
        if isinstance(mr, dict):
            met = mr.get("metrics") or {}
            return Run(
                id=run_id, index=index, status=mr.get("status", "completed"),
                params=mr.get("params") or {}, metrics=met,
                solver="stage-3 reduced-order model",
                mesh_cells=None, iteration=None, end_iteration=None,
                started_at=mr.get("started_at"), finished_at=mr.get("finished_at"),
                runtime_s=None, converged=None, on_pareto=False,
                error=mr.get("error"),
                has_geometry=any(case_dir.glob("*.stl")),
                residuals={"iterations": [], "fields": {}},
                details={
                    "label": mr.get("label"), "source": mr.get("source"),
                    "role": mr.get("role"), "feasible": mr.get("feasible"),
                    "violations": mr.get("violations") or [],
                    "gpu_max_c": met.get("gpu_max_c"), "hbm_max_c": met.get("hbm_max_c"),
                    "hbm_hottest": met.get("hbm_hottest"), "gpu_hottest": met.get("gpu_hottest"),
                    "coolant_rise_k": met.get("coolant_rise_k"),
                    "candidate_id": mr.get("candidate_id"), "design_id": mr.get("design_id"),
                    "files": [p.name for p in case_dir.glob("*")
                              if p.is_file() and p.suffix in {".step", ".stl", ".glb"}]
                             + (["model_result.json"] if mr_path.exists() else []),
                    "cad_error": mr.get("cad_error"),
                },
            )
    params = {}
    params_path = case_dir / "params.json"
    try:
        value = json.loads(params_path.read_text())
        if isinstance(value, dict):
            params = value
    except (OSError, json.JSONDecodeError):
        pass

    solver = _application(case_dir)
    log_path = _solver_log(case_dir, solver)
    residuals = {"iterations": [], "fields": {}}
    iteration = None
    error = None
    status = "pending"
    started_at = None
    finished_at = None
    log_text = ""
    if log_path is not None:
        try:
            stat = log_path.stat()
            log_text = log_path.read_text(errors="replace")
            started_at = _log_start_timestamp(log_text) or _iso_timestamp(stat.st_mtime)
        except OSError:
            log_path = None
            started_at = None
        if log_path is not None:
            nonempty = [line.strip() for line in log_text.splitlines() if line.strip()]
            if "FOAM FATAL" in log_text:
                status = "failed"
                fatal_position = log_text.find("FOAM FATAL")
                error_lines = log_text[fatal_position:].splitlines()[1:]
                error = "\n".join(line.strip() for line in error_lines if line.strip()) or (
                    "OpenFOAM fatal error"
                )
                finished_at = _iso_timestamp(log_path.stat().st_mtime)
            elif nonempty and nonempty[-1] == "End":
                status = "completed"
                finished_at = _iso_timestamp(log_path.stat().st_mtime)
            else:
                status = "running"
            residuals = parse_residuals(log_text)
            if residuals["iterations"]:
                iteration = residuals["iterations"][-1]

    mesh_cells = None
    mesh_log = case_dir / "log.checkMesh"
    if mesh_log.is_file():
        match = re.search(r"^\s*cells:\s*(\d+)", mesh_log.read_text(errors="replace"), re.MULTILINE)
        if match:
            mesh_cells = int(match.group(1))

    objects = cfg.function_objects
    inlet = _last_column_value(
        case_dir, objects["inlet_pressure"], "surfaceFieldValue.dat", "areaAverage(p)"
    )
    outlet = _last_column_value(
        case_dir, objects["outlet_pressure"], "surfaceFieldValue.dat", "areaAverage(p)"
    )
    max_temperature = _last_temperature_max(case_dir, objects["solid_temperature"])
    mean_temperature = _last_column_value(
        case_dir,
        objects["solid_temperature_avg"],
        "volFieldValue.dat",
        "volAverage(T)",
    )
    metrics = {
        "pressure_drop_pa": inlet - outlet if inlet is not None and outlet is not None else None,
        "max_temperature_k": max_temperature,
        "mean_temperature_k": mean_temperature,
    }

    final_residuals = []
    if residuals["iterations"]:
        final_index = len(residuals["iterations"]) - 1
        for values in residuals["fields"].values():
            value = values[final_index]
            if value is not None:
                final_residuals.append(value)
    converged = (
        bool(final_residuals) and all(value < 1e-3 for value in final_residuals)
        if status == "completed"
        else None
    )
    geometry = case_dir / "constant" / "triSurface"
    has_geometry = (
        geometry.is_dir() and any(geometry.glob("*.stl"))
    ) or any(case_dir.glob("*.stl"))

    runtime_s = None
    if started_at:
        start = datetime.fromisoformat(started_at).timestamp()
        if finished_at:
            end = datetime.fromisoformat(finished_at).timestamp()
        else:
            end = datetime.now(timezone.utc).timestamp()
        runtime_s = max(0.0, end - start)

    return Run(
        id=run_id,
        index=index,
        status=status,
        params=params,
        metrics=metrics,
        solver=solver,
        mesh_cells=mesh_cells,
        iteration=iteration,
        end_iteration=_end_iteration(case_dir),
        started_at=started_at,
        finished_at=finished_at,
        runtime_s=runtime_s,
        converged=converged,
        on_pareto=False,
        error=error,
        has_geometry=has_geometry,
        residuals=residuals,
    )

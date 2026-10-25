import json
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_STAGE3 = Path(__file__).resolve().parents[3] / "stage3_modelling"
_LABEL = "Reduced-order model (2-D conjugate conduction + duct-flow ΔP), not CFD"
_state = {
    "campaign_id": None,
    "status": "idle",
    "stage": None,
    "progress": 0.0,
    "message": None,
    "started_at": None,
    "finished_at": None,
    "label": _LABEL,
    "planned": 0,
    "evaluated": 0,
    "generation": 0,
    "n_gen": 0,
    "pareto_ids": [],
    "new_pareto_ids": [],
    "candidate_updates": [],
    "selected": [],
    "cad_exported": 0,
    "cad_total": 0,
    "mean_evaluation_s": None,
    "eta_s": None,
    "error": None,
}
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def status():
    with _lock:
        return dict(_state)


def _set(**values):
    with _lock:
        _state.update(values)


def _archive_runs(runs_dir: Path):
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return
    children = [path for path in runs_dir.iterdir() if path.is_dir()]
    if not children:
        return
    archive = (
        runs_dir.parent / f"{runs_dir.name}_archive"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    archive.mkdir(parents=True, exist_ok=True)
    for child in children:
        shutil.move(str(child), archive / child.name)


def start_intake(intake_id: str, session: dict, runs_dir: Path, sessions: dict) -> dict:
    with _lock:
        if _state["status"] == "running":
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="Campaign already running")
    runs_dir = Path(runs_dir)
    _archive_runs(runs_dir)
    campaign_id = f"j{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
    options = {"pop_size": 24, "n_gen": 8, "seed": 42, "grid_mm": 0.5}
    planned = options["pop_size"] * options["n_gen"] + 1
    started = _now()
    _set(
        campaign_id=campaign_id,
        status="running",
        stage="optimize",
        progress=0.0,
        message="Requirements parsed; optimizing candidates",
        started_at=started,
        finished_at=None,
        planned=planned,
        evaluated=0,
        generation=0,
        n_gen=options["n_gen"],
        pareto_ids=[],
        new_pareto_ids=[],
        candidate_updates=[],
        selected=[],
        cad_exported=0,
        cad_total=0,
        mean_evaluation_s=None,
        eta_s=None,
        error=None,
    )
    (runs_dir / "_campaign.json").parent.mkdir(parents=True, exist_ok=True)
    t0 = datetime.now(timezone.utc)
    candidates_by_id = {}

    def save_campaign():
        try:
            (runs_dir / "_campaign.json").write_text(
                json.dumps(status(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def on_candidate(candidate):
        now = datetime.now(timezone.utc)
        elapsed = (now - t0).total_seconds()
        evaluated = status()["evaluated"] + 1
        candidate_updates = [*status()["candidate_updates"], candidate.candidate_id][-200:]
        mean = elapsed / evaluated if evaluated else None
        remaining = max(planned - evaluated, 0)
        _set(
            evaluated=evaluated,
            candidate_updates=candidate_updates,
            mean_evaluation_s=mean,
            eta_s=(mean * remaining if mean is not None else None),
            message=f"Evaluated {evaluated}/{planned} candidates",
        )
        from coldplate.dashboard_sink import candidate_record, write_model_result

        design_id = f"{campaign_id}-{candidate.candidate_id}"
        record = {
            "candidate_id": candidate.candidate_id,
            "design_id": design_id,
            "params": candidate.params,
            "metrics": candidate.metrics,
            "feasible": candidate.feasible,
            "violations": candidate.violations,
            "role": None,
            "cad_error": None,
        }
        candidates_by_id[candidate.candidate_id] = record
        write_model_result(
            runs_dir,
            candidate.candidate_id,
            candidate_record(record, status="completed", started_at=started, finished_at=_now()),
        )
        save_campaign()

    def on_generation(generation, n_gen, n_evaluated, nondominated_ids):
        previous_ids = set(status()["pareto_ids"])
        new_ids = [candidate_id for candidate_id in nondominated_ids
                   if candidate_id not in previous_ids]
        _set(
            generation=generation,
            n_gen=n_gen,
            pareto_ids=list(nondominated_ids),
            new_pareto_ids=new_ids,
            progress=0.72 * generation / max(n_gen, 1),
            message=(
                f"Generation {generation}/{n_gen}; "
                f"{n_evaluated} evaluated; Pareto front {len(nondominated_ids)}"
            ),
        )
        save_campaign()

    def on_selected(selected):
        from coldplate.dashboard_sink import candidate_record, write_model_result

        roles = {item.candidate_id: item.role for item in selected}
        for candidate_id, record in candidates_by_id.items():
            record["role"] = roles.get(candidate_id)
            write_model_result(
                runs_dir,
                candidate_id,
                candidate_record(
                    record, status="completed", started_at=started, finished_at=_now()
                ),
            )
        _set(selected=[
            {"candidate_id": item.candidate_id, "role": item.role}
            for item in selected
        ])
        save_campaign()

    def on_cad(candidate_id, design_id, files, error):
        from coldplate.dashboard_sink import write_cad

        if files:
            write_cad(runs_dir, candidate_id, {"design_id": design_id, **files})
        run_dir = runs_dir / candidate_id
        result_path = run_dir / "model_result.json"
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                result["cad_error"] = error
                result["files"] = [
                    path.name for path in run_dir.iterdir()
                    if path.is_file() and path.suffix in {".step", ".stl", ".glb"}
                ]
                from coldplate.dashboard_sink import atomic_write
                atomic_write(result_path, json.dumps(result, indent=2).encode())
            except (OSError, json.JSONDecodeError):
                pass
        exported = status()["cad_exported"] + 1
        total = status()["cad_total"]
        _set(
            stage="cad_export",
            cad_exported=exported,
            progress=0.72 + 0.27 * exported / max(total, 1),
            message=f"CAD export {exported}/{total}",
        )
        save_campaign()

    def on_progress(stage, fraction, message):
        if stage == "pareto":
            _set(stage="pareto", progress=max(status()["progress"], 0.73), message=message)
        elif stage == "cad_export":
            _set(
                stage="cad_export",
                cad_total=max(status()["cad_total"], int(message.split("/")[-1]) if "/" in message else 0),
                message=message,
            )
        save_campaign()

    def work():
        try:
            sys.path.insert(0, str(_STAGE3))
            from coldplate.pipeline import run_job
            job_dir = _STAGE3 / "runs" / "jobs" / campaign_id
            result = run_job(
                job_dir,
                session["document"],
                {},
                {
                    "parser": "manual",
                    "manual": session["extraction"],
                    "prompt_version": "v2",
                    "use_cache": False,
                    **options,
                    "sweep": True,
                    "export_cad": True,
                },
                on_candidate=on_candidate,
                on_generation=on_generation,
                on_selected=on_selected,
                on_cad=on_cad,
                progress=on_progress,
            )
            if result.get("status") != "completed":
                raise RuntimeError(result.get("error") or "Modelling pipeline failed")
            _set(
                status="completed",
                stage="done",
                progress=1.0,
                finished_at=_now(),
                message=f"Completed: {status()['evaluated']} candidates evaluated",
                eta_s=0,
            )
            sessions[intake_id]["campaign_id"] = campaign_id
        except Exception as exc:
            _set(status="failed", stage="failed", error=str(exc), finished_at=_now())
        finally:
            save_campaign()

    threading.Thread(target=work, daemon=True).start()
    return {"campaign": status()}

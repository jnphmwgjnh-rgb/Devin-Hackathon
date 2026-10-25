import time
from datetime import datetime
from pathlib import Path

from app.foam_parser import Run, parse_case
from app.pareto import pareto_ids


class RunStore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.runs_dir = Path(cfg.runs_dir)
        self.runs: dict[str, Run] = {}
        self._fingerprints: dict[str, tuple] = {}
        self._started_at: dict[str, str] = {}
        self._finished_at: dict[str, str] = {}

    def _fingerprint(self, case_dir: Path) -> tuple:
        paths = [
            case_dir / "params.json",
            case_dir / "model_result.json",
            case_dir / "system" / "controlDict",
            *case_dir.glob("log.*"),
            *(case_dir / "constant" / "triSurface").glob("*.stl"),
            *case_dir.glob("*.step"),
            *case_dir.glob("*.stl"),
            *case_dir.glob("*.glb"),
        ]
        post_processing = case_dir / "postProcessing"
        if post_processing.is_dir():
            paths.extend(post_processing.rglob("*.dat"))

        values = []
        for path in paths:
            if not path.is_file():
                continue
            relative = path.relative_to(case_dir).as_posix()
            try:
                stat = path.stat()
                values.append((relative, stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
        return tuple(sorted(values))

    def _apply_timestamps(self, run: Run, case_dir: Path) -> None:
        if run.started_at:
            self._started_at.setdefault(run.id, run.started_at)
        run.started_at = self._started_at.get(run.id)
        previous = self.runs.get(run.id)
        if run.status in {"completed", "failed"}:
            if previous is not None and previous.status in {"completed", "failed"}:
                self._finished_at.setdefault(
                    run.id, previous.finished_at or run.finished_at
                )
            elif run.finished_at:
                self._finished_at.setdefault(run.id, run.finished_at)
            run.finished_at = self._finished_at.get(run.id)
        else:
            run.finished_at = None
        if run.started_at:
            start = datetime.fromisoformat(run.started_at).timestamp()
            end = (
                datetime.fromisoformat(run.finished_at).timestamp()
                if run.finished_at
                else time.time()
            )
            run.runtime_s = max(0.0, end - start)
        else:
            run.runtime_s = None

    def summary(self) -> dict:
        values = list(self.runs.values())
        completed = [run for run in values if run.status == "completed"]
        failed = [run for run in values if run.status == "failed"]
        running = [run for run in values if run.status == "running"]
        finished = [run for run in values if run.status in {"completed", "failed"}]
        runtimes = [run.runtime_s for run in finished if run.runtime_s is not None]
        avg_runtime = sum(runtimes) / len(runtimes) if runtimes else None
        pressure_candidates = [
            run
            for run in completed
            if run.metrics["pressure_drop_pa"] is not None
        ]
        temperature_candidates = [
            run
            for run in completed
            if run.metrics["max_temperature_k"] is not None
        ]
        best_pressure = min(
            pressure_candidates,
            key=lambda run: run.metrics["pressure_drop_pa"],
            default=None,
        )
        best_temperature = min(
            temperature_candidates,
            key=lambda run: run.metrics["max_temperature_k"],
            default=None,
        )
        return {
            "total": len(values),
            "completed": len(completed),
            "failed": len(failed),
            "running": len(running),
            "pareto_ids": sorted(run.id for run in values if run.on_pareto),
            "best": {
                "pressure_drop_pa": best_pressure.id if best_pressure else None,
                "max_temperature_k": best_temperature.id if best_temperature else None,
            },
            "avg_runtime_s": avg_runtime,
        }

    def scan(self) -> list[dict]:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        old_pareto = {run_id: run.on_pareto for run_id, run in self.runs.items()}
        events = []
        present = set()
        for case_dir in sorted(path for path in self.runs_dir.iterdir() if path.is_dir()):
            present.add(case_dir.name)
            fingerprint = self._fingerprint(case_dir)
            if self._fingerprints.get(case_dir.name) == fingerprint:
                continue
            try:
                run = parse_case(case_dir, self.cfg)
            except (OSError, ValueError, KeyError):
                continue
            previous = self.runs.get(run.id)
            self._apply_timestamps(run, case_dir)
            self.runs[run.id] = run
            self._fingerprints[run.id] = fingerprint
            events.append((previous, run))

        for run_id in set(self.runs) - present:
            del self.runs[run_id]
            self._fingerprints.pop(run_id, None)
            self._started_at.pop(run_id, None)
            self._finished_at.pop(run_id, None)

        current_pareto = pareto_ids(self.runs.values())
        for run in self.runs.values():
            run.on_pareto = run.id in current_pareto
        summary = self.summary()
        messages = []
        flipped_ids = {
            run_id
            for run_id, run in self.runs.items()
            if old_pareto.get(run_id, run.on_pareto) != run.on_pareto
        }
        for previous, run in events:
            previous_status = previous.status if previous else None
            run_json = run.to_dict()
            if previous_status != run.status and run.status == "completed":
                messages.append(
                    {
                        "type": "run_completed",
                        "run": run_json,
                        "summary": summary,
                        "joined_pareto": run.on_pareto,
                    }
                )
            elif previous_status != run.status and run.status == "failed":
                messages.append(
                    {"type": "run_failed", "run": run_json, "summary": summary}
                )
            else:
                messages.append(
                    {"type": "run_updated", "run": run_json, "summary": summary}
                )
        changed_event_ids = {event["run"]["id"] for event in messages}
        for run_id in sorted(flipped_ids - changed_event_ids):
            messages.append(
                {
                    "type": "run_updated",
                    "run": self.runs[run_id].to_dict(),
                    "summary": summary,
                }
            )
        return messages

    def list_runs(self) -> list[dict]:
        return [
            run.to_dict()
            for run in sorted(self.runs.values(), key=lambda item: item.index)
        ]

    def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

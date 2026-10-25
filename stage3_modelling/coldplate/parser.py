"""parse_requirements: requirements document -> normalized ColdPlateSpec.

Providers:
  - "devin": live Devin API session (initial + at most one repair message),
    cached by prompt/schema/document hash. Normalisation is re-run on every
    call so reference files are always current.
  - "recorded": replay a previously recorded structured_output fixture.
  - "manual": a caller-supplied extraction dict or ColdPlateSpec dict.

Devin/validation problems never raise out of here; they map to
``status="failed"`` (or "needs_input" / "unsupported").
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Callable, Literal, Mapping, Optional

from pydantic import BaseModel, Field, ValidationError

from coldplate.devin_client import DevinClient, DevinError
from coldplate.extraction_schema import (
    SCHEMA_VERSION,
    RequirementsExtraction,
    devin_json_schema,
)
from coldplate.normalize import NormalizationResult, normalize_extraction
from coldplate.prompts import PromptTemplate, load_prompt
from coldplate.spec import ColdPlateSpec

logger = logging.getLogger("coldplate.parser")

Provider = Literal["devin", "recorded", "manual"]
ParseStatus = Literal["ok", "needs_input", "failed", "unsupported"]

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "runs" / "cache" / "parse"

PROVIDER_LABELS = {
    "devin": "Devin API session (live interpretation)",
    "devin_cached": "Devin API session (cached result of an earlier live interpretation)",
    "recorded": "RECORDED Devin output replayed from a fixture (no live Devin call)",
    "manual": "MANUAL input (no Devin interpretation)",
}


class SessionRecord(BaseModel):
    session_id: str
    url: Optional[str] = None
    kind: Literal["initial", "repair"] = "initial"
    status: Optional[str] = None
    status_detail: Optional[str] = None
    acus_consumed: Optional[float] = None
    elapsed_s: Optional[float] = None


class ParseResult(BaseModel):
    status: ParseStatus
    provider: str
    provider_label: str
    prompt_version: str = ""
    schema_version: str = SCHEMA_VERSION
    cache_key: Optional[str] = None
    cached: bool = False
    devin_calls: int = 0
    sessions: list[SessionRecord] = []
    extraction: Optional[dict] = None
    initial_validation_errors: list[str] = []
    final_validation_errors: list[str] = []
    repaired: bool = False
    spec: Optional[ColdPlateSpec] = None
    missing_essential: list[str] = []
    warnings: list[str] = []
    unsupported_assumptions: list[str] = []
    evidence_misses: list[str] = []
    error: Optional[str] = None
    api_version: Optional[str] = None


def _norm_lf(document: str) -> str:
    return document.replace("\r\n", "\n").replace("\r", "\n")


def _cache_key(prompt: PromptTemplate, document: str) -> str:
    doc_sha = hashlib.sha256(_norm_lf(document).encode("utf-8")).hexdigest()
    payload = {
        "prompt_version": prompt.version,
        "schema_version": SCHEMA_VERSION,
        "prompt_sha256": prompt.sha256,
        "document_sha256": doc_sha,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _validate_extraction(raw) -> tuple[Optional[RequirementsExtraction], list[str]]:
    try:
        return RequirementsExtraction.model_validate(raw), []
    except ValidationError as e:
        errors = []
        for err in e.errors()[:40]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            errors.append(f"{loc}: {err.get('msg')}")
        return None, errors or [str(e)]


def _apply_normalization(
    result: ParseResult,
    extraction: RequirementsExtraction,
    document: str,
    reference_files: Mapping[str, bytes],
    *,
    verify_evidence: bool = True,
) -> NormalizationResult:
    norm = normalize_extraction(
        extraction, document=document, reference_files=reference_files,
        verify_evidence=verify_evidence,
    )
    result.spec = norm.spec
    result.missing_essential = norm.missing_essential
    result.warnings = norm.warnings
    result.unsupported_assumptions = norm.unsupported_assumptions
    result.evidence_misses = norm.evidence_misses
    if norm.status == "ok":
        result.status = "ok"
    elif norm.status == "needs_input":
        result.status = "needs_input"
    elif norm.status == "unsupported":
        result.status = "unsupported"
        result.error = "; ".join(norm.blocking_errors) or "unsupported inputs"
    else:  # invalid
        result.status = "failed"
    return norm


def _session_record(result_obj, kind: str) -> SessionRecord:
    return SessionRecord(
        session_id=result_obj.session_id,
        url=result_obj.url,
        kind=kind,
        status=result_obj.status,
        status_detail=result_obj.status_detail,
        acus_consumed=result_obj.acus_consumed,
        elapsed_s=result_obj.elapsed_s,
    )


def parse_requirements(
    document: str,
    *,
    reference_files: Optional[Mapping[str, bytes]] = None,
    provider: Provider = "devin",
    prompt_version: str = "v2",
    use_cache: bool = True,
    client: Optional[DevinClient] = None,
    recording: Optional[dict] = None,
    manual: Optional[dict] = None,
    cache_dir=DEFAULT_CACHE_DIR,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> ParseResult:
    reference_files = reference_files or {}
    prompt = load_prompt(prompt_version)
    result = ParseResult(
        status="failed",
        provider=provider,
        provider_label=PROVIDER_LABELS[provider],
        prompt_version=prompt_version,
    )

    # ------------------------------------------------------------------
    # manual: a ColdPlateSpec dict or an extraction dict
    # ------------------------------------------------------------------
    if provider == "manual":
        if manual is None:
            result.error = "provider='manual' requires manual=<dict>"
            return result
        if manual.get("spec_version") == "coldplate-spec/1":
            try:
                spec = ColdPlateSpec.model_validate(manual)
            except ValidationError as e:
                result.error = f"manual ColdPlateSpec invalid: {e}"
                return result
            result.spec = spec
            result.warnings = spec.warnings
            result.missing_essential = spec.missing_essential
            result.status = "needs_input" if spec.missing_essential else "ok"
            return result
        extraction_obj, errors = _validate_extraction(manual)
        result.extraction = manual
        result.initial_validation_errors = errors
        if extraction_obj is None:
            result.final_validation_errors = errors
            result.error = "manual extraction failed schema validation"
            return result
        norm = _apply_normalization(
            result, extraction_obj, document, reference_files,
            verify_evidence=False,
        )
        result.final_validation_errors = norm.blocking_errors
        if result.status == "failed":
            result.error = "; ".join(norm.blocking_errors[:5])
        return result

    # ------------------------------------------------------------------
    # recorded: replayed structured_output
    # ------------------------------------------------------------------
    if provider == "recorded":
        if not isinstance(recording, dict) or "structured_output" not in recording:
            result.error = "provider='recorded' requires recording={'structured_output': ...}"
            return result
        raw = recording["structured_output"]
        meta = recording.get("session") or {}
        if meta.get("session_id"):
            result.sessions = [
                SessionRecord(
                    session_id=meta["session_id"],
                    url=meta.get("url"),
                    kind="initial",
                    status=meta.get("status"),
                    status_detail=meta.get("status_detail"),
                    acus_consumed=meta.get("acus_consumed"),
                    elapsed_s=meta.get("elapsed_s"),
                )
            ]
        result.extraction = raw
        result.api_version = meta.get("api_version")
        extraction_obj, errors = _validate_extraction(raw)
        result.initial_validation_errors = errors
        if extraction_obj is None:
            result.final_validation_errors = errors
            result.error = "recorded extraction failed schema validation"
            return result
        norm = _apply_normalization(result, extraction_obj, document, reference_files)
        result.final_validation_errors = norm.blocking_errors
        if result.status == "failed":
            result.error = "; ".join(norm.blocking_errors[:5])
        return result

    # ------------------------------------------------------------------
    # devin: cache lookup -> live session -> at most one repair
    # ------------------------------------------------------------------
    cache_key = _cache_key(prompt, document)
    result.cache_key = cache_key
    cache_dir = Path(cache_dir)
    cache_path = cache_dir / f"{cache_key}.json"

    if use_cache and cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("unreadable parse cache %s: %s", cache_path, e)
            payload = None
        if payload and "extraction" in payload:
            result.cached = True
            result.provider_label = PROVIDER_LABELS["devin_cached"]
            result.extraction = payload["extraction"]
            result.sessions = [
                SessionRecord.model_validate(s) for s in payload.get("sessions", [])
            ]
            extraction_obj, errors = _validate_extraction(payload["extraction"])
            if extraction_obj is not None:
                norm = _apply_normalization(
                    result, extraction_obj, document, reference_files
                )
                result.final_validation_errors = norm.blocking_errors
            else:
                result.status = "failed"
                result.error = "cached extraction no longer validates"
            return result

    if client is None:
        result.error = "provider='devin' requires a DevinClient (no credentials configured)"
        return result
    result.api_version = getattr(client, "api_version", None)

    doc_sha = hashlib.sha256(_norm_lf(document).encode("utf-8")).hexdigest()
    session_id: Optional[str] = None
    try:
        r1 = client.run_structured(
            prompt.render_task(document),
            devin_json_schema(),
            title=f"coldplate-parse {doc_sha[:8]} {prompt_version}",
            tags=["coldplate-engine", "requirements-parser", prompt_version],
            resumable=True,
            on_poll=on_progress,
        )
        result.devin_calls += 1
        session_id = r1.session_id
        result.sessions.append(_session_record(r1, "initial"))
        raw = r1.structured_output
        result.extraction = raw

        extraction_obj, errors = _validate_extraction(raw)
        blocking: list[str] = []
        if extraction_obj is not None:
            norm1 = normalize_extraction(
                extraction_obj, document=document, reference_files=reference_files
            )
            blocking = norm1.blocking_errors
        result.initial_validation_errors = errors + blocking

        if result.initial_validation_errors:
            # at most one repair
            try:
                r2 = client.continue_structured(
                    session_id,
                    prompt.render_repair(result.initial_validation_errors),
                    on_poll=on_progress,
                )
                result.devin_calls += 1
                result.sessions.append(_session_record(r2, "repair"))
                raw2 = r2.structured_output
                result.extraction = raw2
                extraction_obj2, errors2 = _validate_extraction(raw2)
                blocking2: list[str] = []
                if extraction_obj2 is not None:
                    norm2 = normalize_extraction(
                        extraction_obj2,
                        document=document,
                        reference_files=reference_files,
                    )
                    blocking2 = norm2.blocking_errors
                result.final_validation_errors = errors2 + blocking2
                if not result.final_validation_errors:
                    result.repaired = True
                    extraction_obj = extraction_obj2
            except DevinError as e:
                result.status = "failed"
                result.error = str(e)
                result.final_validation_errors = result.initial_validation_errors
                return result

        if extraction_obj is None or result.final_validation_errors:
            result.status = "failed"
            problems = result.final_validation_errors or result.initial_validation_errors
            result.error = "extraction failed validation: " + "; ".join(problems[:5])
            return result

        norm = _apply_normalization(result, extraction_obj, document, reference_files)
        result.final_validation_errors = norm.blocking_errors

        # cache only when the final extraction validated (ok or needs_input)
        if result.status in ("ok", "needs_input") and use_cache:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "cache_key": cache_key,
                        "prompt_version": prompt_version,
                        "extraction": result.extraction,
                        "sessions": [s.model_dump() for s in result.sessions],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return result
    except DevinError as e:
        result.status = "failed"
        result.error = str(e)
        return result
    finally:
        if session_id is not None and client is not None:
            try:
                client.terminate_session(session_id)
            except DevinError as e:
                logger.warning("could not terminate session %s: %s", session_id, e)

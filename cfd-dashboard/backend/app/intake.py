import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from coldplate.intake import (
    apply_answers,
    assess,
    build_external_prompt,
    validate_pasted,
)
from coldplate.parser import parse_requirements

router = APIRouter(prefix="/api/intake")


def _session_response(intake_id, assessment, errors=None):
    return {
        "intake_id": intake_id,
        "status": assessment["status"],
        "errors": errors or [],
        "questions": assessment["questions"],
        "summary": assessment["summary"],
        "warnings": assessment["warnings"],
        "assumptions": assessment["assumptions"],
    }


@router.get("/capabilities")
def capabilities():
    available = bool(os.getenv("DEVIN_API_KEY"))
    return {
        "builtin_parser": available,
        "reason": None if available else "Built-in parsing requires DEVIN_API_KEY.",
    }


@router.get("/prompt")
def external_prompt():
    return {"prompt": build_external_prompt()}


@router.post("/external")
def external_intake(body: dict, request: Request):
    extraction, errors = validate_pasted(body.get("json_text", ""))
    if errors:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid", "errors": errors, "questions": []},
        )
    source_name = str(body.get("source_name") or "").strip()
    source_label = source_name or "diagram or pasted text"
    document = f"External AI read: {source_label}"
    assessment = assess(extraction, document)
    intake_id = str(uuid.uuid4())
    request.app.state.intake_sessions[intake_id] = {
        "extraction": extraction,
        "document": document,
        "provider": "manual",
        "history": [{
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "external_extraction",
            "source_name": source_name or None,
        }],
        "assessment": assessment,
    }
    return _session_response(intake_id, assessment)


@router.post("/builtin")
def builtin_intake(body: dict, request: Request):
    if not os.getenv("DEVIN_API_KEY"):
        return JSONResponse(
            status_code=409,
            content={
                "error": "builtin_parser_unavailable",
                "message": "Built-in parsing is unavailable because DEVIN_API_KEY is not configured.",
            },
        )
    document = str(body.get("text") or "")
    parsed = parse_requirements(document, provider="devin")
    if not parsed.extraction:
        return JSONResponse(
            status_code=422,
            content={
                "status": "invalid",
                "errors": parsed.final_validation_errors or [parsed.error or "Parser returned no extraction."],
                "questions": [],
            },
        )
    assessment = assess(parsed.extraction, document)
    assessment["warnings"] = list(dict.fromkeys(
        [*parsed.warnings, *assessment["warnings"]]
    ))
    intake_id = str(uuid.uuid4())
    request.app.state.intake_sessions[intake_id] = {
        "extraction": parsed.extraction,
        "document": document,
        "provider": "devin",
        "history": [{
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "builtin_extraction",
        }],
        "assessment": assessment,
    }
    return _session_response(intake_id, assessment)


@router.post("/{intake_id}/answers")
def answer_intake(intake_id: str, body: dict, request: Request):
    session = request.app.state.intake_sessions.get(intake_id)
    if session is None:
        return JSONResponse(status_code=404, content={"error": "Intake not found."})
    try:
        extraction, document = apply_answers(
            session["extraction"], session["document"], body.get("answers") or {}
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=422, content={"errors": [str(exc)]})
    assessment = assess(extraction, document)
    session["extraction"] = extraction
    session["document"] = document
    session["assessment"] = assessment
    session["history"].append({
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": "answers",
        "answers": body.get("answers") or {},
    })
    return _session_response(intake_id, assessment)

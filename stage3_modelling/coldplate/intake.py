"""Pure helpers for validating external extraction JSON and gathering follow-ups."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from coldplate.extraction_schema import devin_json_schema
from coldplate.optimize import requirement_conflicts
from coldplate.parser import _validate_extraction, parse_requirements

_JSON_FENCE = re.compile(r"```json\b\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_ANY_FENCE = re.compile(r"```")
_EVIDENCE_WARNING = "evidence quoted by your AI, not verifiable against the diagram"


def build_external_prompt() -> str:
    rules = """Read the attached cold-plate requirements diagram or the pasted requirements text.

Return exactly ONE ```json fenced block and nothing else.
Copy every number and its unit exactly as written; do not convert units or do arithmetic.
Each evidence value must be a verbatim diagram/document label or text, at most 160 characters.
Use source="input" for stated values, "input_flagged_assumption" for stated estimates or placeholders, "assumed" only for values you supply, and "missing" for values not stated.
Never invent essential coolant, temperature, heat-source, plate, or fin-region values. Mark absent essential values missing and list them in missing_fields.
List every heat source separately in document order and preserve its coordinate frame.
Use only the supplied diagram or text; do not fill gaps from prior examples or outside knowledge."""
    return (
        f"{rules}\n\nExtraction schema:\n"
        f"{json.dumps(devin_json_schema(), indent=1)}"
    )


def extract_json_block(text: str) -> tuple[dict | None, list[str]]:
    if not isinstance(text, str) or not text.strip():
        return None, ["No JSON block found; paste one ```json block."]

    blocks = list(_JSON_FENCE.finditer(text))
    fence_count = len(_ANY_FENCE.findall(text)) // 2
    if len(blocks) > 1 or fence_count > 1:
        return None, ["More than one JSON block found; paste ONE block."]
    if blocks:
        raw = blocks[0].group(1).strip()
    else:
        raw = text.strip()
        if not raw.startswith("{"):
            return None, ["No JSON block found; paste one ```json block."]

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"Malformed JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."]
    if not isinstance(obj, dict):
        return None, ["The JSON value must be an object."]
    return obj, []


def validate_pasted(text: str) -> tuple[dict | None, list[str]]:
    raw, errors = extract_json_block(text)
    if errors:
        return None, errors
    extraction, schema_errors = _validate_extraction(raw)
    if extraction is None:
        return None, list(dict.fromkeys(schema_errors))
    return extraction.model_dump(mode="json"), []


def _tokens(path: str) -> list[str]:
    return re.findall(r"[^.[\]]+|\[(?:[^\]]*)\]", path)


def _resolve_parent(root: Any, path: str):
    tokens = _tokens(path)
    if not tokens:
        return None, None
    current = root
    for token in tokens[:-1]:
        if token.startswith("["):
            key = token[1:-1]
            if not isinstance(current, list):
                return None, None
            if key.isdigit():
                index = int(key)
            else:
                index = next(
                    (
                        i for i, item in enumerate(current)
                        if isinstance(item, dict)
                        and key in (item.get("name"), item.get("type"), item.get("component_type"))
                    ),
                    None,
                )
            if index is None or index >= len(current):
                return None, None
            current = current[index]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            return None, None
    last = tokens[-1]
    if last.startswith("["):
        return None, None
    return current, last


def _raw_value(root: dict, path: str):
    parent, key = _resolve_parent(root, path)
    if parent is None:
        return None
    if isinstance(parent, list) and key.isdigit():
        index = int(key)
        return parent[index] if index < len(parent) else None
    return parent.get(key) if isinstance(parent, dict) else None


def _field(path: str, extraction: dict) -> dict | None:
    parent, key = _resolve_parent(extraction, path)
    if parent is None:
        return None
    value = parent.get(key) if isinstance(parent, dict) else None
    heat_source = isinstance(parent, dict) and "name" in parent and key in {
        "x0", "y0", "width", "height", "power"
    }
    if heat_source:
        unit = parent.get("power_unit") if key == "power" else parent.get("length_unit")
        unit = unit or ("W" if key == "power" else "mm")
        return {
            "path": path,
            "label": f"{parent.get('name', 'Heat source')} {key.replace('_', ' ')}",
            "unit": unit,
            "kind": "heat_source",
            "current": {"value": value, "unit": unit},
        }
    if isinstance(value, dict) and "values" in value:
        return {
            "path": path,
            "label": key.replace("_", " "),
            "unit": value.get("unit"),
            "kind": "number_list",
            "current": {"value": value.get("values"), "unit": value.get("unit")},
        }
    if isinstance(value, dict) and "value" in value:
        kind = "quantity" if "unit" in value else "text"
        return {
            "path": path,
            "label": key.replace("_", " "),
            "unit": value.get("unit"),
            "kind": kind,
            "current": {"value": value.get("value"), "unit": value.get("unit")},
        }
    if path == "heat_sources":
        return {
            "path": path, "label": "Heat source geometry and power",
            "unit": None, "kind": "heat_source", "current": value,
        }
    return None


def _summary(spec) -> dict:
    if spec is None:
        return {}
    data = spec.model_dump(mode="json")
    out = {}

    def visit(node, path):
        if isinstance(node, dict):
            if "value" in node and "source" in node:
                out[path] = {
                    "value": node.get("value"),
                    "unit": node.get("unit"),
                    "source": node.get("source"),
                    "evidence": node.get("evidence"),
                }
                return
            for key, value in node.items():
                visit(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]")

    visit(data, "")
    return out


def assess(extraction: dict, document: str) -> dict:
    result = parse_requirements(
        document, provider="manual", manual=extraction, use_cache=False
    )
    questions = []
    missing = []
    unresolved = []
    for path in result.missing_essential:
        field = _field(path, extraction)
        if field is None:
            unresolved.append(f"{path}: required value is missing; edit the JSON.")
        else:
            missing.append(field)
    if missing:
        questions.append({
            "id": "missing_essential",
            "kind": "missing",
            "message": "Provide the missing required values.",
            "fields": missing,
        })

    if result.spec is not None and not result.missing_essential:
        conflicts = requirement_conflicts(result.spec.engine_inputs())
        if conflicts:
            conflict_paths = (
                "coolant.flow_rate",
                "coolant.flow_rate_sweep",
                "coolant.max_outlet_temperature_rise",
            )
            questions.append({
                "id": "requirement_conflicts",
                "kind": "contradiction",
                "message": "The stated flow and outlet-rise limit conflict: " + " ".join(conflicts),
                "fields": [field for path in conflict_paths
                           if (field := _field(path, extraction)) is not None],
            })

    blocking = list(result.final_validation_errors or [])
    if result.error and not blocking and result.status in ("failed", "unsupported"):
        blocking = [result.error]
    for error in dict.fromkeys(blocking):
        path = error.split(":", 1)[0].split("=", 1)[0].strip()
        field = _field(path, extraction)
        if field is not None:
            questions.append({
                "id": f"invalid:{path}",
                "kind": "invalid",
                "message": error,
                "fields": [field],
            })
        else:
            unresolved.append(f"{error} Edit the JSON and validate it again.")
    if unresolved:
        questions.append({
            "id": "invalid:normalization",
            "kind": "invalid",
            "message": " ".join(unresolved),
            "fields": [],
        })

    warnings = list(result.warnings)
    if result.evidence_misses:
        warnings = [
            warning for warning in warnings
            if "evidence quoted by your AI" in warning
            or not any(miss.split(":", 1)[0] in warning for miss in result.evidence_misses)
        ]
        if _EVIDENCE_WARNING not in warnings:
            warnings.append(_EVIDENCE_WARNING)
    status = "invalid" if any(q["kind"] == "invalid" for q in questions) else (
        "needs_input" if questions or result.status == "needs_input" else "ok"
    )
    return {
        "status": status,
        "summary": _summary(result.spec),
        "warnings": list(dict.fromkeys(warnings)),
        "assumptions": [assumption.model_dump(mode="json")
                        for assumption in (result.spec.assumptions if result.spec else [])],
        "questions": questions,
    }


def _answer_value(value, unit):
    if isinstance(value, list):
        rendered = ", ".join(str(item) for item in value)
    else:
        rendered = str(value)
    return f"{rendered} {unit}".strip() if unit else rendered


def apply_answers(
    extraction: dict, document: str, answers: dict[str, dict]
) -> tuple[dict, str]:
    updated = copy.deepcopy(extraction)
    evidence_lines = []
    for path, answer in answers.items():
        parent, key = _resolve_parent(updated, path)
        if parent is None:
            if path != "heat_sources":
                raise ValueError(f"Unresolvable answer path: {path}")
            parent, key = updated, "heat_sources"
        value = answer.get("value")
        target = parent.get(key) if isinstance(parent, dict) else None
        unit = answer.get("unit")
        evidence = f"{path}: {_answer_value(value, unit)}"
        if path == "heat_sources":
            updated["heat_sources"] = value
            evidence_lines.append(evidence)
            continue
        if isinstance(target, dict) and "values" in target:
            target["values"] = value if isinstance(value, list) else [value]
            target["unit"] = unit if unit is not None else target.get("unit")
            target["source"] = "input"
            target["evidence"] = evidence
        elif isinstance(target, dict) and "value" in target:
            target["value"] = value
            if "unit" in target:
                target["unit"] = unit if unit is not None else target.get("unit")
            target["source"] = "input"
            target["evidence"] = evidence
        elif isinstance(parent, dict) and key in {"x0", "y0", "width", "height", "power"}:
            parent[key] = value
            unit_key = "power_unit" if key == "power" else "length_unit"
            parent[unit_key] = unit if unit is not None else parent.get(unit_key)
            prior = parent.get("evidence")
            parent["evidence"] = "\n".join(
                line for line in [prior, evidence] if line
            )
            parent["source"] = "input"
        else:
            raise ValueError(f"Unresolvable answer path: {path}")
        evidence_lines.append(evidence)
    if evidence_lines:
        updated_doc = document.rstrip() + "\n\n## Follow-up answers\n" + "\n".join(evidence_lines)
    else:
        updated_doc = document
    return updated, updated_doc

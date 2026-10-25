import copy
import json
from dataclasses import fields

import pytest

from coldplate.intake import (
    apply_answers,
    assess,
    build_external_prompt,
    extract_json_block,
    validate_pasted,
)
from coldplate.parser import parse_requirements
from tests.test_api_stack import DOCS, REF, rec


@pytest.mark.parametrize(
    "name",
    ["A_canonical", "B_flow_3lpm", "C_gpu1_power_plus20", "D_plate_90x84"],
)
def test_external_extraction_matches_recorded_engine_inputs(name):
    recording = rec(name)
    structured = recording["structured_output"]
    pasted = (
        "Here are the extracted requirements:\n\n```json\n"
        + json.dumps(structured)
        + "\n```\nI copied this from the reference package."
    )

    extraction, errors = validate_pasted(pasted)
    assert not errors
    external = assess(extraction, DOCS[name])
    recorded = parse_requirements(
        DOCS[name], reference_files=REF, provider="recorded",
        recording=recording, use_cache=False,
    )

    assert external["status"] in {"ok", "needs_input"}
    actual = parse_requirements(
        DOCS[name], reference_files=REF, provider="manual",
        manual=extraction, use_cache=False,
    )
    expected_inputs = recorded.spec.engine_inputs()
    actual_inputs = actual.spec.engine_inputs()
    for field in fields(expected_inputs):
        actual_value = getattr(actual_inputs, field.name)
        expected_value = getattr(expected_inputs, field.name)
        if field.name == "power_map":
            assert actual_value.sources == expected_value.sources
            assert actual_value.pitch_mm == expected_value.pitch_mm
            assert actual_value.values_W_mm2.tolist() == expected_value.values_W_mm2.tolist()
        else:
            assert actual_value == expected_value


def test_extract_json_block_reports_syntax_and_block_errors():
    obj, errors = extract_json_block('```json\n{"a": }\n```')
    assert obj is None
    assert "line 1, column 7" in errors[0]

    _, errors = extract_json_block("```json\n{}\n```\n```json\n{}\n```")
    assert "paste ONE block" in errors[0]

    _, errors = extract_json_block("There is no structured data here.")
    assert "No JSON block" in errors[0]


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda data: data["coolant"].pop("flow_rate"), "coolant.flow_rate"),
        (lambda data: data["plate"]["length_x"].update(value="bad"),
         "plate.length_x.value"),
        (lambda data: data.update(unexpected=True), "unexpected"),
        (lambda data: data["flow"].update(arrangement="impossible"),
         "flow.arrangement"),
    ],
)
def test_validate_pasted_reports_specific_schema_paths(mutate, path):
    raw = copy.deepcopy(rec("A_canonical")["structured_output"])
    mutate(raw)
    extraction, errors = validate_pasted(
        "```json\n" + json.dumps(raw) + "\n```"
    )
    assert extraction is None
    assert any(error.startswith(path + ":") for error in errors)


def test_missing_flow_is_one_question_and_can_be_answered():
    raw = rec("E_missing_flow")["structured_output"]
    document = DOCS["E_missing_flow"]
    extraction, errors = validate_pasted("```json\n" + json.dumps(raw) + "\n```")
    assert not errors

    result = assess(extraction, document)
    assert result["status"] == "needs_input"
    assert len(result["questions"]) == 1
    assert [field["path"] for field in result["questions"][0]["fields"]] == [
        "coolant.flow_rate"
    ]

    updated, updated_document = apply_answers(
        extraction, document,
        {"coolant.flow_rate": {"value": 2, "unit": "L/min"}},
    )
    completed = assess(updated, updated_document)
    assert completed["status"] == "ok"
    inputs = parse_requirements(
        updated_document, provider="manual", manual=updated, use_cache=False
    ).spec.engine_inputs()
    assert inputs.flow_Lpm == 2
    assert updated["coolant"]["flow_rate"]["source"] == "input"
    assert "coolant.flow_rate: 2 L/min" in updated["coolant"]["flow_rate"]["evidence"]


def test_impossible_limits_question_prefills_three_fields_and_accepts_sweep():
    raw = rec("F_impossible_limits")["structured_output"]
    document = DOCS["F_impossible_limits"]
    extraction, errors = validate_pasted("```json\n" + json.dumps(raw) + "\n```")
    assert not errors
    result = assess(extraction, document)
    assert result["status"] == "needs_input"
    assert len(result["questions"]) == 1
    question = result["questions"][0]
    assert question["kind"] == "contradiction"
    assert [field["path"] for field in question["fields"]] == [
        "coolant.flow_rate",
        "coolant.flow_rate_sweep",
        "coolant.max_outlet_temperature_rise",
    ]
    assert all(field["current"] is not None for field in question["fields"])

    updated, updated_document = apply_answers(
        extraction, document,
        {"coolant.flow_rate_sweep": {"value": [2, 2.5, 3], "unit": "L/min"}},
    )
    assert assess(updated, updated_document)["status"] == "ok"


def test_multiple_missing_essential_values_are_requested_together():
    raw = copy.deepcopy(rec("A_canonical")["structured_output"])
    raw["coolant"]["flow_rate"] = {
        "value": None, "unit": None, "source": "missing",
        "evidence": None, "note": None,
    }
    gpu_limit = next(
        limit for limit in raw["temperature_limits"]
        if limit["component_type"].casefold() == "gpu"
    )
    gpu_limit["hard_max"] = {
        "value": None, "unit": None, "source": "missing",
        "evidence": None, "note": None,
    }
    extraction, errors = validate_pasted("```json\n" + json.dumps(raw) + "\n```")
    assert not errors
    result = assess(extraction, DOCS["A_canonical"])
    missing_paths = [
        field["path"]
        for question in result["questions"]
        for field in question["fields"]
    ]
    assert "coolant.flow_rate" in missing_paths
    assert "temperature_limits[GPU].hard_max" in missing_paths


def test_external_prompt_includes_one_block_rule_and_schema():
    prompt = build_external_prompt()
    assert "exactly ONE ```json fenced block and nothing else" in prompt
    assert '"schema_version"' in prompt

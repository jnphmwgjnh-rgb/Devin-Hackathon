from types import SimpleNamespace

import pytest

import llm


class FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.code = status_code


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _provider(monkeypatch, outcomes, fallback_models="gemini-3.6-flash,gemini-3.5-flash"):
    from google import genai
    from google.genai import types

    models = FakeModels(outcomes)
    monkeypatch.setattr(
        genai,
        "Client",
        lambda: SimpleNamespace(models=models),
    )
    monkeypatch.setenv("LLM_FALLBACK_MODELS", fallback_models)
    sleeps = []
    provider = llm.GeminiProvider(
        model="gemini-3.8-flash",
        sleep_fn=sleeps.append,
    )
    return provider, models, sleeps, types


def _generate(provider):
    return provider.generate(
        system="system",
        history=[],
        tools=[
            {
                "name": "set_field",
                "description": "Set a field",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            }
        ],
        digest={},
    )


def _success():
    return SimpleNamespace(candidates=[])


def test_mock_group_prompt_separates_confirmation_from_bullet_list():
    turn = llm.MockProvider().generate(
        system="",
        history=[{"role": "tool"}],
        tools=[],
        digest={
            "next_group": 1,
            "next_group_name": "Limits and scoring",
            "next_group_fields": [
                {
                    "label": "HBM figure-of-merit weight",
                    "value_display": "0.7",
                    "status": "default_proposed",
                }
            ],
        },
    )

    assert turn.text.endswith(
        "- HBM figure-of-merit weight: 0.7\n\nUse these?"
    )


def test_render_group_omits_header_when_group_is_unavailable():
    text = llm.render_group({
        "next_group": None,
        "next_group_name": None,
        "next_group_fields": [{"label": "Flow rate", "value_display": "2 L/min"}],
    })

    assert "Group None: None" not in text
    assert "- Flow rate: 2 L/min" in text


def test_gemini_retries_transient_error_with_injectable_backoff(monkeypatch):
    provider, models, sleeps, _ = _provider(
        monkeypatch,
        [FakeStatusError(503), FakeStatusError(503), FakeStatusError(503), _success()],
    )

    turn = _generate(provider)

    assert turn == llm.LLMTurn(text="", tool_calls=[], raw=None)
    assert [call[0] for call in models.calls] == ["gemini-3.8-flash"] * 4
    assert sleeps == [2, 4, 8]


def test_gemini_falls_back_after_primary_retries_and_updates_model(monkeypatch):
    provider, models, sleeps, _ = _provider(
        monkeypatch,
        [FakeStatusError(503) for _ in range(4)] + [_success()],
    )

    _generate(provider)

    assert [call[0] for call in models.calls] == [
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
    ]
    assert sleeps == [2, 4, 8]
    assert provider.model == "gemini-3.6-flash"


def test_gemini_raises_non_transient_error_without_retry(monkeypatch):
    provider, models, sleeps, _ = _provider(monkeypatch, [FakeStatusError(400)])

    with pytest.raises(FakeStatusError, match="status 400"):
        _generate(provider)

    assert len(models.calls) == 1
    assert sleeps == []


def test_gemini_rebuilds_history_only_for_a_different_model(monkeypatch):
    provider, _, _, types = _provider(monkeypatch, [])
    raw = types.Content(role="model", parts=[types.Part(text="prior response")])
    history = [
        {
            "role": "assistant",
            "text": "prior response",
            "tool_calls": [],
            "raw": raw,
            "model": "gemini-3.8-flash",
        }
    ]

    same_model = provider._content_history(history, "gemini-3.8-flash")
    other_model = provider._content_history(history, "gemini-3.5-flash")

    assert same_model[0] is raw
    assert other_model[0] is not raw
    assert other_model[0].parts[0].text == "prior response"

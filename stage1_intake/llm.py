"""Provider abstraction for requirements-intake conversations."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol
import uuid

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / ".env")


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMTurn:
    text: str
    tool_calls: list[ToolCall]
    raw: Any = None


class LLMProvider(Protocol):
    def generate(self, system: str, history: list[dict], tools: list[dict], digest: dict) -> LLMTurn:
        ...


class GeminiProvider:
    def __init__(
        self,
        model=os.getenv("LLM_MODEL", "gemini-3.8-flash"),
        sleep_fn=time.sleep,
        fallback_models=None,
    ):
        from google import genai
        from google.genai import types

        self.client = genai.Client()
        self.model = model
        self.types = types
        self.sleep = sleep_fn
        configured_fallbacks = (
            os.getenv(
                "LLM_FALLBACK_MODELS",
                "gemini-3.6-flash,gemini-3.5-flash",
            )
            if fallback_models is None
            else ",".join(fallback_models)
        )
        self.fallback_models = [
            fallback.strip()
            for fallback in configured_fallbacks.split(",")
            if fallback.strip()
        ]

    def _content_history(self, history, model=None):
        types = self.types
        model = model or self.model
        contents = []
        for entry in history:
            role = entry["role"]
            if role == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=entry["text"])]))
            elif role == "assistant":
                raw = entry.get("raw")
                raw_model = entry.get("model")
                if isinstance(raw, types.Content) and (
                    raw_model is None or raw_model == model
                ):
                    contents.append(raw)
                    continue
                parts = []
                if entry.get("text"):
                    parts.append(types.Part(text=entry["text"]))
                for call in entry.get("tool_calls", []):
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                id=call.id,
                                name=call.name,
                                args=call.args,
                            )
                        )
                    )
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                parts = [
                    types.Part.from_function_response(
                        name=result["name"],
                        response=result["result"],
                    )
                    for result in entry.get("results", [])
                ]
                if parts:
                    contents.append(types.Content(role="user", parts=parts))
        return contents

    @staticmethod
    def _transient_error(exc):
        candidates = [
            getattr(exc, "status_code", None),
            getattr(exc, "code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
        ]
        for candidate in candidates:
            try:
                status = int(candidate)
            except (TypeError, ValueError):
                continue
            if status == 429 or 500 <= status <= 599:
                return True
        return False

    def _generate_with_retries(self, model, history, config):
        contents = self._content_history(history, model)
        delays = (2, 4, 8)
        for attempt in range(4):
            try:
                return self.client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                if not self._transient_error(exc) or attempt == len(delays):
                    raise
                self.sleep(delays[attempt])

    def generate(self, system, history, tools, digest):
        types = self.types
        function_declarations = [
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                parameters_json_schema=tool["parameters"],
            )
            for tool in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=function_declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        )
        response = None
        last_transient_error = None
        succeeded = False
        try:
            response = self._generate_with_retries(self.model, history, config)
            succeeded = True
        except Exception as exc:
            if not self._transient_error(exc):
                raise
            last_transient_error = exc

        if not succeeded:
            for fallback_model in self.fallback_models:
                if fallback_model == self.model:
                    continue
                try:
                    response = self._generate_with_retries(
                        fallback_model,
                        history,
                        config,
                    )
                except Exception as exc:
                    if not self._transient_error(exc):
                        raise
                    last_transient_error = exc
                    continue
                self.model = fallback_model
                succeeded = True
                break

        if not succeeded:
            raise last_transient_error
        if not getattr(response, "candidates", None):
            return LLMTurn(text="", tool_calls=[], raw=None)
        content = response.candidates[0].content
        text_parts = []
        tool_calls = []
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "thought", False):
                continue
            function_call = getattr(part, "function_call", None)
            if function_call is not None and getattr(function_call, "name", None):
                tool_calls.append(
                    ToolCall(
                        id=getattr(function_call, "id", None) or uuid.uuid4().hex,
                        name=function_call.name,
                        args=dict(getattr(function_call, "args", None) or {}),
                    )
                )
            elif getattr(part, "text", None):
                text_parts.append(part.text)
        return LLMTurn(text="".join(text_parts), tool_calls=tool_calls, raw=content)


def _yes(message):
    return re.match(
        r"^\s*(yes|yep|yeah|y|correct|ok|okay|sure|sounds good|use (those|these|them)|confirm(ed)?)\b",
        message,
        re.IGNORECASE,
    ) is not None


def _clear_acknowledgement(message):
    normalized = re.sub(r"[^a-z]+", " ", message.casefold()).strip()
    return normalized in {
        "yes",
        "ok",
        "acknowledge",
        "acknowledged",
        "fine",
        "accept",
        "yes acknowledged",
    }


def _choice_mentions(message, choices):
    normalized_message = message.casefold()
    matched = []
    for choice in choices:
        normalized_choice = choice.replace("_", " ").casefold()
        variants = {normalized_choice, normalized_choice.split()[0]}
        found = False
        for variant in variants:
            pattern = r"(?<!\w)" + re.escape(variant).replace(r"\ ", r"\s+") + r"(?!\w)"
            if re.search(pattern, normalized_message):
                found = True
                break
        if found:
            matched.append(choice)
    return matched


def render_group(digest):
    if digest.get("finalized"):
        return "Requirements finalized. requirements_final.json is written."
    if digest.get("complete"):
        return f'{digest.get("summary", "")}\n\nIs this correct?'
    group_number = digest.get("next_group")
    group_name = digest.get("next_group_name")
    lines = (
        [f"Group {group_number}: {group_name}"]
        if group_number is not None and group_name is not None
        else []
    )
    for item in digest.get("next_group_fields", []):
        display = item.get("value_display", "MISSING")
        if item.get("status") == "missing":
            display = "MISSING"
            if item.get("choices"):
                display += f" (choices: {', '.join(item['choices'])})"
        if item.get("is_assumption"):
            display += " (ASSUMPTION)"
        lines.append(f"- {item['label']}: {display}")
    lines.append("")
    lines.append("Use these?")
    return "\n".join(lines)


class MockProvider:
    def generate(self, system, history, tools, digest):
        if history and history[-1].get("role") == "tool":
            return LLMTurn(text=render_group(digest), tool_calls=[])
        if history and history[-1].get("role") == "user":
            message = history[-1].get("text", "")
            if digest.get("awaiting_final_confirmation") and _yes(message):
                return LLMTurn(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id=uuid.uuid4().hex,
                            name="finalize",
                            args={"user_quote": message},
                        )
                    ],
                )
            calls = []
            if _yes(message):
                field_ids = [
                    item["id"]
                    for item in digest.get("presented_fields", [])
                    if item["status"] == "default_proposed"
                ]
                if field_ids:
                    calls.append(
                        ToolCall(
                            id=uuid.uuid4().hex,
                            name="confirm_defaults",
                            args={"field_ids": field_ids, "user_quote": message},
                        )
                    )
            has_proposed_fields = any(
                item.get("status") == "default_proposed"
                for item in digest.get("presented_fields", [])
            )
            if _clear_acknowledgement(message) and not has_proposed_fields:
                for check in digest.get("failing_checks", []):
                    if check.get("level") != "warning" or check.get("acknowledged"):
                        continue
                    calls.append(
                        ToolCall(
                            id=uuid.uuid4().hex,
                            name="acknowledge_warning",
                            args={
                                "check_id": check["id"],
                                "user_quote": message,
                            },
                        )
                    )
            for item in digest.get("next_group_fields", []):
                if item.get("type") != "enum" or item.get("status") != "missing":
                    continue
                matches = _choice_mentions(message, item.get("choices") or [])
                if len(matches) == 1:
                    choice = matches[0]
                    calls.append(
                        ToolCall(
                            id=uuid.uuid4().hex,
                            name="set_field",
                            args={
                                "field_id": item["id"],
                                "value": json.dumps(choice),
                                "user_quote": message,
                            },
                        )
                    )
            if calls:
                return LLMTurn(text="", tool_calls=calls)
            return LLMTurn(
                text=(
                    "(mock LLM) I can only understand 'yes' to accept the proposed values or a choice name. "
                    "Set GEMINI_API_KEY for free-text answers.\n\n"
                    f"{render_group(digest)}"
                ),
                tool_calls=[],
            )
        return LLMTurn(text=render_group(digest), tool_calls=[])


class ScriptedProvider:
    def __init__(self, turns: list[LLMTurn | Callable[[str, list[dict], dict], LLMTurn]]):
        self.turns = list(turns)
        self.calls = []
        self.index = 0

    def generate(self, system, history, tools, digest):
        self.calls.append((system, history, digest))
        if self.index >= len(self.turns):
            raise RuntimeError("ScriptedProvider ran out of turns")
        turn = self.turns[self.index]
        self.index += 1
        if callable(turn):
            return turn(system, history, digest)
        return turn


def get_provider(name=None):
    configured = name or os.getenv("LLM_PROVIDER")
    if configured is None:
        configured = "gemini" if os.getenv("GEMINI_API_KEY") else "mock"
    if configured == "gemini":
        return GeminiProvider()
    if configured == "mock":
        return MockProvider()
    if configured == "scripted":
        raise ValueError("ScriptedProvider must be instantiated with a turn list")
    raise ValueError(f"unknown LLM provider {configured!r}")

"""LLM access for every agent.

Two operations are all the agents need:
- ``decide``: function calling. The model must pick exactly one of the offered tools (or ``finish``)
  and give a one-sentence ``reasoning``. This is what drives each agent's own reasoning loop.
- ``generate``: structured JSON output validated against a Pydantic schema (optionally with images,
  used by the ROI vision checks).

``GeminiLLM`` talks to Google Gemini. ``FakeLLM`` replays scripted policies so tests never hit the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the model is unavailable or returns something unusable."""


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict = {"type": "object", "properties": {}}


class ToolCall(BaseModel):
    name: str
    args: dict = {}
    reasoning: str = ""


@dataclass
class ImageInput:
    data: bytes
    mime_type: str = "image/jpeg"


FINISH_TOOL = ToolSpec(
    name="finish",
    description="Call when your work for this turn is done. Summarise the outcome.",
    parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
)


class LLM(Protocol):
    async def decide(self, *, purpose: str, system: str, context: dict, tools: list[ToolSpec]) -> ToolCall: ...

    async def generate(
        self, *, purpose: str, system: str, context: dict, schema: type[T], images: list[ImageInput] | None = None
    ) -> T: ...


def render_context(context: dict) -> str:
    return "Context (JSON):\n" + json.dumps(context, default=str, indent=1, ensure_ascii=False)


def _with_reasoning(parameters: dict) -> dict:
    """Every tool call must carry a short reasoning string, which ends up in the trace."""
    params = json.loads(json.dumps(parameters))
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    params["properties"]["reasoning"] = {
        "type": "string",
        "description": "One sentence: why this is the right next step given the context.",
    }
    params["required"] = sorted(set(params.get("required", [])) | {"reasoning"})
    return params


class GeminiLLM:
    """Gemini via the google-genai SDK, with retry on rate limits and optional call spacing."""

    RETRY_DELAYS = (2, 6, 15, 30)

    def __init__(self, api_key: str, model: str, min_interval_s: float = 0.0):
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.min_interval_s = min_interval_s
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def _generate_content(self, contents: list, config: Any):
        last_error: Exception | None = None
        for attempt, delay in enumerate((0, *self.RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            async with self._lock:
                wait = self.min_interval_s - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
            try:
                return await self.client.aio.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except Exception as exc:  # SDK raises several error types; inspect the status code
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                last_error = exc
                if code in (429, 500, 503, 504):
                    log.warning("Gemini call failed with %s (attempt %d), retrying", code, attempt + 1)
                    continue
                raise LLMError(f"Gemini error: {exc}") from exc
        raise LLMError(f"Gemini unavailable after retries: {last_error}")

    async def decide(self, *, purpose: str, system: str, context: dict, tools: list[ToolSpec]) -> ToolCall:
        from google.genai import types

        declarations = [
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=_with_reasoning(t.parameters)
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY")),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        )
        response = await self._generate_content([render_context(context)], config)
        calls = response.function_calls or []
        if not calls:
            raise LLMError(f"[{purpose}] model returned no tool call")
        args = dict(calls[0].args or {})
        reasoning = str(args.pop("reasoning", ""))
        return ToolCall(name=calls[0].name, args=args, reasoning=reasoning)

    async def generate(
        self, *, purpose: str, system: str, context: dict, schema: type[T], images: list[ImageInput] | None = None
    ) -> T:
        from google.genai import types

        contents: list = [types.Part.from_bytes(data=i.data, mime_type=i.mime_type) for i in images or []]
        contents.append(render_context(context))
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.3,
        )
        response = await self._generate_content(contents, config)
        try:
            return schema.model_validate_json(response.text or "")
        except Exception as exc:
            raise LLMError(f"[{purpose}] invalid structured output: {exc}") from exc


class UnconfiguredLLM:
    """Used when GEMINI_API_KEY is missing: every call fails cleanly so callers can degrade."""

    async def decide(self, **_: Any) -> ToolCall:
        raise LLMError("GEMINI_API_KEY is not set")

    async def generate(self, **_: Any) -> Any:
        raise LLMError("GEMINI_API_KEY is not set")


Policy = Callable[[dict], Any] | list | Any


class FakeLLM:
    """Scripted LLM for tests.

    Policies are looked up by ``purpose`` (exact match, then the part before the first dot).
    A policy can be a callable ``f(context) -> value``, a list (one item consumed per call) or a fixed value.
    For ``decide`` the value may be a ToolCall or a dict ``{"name", "args", "reasoning"}``; for ``generate``
    it may be a model instance or a dict. An Exception value is raised, to simulate model failures.
    """

    def __init__(self, decide: dict[str, Policy] | None = None, generate: dict[str, Policy] | None = None):
        self.decide_policies = decide or {}
        self.generate_policies = generate or {}
        self.calls: list[dict] = []

    @staticmethod
    def _resolve(policies: dict[str, Policy], purpose: str, context: dict) -> Any:
        policy = policies.get(purpose, policies.get(purpose.split(".")[0]))
        if policy is None:
            raise LLMError(f"FakeLLM has no policy for '{purpose}'")
        if isinstance(policy, list):
            if not policy:
                raise LLMError(f"FakeLLM policy for '{purpose}' is exhausted")
            value = policy.pop(0)
        else:
            value = policy
        if callable(value) and not isinstance(value, BaseModel):
            value = value(context)
        if isinstance(value, Exception):
            raise value
        return value

    async def decide(self, *, purpose: str, system: str, context: dict, tools: list[ToolSpec]) -> ToolCall:
        self.calls.append({"op": "decide", "purpose": purpose, "context": context, "tools": [t.name for t in tools]})
        value = self._resolve(self.decide_policies, purpose, context)
        call = value if isinstance(value, ToolCall) else ToolCall(**value)
        if call.name not in {t.name for t in tools}:
            raise LLMError(f"FakeLLM chose unknown tool '{call.name}' for '{purpose}'")
        return call

    async def generate(
        self, *, purpose: str, system: str, context: dict, schema: type[T], images: list[ImageInput] | None = None
    ) -> T:
        self.calls.append({"op": "generate", "purpose": purpose, "context": context, "images": len(images or [])})
        value = self._resolve(self.generate_policies, purpose, context)
        return value if isinstance(value, schema) else schema.model_validate(value)


def build_llm(settings) -> LLM:
    if not settings.gemini_api_key:
        log.warning("GEMINI_API_KEY not set: LLM calls will fail and agents will report errors")
        return UnconfiguredLLM()
    return GeminiLLM(settings.gemini_api_key, settings.gemini_model, settings.gemini_min_interval_s)

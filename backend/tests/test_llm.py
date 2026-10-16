import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.core.llm import FINISH_TOOL, FakeLLM, GeminiLLM, LLMError, ToolSpec, _with_reasoning, render_context

SEARCH = ToolSpec(name="search", description="search")


class Answer(BaseModel):
    value: int


async def test_list_policy_is_consumed_in_order():
    llm = FakeLLM(decide={"agent": [{"name": "search", "args": {"q": 1}}, {"name": "finish", "args": {}}]})
    first = await llm.decide(purpose="agent.plan", system="", context={}, tools=[SEARCH, FINISH_TOOL])
    second = await llm.decide(purpose="agent.plan", system="", context={}, tools=[SEARCH, FINISH_TOOL])
    assert (first.name, first.args) == ("search", {"q": 1})
    assert second.name == "finish"
    with pytest.raises(LLMError):
        await llm.decide(purpose="agent.plan", system="", context={}, tools=[SEARCH, FINISH_TOOL])


async def test_callable_policy_sees_context_and_validates_schema():
    llm = FakeLLM(generate={"calc.add": lambda ctx: {"value": ctx["a"] + ctx["b"]}})
    out = await llm.generate(purpose="calc.add", system="", context={"a": 2, "b": 3}, schema=Answer)
    assert out == Answer(value=5)
    assert llm.calls[0]["purpose"] == "calc.add"


async def test_exception_policy_simulates_model_failure():
    llm = FakeLLM(generate={"x": LLMError("quota")})
    with pytest.raises(LLMError, match="quota"):
        await llm.generate(purpose="x", system="", context={}, schema=Answer)


async def test_choosing_a_tool_that_was_not_offered_fails():
    llm = FakeLLM(decide={"a": {"name": "delete_everything"}})
    with pytest.raises(LLMError):
        await llm.decide(purpose="a", system="", context={}, tools=[SEARCH])


class FlakyModels:
    """Stands in for client.aio.models: raises the given errors in order, then answers."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    async def generate_content(self, **_):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "response"


def gemini_with(models) -> GeminiLLM:
    llm = GeminiLLM.__new__(GeminiLLM)  # no real client, no network
    llm.client = SimpleNamespace(aio=SimpleNamespace(models=models))
    llm.model, llm.min_interval_s, llm._last_call = "gemini-flash-latest", 0.0, 0.0
    llm.RETRY_DELAYS = (0, 0, 0, 0)
    llm._lock = asyncio.Lock()
    return llm


class ApiError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


async def test_gemini_retries_rate_limits_then_succeeds():
    models = FlakyModels([ApiError(429), ApiError(503)])
    assert await gemini_with(models)._generate_content([], None) == "response"
    assert models.calls == 3


async def test_gemini_gives_up_with_llm_error_so_callers_can_fall_back():
    models = FlakyModels([ApiError(429)] * 5)
    with pytest.raises(LLMError, match="after retries"):
        await gemini_with(models)._generate_content([], None)
    assert models.calls == 5


async def test_gemini_timeout_is_an_llm_error_not_a_crash():
    models = FlakyModels([TimeoutError("read timed out")])
    with pytest.raises(LLMError):
        await gemini_with(models)._generate_content([], None)
    assert models.calls == 1  # no point retrying a non-retryable error


def test_every_tool_requires_reasoning_and_context_is_json():
    params = _with_reasoning({"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]})
    assert params["required"] == ["q", "reasoning"]
    assert '"phase": "discovery"' in render_context({"phase": "discovery"})

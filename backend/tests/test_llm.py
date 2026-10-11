import pytest
from pydantic import BaseModel

from app.core.llm import FINISH_TOOL, FakeLLM, LLMError, ToolSpec, _with_reasoning, render_context

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


def test_every_tool_requires_reasoning_and_context_is_json():
    params = _with_reasoning({"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]})
    assert params["required"] == ["q", "reasoning"]
    assert '"phase": "discovery"' in render_context({"phase": "discovery"})

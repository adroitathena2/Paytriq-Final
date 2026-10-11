"""The reasoning loop every agent runs inside its LangGraph node.

Each iteration the LLM sees the task context plus everything it has observed so far, picks ONE tool
(function calling) with a short reasoning, the tool runs through ``call_tool`` (timeout, retry,
structured error), and the observation is fed back. The loop ends when the LLM calls ``finish`` or
the budget (max 6 tool calls per agent turn) runs out. Tool failures are observations too, so the LLM
can choose an alternative instead of the run crashing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.llm import FINISH_TOOL, LLMError, ToolSpec
from app.tools.base import call_tool

MAX_TOOL_CALLS = 6


@dataclass
class AgentTool:
    spec: ToolSpec
    fn: Callable[..., Awaitable[dict]]
    timeout: float = 90.0


@dataclass
class LoopResult:
    finished: bool
    summary: str
    observations: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    llm_failed: bool = False


async def run_tool_loop(
    deps, agent: str, purpose: str, system: str, context: dict, tools: list[AgentTool],
    max_calls: int = MAX_TOOL_CALLS,
) -> LoopResult:
    by_name = {t.spec.name: t for t in tools}
    observations: list[dict] = []
    errors: list[dict] = []
    for _ in range(max_calls + 2):  # hard stop even if the model keeps misbehaving
        budget_left = max_calls - len(observations)
        offered = [t.spec for t in tools] + [FINISH_TOOL] if budget_left > 0 else [FINISH_TOOL]
        try:
            call = await deps.llm.decide(
                purpose=purpose, system=system, tools=offered,
                context={**context, "observations": observations, "tool_calls_left": budget_left},
            )
        except LLMError as exc:
            await deps.log(agent, "llm_error", {"purpose": purpose, "error": str(exc)})
            errors.append({"agent": agent, "tool": "llm", "error": str(exc)})
            return LoopResult(False, f"LLM unavailable: {exc}", observations, errors, llm_failed=True)

        await deps.log(agent, "tool_call", {"tool": call.name, "args": call.args, "reasoning": call.reasoning})
        if call.name == "finish":
            return LoopResult(True, str(call.args.get("summary", "")), observations, errors)
        if call.name not in by_name or budget_left <= 0:
            observations.append({"tool": call.name, "result": {"ok": False, "error": "unknown or unavailable tool"}})
            continue

        tool = by_name[call.name]
        result = await call_tool(deps, agent, call.name, tool.fn, call.args, timeout=tool.timeout)
        if not result["ok"]:
            errors.append({"agent": agent, "tool": call.name, "error": result["error"]})
        observations.append({"tool": call.name, "args": call.args, "reasoning": call.reasoning, "result": result})
    return LoopResult(False, "tool-call budget exhausted", observations, errors)

"""Helpers for the inter-agent message bus (the ``messages`` blackboard in EventState).

Agents never call each other directly. They post AgentMessages addressed to another agent; the
supervisor reads the open messages and decides who acts next. A request counts as handled once the
addressed agent posts a message whose ``content.in_reply_to`` lists the request id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.graph.state import AgentMessage, EventState

ACTIONABLE_TYPES = {"request", "revision_request", "escalation"}


def as_message(m) -> AgentMessage:
    return m if isinstance(m, AgentMessage) else AgentMessage.model_validate(m)


def new_message(from_agent: str, to_agent: str, type_: str, content: dict, reasoning: str) -> AgentMessage:
    return AgentMessage(
        id=uuid.uuid4().hex[:12], from_agent=from_agent, to_agent=to_agent, type=type_,
        content=content, reasoning=reasoning, ts=datetime.now(UTC),
    )


async def emit(deps, from_agent: str, to_agent: str, type_: str, content: dict, reasoning: str) -> AgentMessage:
    """Create a message and record it in the trace. The caller adds it to the state update."""
    msg = new_message(from_agent, to_agent, type_, content, reasoning)
    await deps.log(from_agent, "message", msg.model_dump(mode="json"))
    return msg


def handled_ids(state: EventState) -> set[str]:
    done: set[str] = set()
    for m in state.get("messages", []):
        replied = as_message(m).content.get("in_reply_to") or []
        done.update(replied if isinstance(replied, list) else [replied])
    return done


def open_requests(state: EventState, agent: str) -> list[AgentMessage]:
    """Actionable messages addressed to ``agent`` that it has not answered yet."""
    done = handled_ids(state)
    return [
        msg for msg in map(as_message, state.get("messages", []))
        if msg.to_agent == agent and msg.type in ACTIONABLE_TYPES and msg.id not in done
    ]


def compact(msg: AgentMessage) -> dict:
    """Short form of a message for LLM prompts."""
    return {
        "id": msg.id, "from": msg.from_agent, "to": msg.to_agent, "type": msg.type,
        "content": msg.content, "reasoning": msg.reasoning,
    }


def recent(state: EventState, n: int = 10) -> list[dict]:
    return [compact(as_message(m)) for m in state.get("messages", [])[-n:]]

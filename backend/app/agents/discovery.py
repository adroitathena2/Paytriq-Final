"""discovery agent (placeholder until its build phase)."""

from app.graph.messages import emit
from app.graph.state import EventState


async def run(state: EventState, deps) -> dict:
    msg = await emit(deps, "discovery", "supervisor", "result", {"status": "not_implemented"}, "Agent not built yet.")
    return {"messages": [msg], "phase": "blocked"}

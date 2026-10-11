"""Runs: one multi-agent run per event. State is read from the LangGraph checkpointer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.common import Services, effective_status, ensure_idle, get_services, owned_run
from app.core.auth import User, current_user
from app.graph.messages import new_message

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/{run_id}")
async def get_run(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    run = await owned_run(services, run_id, user)
    state = await services.runner.get_state(run_id)
    return {"run": {**run, "status": effective_status(services, run)}, **state}


@router.post("/{run_id}/continue", status_code=202)
async def continue_run(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    """Let the supervisor pick up again, e.g. after a failure or a server restart."""
    run = await owned_run(services, run_id, user)
    ensure_idle(services, run)
    if run["status"] == "completed":
        raise HTTPException(409, "This run is already complete.")
    services.runner.trigger(run_id, {}, "continue")
    return {"status": "running"}


async def trigger_with_request(services: Services, run_id: str, to_agent: str, content: dict, reasoning: str,
                               phase: str, reason: str) -> None:
    """Start a new segment by posting the organizer's request on the message bus."""
    msg = new_message("human", to_agent, "request", content, reasoning)
    await services.deps.tracer.log(run_id, "human", "message", msg.model_dump(mode="json"))
    services.runner.trigger(run_id, {"phase": phase, "messages": [msg]}, reason)


@router.post("/{run_id}/poll-replies", status_code=202)
async def poll_replies(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    """Check the Gmail threads for new replies now (used live in the demo)."""
    run = await owned_run(services, run_id, user)
    ensure_idle(services, run)
    await trigger_with_request(services, run_id, "outreach", {"action": "poll_replies"},
                               "Organizer asked to check for replies.", "replies", "poll_replies")
    return {"status": "running"}

"""Runs: one multi-agent run per event. State is read from the LangGraph checkpointer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.common import Services, effective_status, ensure_idle, get_services, owned_run
from app.core.auth import User, current_user

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

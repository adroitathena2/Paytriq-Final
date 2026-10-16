"""Human-in-the-loop: resume a paused run with the organizer's decision."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.common import Services, get_services, owned_run
from app.core.auth import User, current_user

router = APIRouter(prefix="/runs", tags=["approvals"])


class ApprovalRequest(BaseModel):
    gate: Literal["shortlist", "send", "roi_publish"]
    decision: Literal["approve", "reject"]
    edits: dict = {}


@router.post("/{run_id}/approve", status_code=202)
async def approve(run_id: str, body: ApprovalRequest, user: User = Depends(current_user),
                  services: Services = Depends(get_services)):
    run = await owned_run(services, run_id, user)
    if services.runner.is_busy(run_id):
        raise HTTPException(409, "Agents are still working on this run.")
    if run["status"] != "waiting_approval":
        raise HTTPException(409, "This run is not waiting for an approval.")
    if run["pending_gate"] != body.gate:
        raise HTTPException(409, f"This run is waiting for the '{run['pending_gate']}' gate, not '{body.gate}'.")
    services.runner.resume(run_id, body.model_dump())
    return {"status": "running"}

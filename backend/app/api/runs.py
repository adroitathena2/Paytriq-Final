"""Runs: one multi-agent run per event. State is read from the LangGraph checkpointer."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.common import Services, effective_status, ensure_idle, get_services, owned_run
from app.core.auth import User, current_user
from app.graph.messages import new_message

router = APIRouter(prefix="/runs", tags=["runs"])

# Phases where "continue" should retry the failed step instead of just re-routing.
RETRY_PHASES = {"discovery_failed": "discovery"}


@router.get("")
async def list_runs(user: User = Depends(current_user), services: Services = Depends(get_services)):
    runs = await services.db.list_owner_runs(user.id)
    return [{**r, "status": effective_status(services, r)} for r in runs]


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
    retry = RETRY_PHASES.get(run["phase"])
    services.runner.trigger(run_id, {"phase": retry} if retry else {}, "continue")
    return {"status": "running"}


async def trigger_with_request(services: Services, run_id: str, to_agent: str, content: dict, reasoning: str,
                               phase: str, reason: str, extra: dict | None = None) -> None:
    """Start a new segment by posting the organizer's request on the message bus."""
    msg = new_message("human", to_agent, "request", content, reasoning)
    await services.deps.tracer.log(run_id, "human", "message", msg.model_dump(mode="json"))
    services.runner.trigger(run_id, {"phase": phase, "messages": [msg], **(extra or {})}, reason)


@router.post("/{run_id}/poll-replies", status_code=202)
async def poll_replies(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    """Check the Gmail threads for new replies now (used live in the demo)."""
    run = await owned_run(services, run_id, user)
    ensure_idle(services, run)
    await trigger_with_request(services, run_id, "outreach", {"action": "poll_replies"},
                               "Organizer asked to check for replies.", "replies", "poll_replies")
    return {"status": "running"}


IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@router.post("/{run_id}/evidence", status_code=201)
async def add_evidence(
    run_id: str,
    brand_id: str = Form(..., min_length=1, max_length=80),
    note: str = Form("", max_length=500),
    link: str = Form("", max_length=1000),
    file: UploadFile | None = File(None),
    user: User = Depends(current_user),
    services: Services = Depends(get_services),
):
    """Organizer uploads one photo or one link as evidence for a sponsor's deliverables."""
    await owned_run(services, run_id, user)
    if bool(link.strip()) == bool(file and file.filename):
        raise HTTPException(422, "Provide either a photo or a link, not both or neither.")
    if link.strip():
        if not link.startswith(("http://", "https://")):
            raise HTTPException(422, "The link must start with http:// or https://")
        return await services.db.add_evidence(run_id, {"brand_id": brand_id, "kind": "link", "url": link.strip(),
                                                       "note": note})
    if file.content_type not in IMAGE_TYPES:
        raise HTTPException(415, "Photos must be JPEG, PNG or WebP.")
    data = await file.read()
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Photos must be between 1 byte and 8 MB.")
    path = f"{run_id}/{uuid.uuid4().hex}.{IMAGE_TYPES[file.content_type]}"
    try:
        await services.deps.storage.upload("evidence", path, data, file.content_type)
    except Exception as exc:
        raise HTTPException(502, "Could not store the photo right now; please retry.") from exc
    return await services.db.add_evidence(run_id, {"brand_id": brand_id, "kind": "photo", "storage_path": path,
                                                   "mime_type": file.content_type, "note": note})


@router.get("/{run_id}/evidence")
async def list_evidence(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    await owned_run(services, run_id, user)
    return await services.db.list_evidence(run_id)


@router.post("/{run_id}/audit", status_code=202)
async def start_audit(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    """After the event: the ROI Audit agent verifies deliverables against the uploaded evidence."""
    run = await owned_run(services, run_id, user)
    ensure_idle(services, run)
    evidence = [{k: str(v) if k in ("id", "run_id", "created_at") else v for k, v in e.items()}
                for e in await services.db.list_evidence(run_id)]
    await trigger_with_request(services, run_id, "roi_audit", {"action": "audit", "evidence_items": len(evidence)},
                               "Organizer asked for the post-event ROI audit.", "roi", "audit",
                               extra={"evidence": evidence})
    return {"status": "running", "evidence_items": len(evidence)}


@router.get("/{run_id}/report")
async def get_report(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    await owned_run(services, run_id, user)
    report = (await services.runner.get_state(run_id))["values"].get("roi_report")
    if not report:
        raise HTTPException(404, "No ROI report yet. Upload evidence and run the audit first.")
    return report

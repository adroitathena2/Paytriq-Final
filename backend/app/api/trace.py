"""Read-only access to the execution trace captured during real runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from fastapi.encoders import jsonable_encoder

from app.api.common import Services, get_services, owned_run
from app.core.auth import User, current_user

router = APIRouter(prefix="/runs", tags=["trace"])


@router.get("/{run_id}/trace")
async def get_trace(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    await owned_run(services, run_id, user)
    return await services.db.list_trace(run_id)


@router.get("/{run_id}/trace/export")
async def export_trace(run_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    run = await owned_run(services, run_id, user)
    entries = await services.db.list_trace(run_id)
    document = {
        "run_id": run_id,
        "event_id": run["event_id"],
        "exported_at": datetime.now(UTC).isoformat(),
        "note": "Captured from a live Paytriq run. Unedited.",
        "entries": jsonable_encoder(entries),
    }
    return Response(
        content=json.dumps(document, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="paytriq-trace-{run_id}.json"'},
    )

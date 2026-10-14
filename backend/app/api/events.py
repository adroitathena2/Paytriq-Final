"""Events: what the organizer is raising sponsorship for. Creating a run starts the agents."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.api.common import Services, effective_status, get_services, owned_event
from app.core.auth import User, current_user
from app.demo import world

router = APIRouter(prefix="/events", tags=["events"])


class EventCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    college: str = Field(min_length=2, max_length=160)
    event_date: date
    expected_footfall: int = Field(ge=10, le=200_000)
    audience: str = Field(min_length=3, max_length=500, description="Who attends, e.g. '18-22 engineering students'")
    budget_goal_inr: int = Field(ge=0, le=100_000_000)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)

    @field_validator("name", "college", "audience")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class DemoEventCreate(BaseModel):
    rehearsal: bool = Field(False, description="True: offline rehearsal with a scripted LLM (not for submission)")


def state_event(row: dict) -> dict:
    """The event as agents see it (EventState.event)."""
    return {
        "name": row["name"], "college": row["college"], "date": str(row["event_date"]),
        "expected_footfall": row["expected_footfall"], "audience": row["audience"],
        "budget_goal_inr": row["budget_goal_inr"], "lat": row["lat"], "lng": row["lng"],
        "demo_scenario": row.get("demo_scenario"),
    }


@router.post("", status_code=201)
async def create_event(body: EventCreate, user: User = Depends(current_user), services: Services = Depends(get_services)):
    return await services.db.create_event(user.id, body.model_dump())


@router.post("/demo", status_code=201)
async def create_demo_event(body: DemoEventCreate | None = None, user: User = Depends(current_user),
                            services: Services = Depends(get_services)):
    """A demo event in the fixture world (app/demo/world.py); also resets the seeded past deals."""
    scenario = "rehearsal" if body and body.rehearsal else "demo"
    event = await services.db.create_event(user.id, {**EventCreate(**world.EVENT).model_dump(),
                                                     "demo_scenario": scenario})
    await world.seed_deals(services.db, user.id)
    return event


@router.get("")
async def list_events(user: User = Depends(current_user), services: Services = Depends(get_services)):
    return await services.db.list_events(user.id)


@router.get("/{event_id}")
async def get_event(event_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    event = await owned_event(services, event_id, user)
    runs = await services.db.list_runs(event_id)
    return {**event, "runs": [{**r, "status": effective_status(services, r)} for r in runs]}


@router.post("/{event_id}/runs", status_code=202)
async def start_run(event_id: str, user: User = Depends(current_user), services: Services = Depends(get_services)):
    event = await owned_event(services, event_id, user)
    run = await services.db.create_run(event_id, user.id)
    services.runner.start(run["id"], event_id, state_event(event))
    return run

"""Shared API helpers: the service container and ownership checks."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.core.auth import User
from app.graph.deps import Deps
from app.graph.runner import RunManager


@dataclass
class Services:
    deps: Deps
    runner: RunManager

    @property
    def db(self):
        return self.deps.db


def get_services(request: Request) -> Services:
    return request.app.state.services


async def owned_event(services: Services, event_id: str, user: User) -> dict:
    event = await services.db.get_event(event_id)
    if not event or event["owner_id"] != user.id:
        raise HTTPException(404, "Event not found")
    return event


async def owned_run(services: Services, run_id: str, user: User) -> dict:
    run = await services.db.get_run(run_id)
    if not run or run["owner_id"] != user.id:
        raise HTTPException(404, "Run not found")
    return run


def ensure_idle(services: Services, run: dict) -> None:
    """Refuse to start new work while agents are still working on this run or an approval is pending."""
    if services.runner.is_busy(run["id"]):
        raise HTTPException(409, "Agents are still working on this run; try again when they finish.")
    if run["status"] == "waiting_approval":
        raise HTTPException(409, f"Approve or reject the pending '{run['pending_gate']}' step first.")


def effective_status(services: Services, run: dict) -> str:
    """A run marked running whose task is gone (e.g. after a server restart) is reported as stalled."""
    if run["status"] == "running" and not services.runner.is_busy(run["id"]):
        return "stalled"
    return run["status"]

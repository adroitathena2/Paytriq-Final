"""Runs the graph in the background for each run (thread_id = run_id) and keeps the runs table in sync.

Agent work takes minutes (many LLM calls), so API calls only schedule work and return immediately;
the frontend polls ``GET /runs/{id}``. A run moves between: running -> waiting_approval | idle |
completed | failed. Any exception is caught, traced and stored, never propagated to the server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi.encoders import jsonable_encoder
from langgraph.types import Command

from app.graph.deps import Deps
from app.graph.state import initial_state

log = logging.getLogger(__name__)


class RunManager:
    def __init__(self, graph, deps: Deps) -> None:
        self.graph = graph
        self.deps = deps
        self.tasks: dict[str, asyncio.Task] = {}

    @staticmethod
    def config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 200}

    def is_busy(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        return bool(task and not task.done())

    def start(self, run_id: str, event_id: str, event: dict) -> asyncio.Task:
        return self._schedule(run_id, initial_state(event_id, event), "start")

    def resume(self, run_id: str, answer: dict) -> asyncio.Task:
        return self._schedule(run_id, Command(resume=answer), "approve")

    def trigger(self, run_id: str, update: dict, reason: str) -> asyncio.Task:
        """Start a new segment on an existing run, e.g. polling replies or auditing evidence."""
        return self._schedule(run_id, {"step_count": 0, **update}, reason)

    def _schedule(self, run_id: str, payload: Any, reason: str) -> asyncio.Task:
        task = asyncio.create_task(self._execute(run_id, payload, reason))
        self.tasks[run_id] = task
        return task

    async def _execute(self, run_id: str, payload: Any, reason: str) -> None:
        db, tracer = self.deps.db, self.deps.tracer
        await db.update_run(run_id, status="running", last_error=None)
        await tracer.log(run_id, "system", "run_start", {"trigger": reason})
        try:
            await self.graph.ainvoke(payload, config=self.config(run_id))
        except Exception as exc:
            log.exception("run %s failed", run_id)
            await tracer.log(run_id, "system", "error", {"error": f"{type(exc).__name__}: {exc}"})
            await db.update_run(run_id, status="failed", last_error=f"{type(exc).__name__}: {exc}")
            return
        await self._sync_status(run_id)

    async def _sync_status(self, run_id: str) -> None:
        snap = await self.graph.aget_state(self.config(run_id))
        phase = snap.values.get("phase", "")
        interrupts = [i for task in snap.tasks for i in task.interrupts]
        if interrupts:
            gate = (interrupts[0].value or {}).get("gate")
            await self.deps.tracer.log(run_id, "human_approval", "interrupt", {"gate": gate})
            await self.deps.db.update_run(run_id, status="waiting_approval", phase=phase, pending_gate=gate)
        else:
            status = "completed" if phase == "done" else "idle"
            await self.deps.db.update_run(run_id, status=status, phase=phase, pending_gate=None)
        await self.deps.tracer.log(run_id, "system", "run_end", {"phase": phase})

    async def get_state(self, run_id: str) -> dict:
        snap = await self.graph.aget_state(self.config(run_id))
        interrupts = [i.value for task in snap.tasks for i in task.interrupts]
        return {"values": jsonable_encoder(snap.values), "interrupt": jsonable_encoder(interrupts[0]) if interrupts else None}

    async def shutdown(self) -> None:
        for task in self.tasks.values():
            task.cancel()

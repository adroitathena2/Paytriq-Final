"""Execution trace: every agent decision, message and tool call of a real run.

``trace.log(run_id, agent, event_type, payload)`` writes one JSON line to stdout and one row to the
``agent_trace`` table. Traces are only ever produced by the running system; nothing here edits them.
A failing trace write is logged and swallowed so it can never crash a run.

Runs of a *rehearsal* demo event (scripted LLM, see app/demo/rehearsal.py) get ``REHEARSAL_MARK`` in
every entry's payload, so a rehearsal trace can never be mistaken for real reasoning.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

log = logging.getLogger("paytriq.trace")

EVENT_TYPES = {
    "run_start", "run_end", "message", "tool_call", "tool_result", "tool_error",
    "llm_error", "guardrail", "decision", "interrupt", "resume", "error",
}
REHEARSAL_MARK = "rehearsal — not real reasoning"


class Tracer:
    def __init__(self, db=None, echo: bool = True) -> None:
        self.db = db
        self.echo = echo
        self.scenarios: dict[str, str | None] = {}  # run_id -> demo_scenario of its event (cached)

    def bind(self, db) -> None:
        self.db = db

    async def scenario(self, run_id: str) -> str | None:
        if run_id not in self.scenarios and self.db is not None:
            try:
                run = await self.db.get_run(run_id)
                event = await self.db.get_event(run["event_id"]) if run else None
                self.scenarios[run_id] = (event or {}).get("demo_scenario")
            except Exception:  # unknown for now; ask again next time
                return None
        return self.scenarios.get(run_id)

    async def log(self, run_id: str, agent: str, event_type: str, payload: dict) -> None:
        payload = json.loads(json.dumps(payload, default=str))  # make sure it is plain JSON
        if await self.scenario(run_id) == "rehearsal":
            payload["rehearsal"] = REHEARSAL_MARK
        if self.echo:
            line = {"ts": datetime.now(UTC).isoformat(), "run_id": run_id, "agent": agent,
                    "event_type": event_type, "payload": payload}
            sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
        if self.db is None:
            return
        try:
            await self.db.add_trace(run_id, agent, event_type, payload)
        except Exception as exc:  # the trace must never take the run down with it
            log.warning("could not persist trace entry: %s", exc)


trace = Tracer()

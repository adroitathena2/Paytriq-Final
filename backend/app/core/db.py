"""Persistence for events, runs, the agent trace and ROI evidence.

``PostgresDB`` talks to Supabase Postgres directly (DATABASE_URL). ``MemoryDB`` keeps everything in
process memory and is used by tests and for local development without a database.
Graph state itself is stored by the LangGraph checkpointer, not here.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

RUN_FIELDS = {"status", "phase", "pending_gate", "last_error"}


def _now() -> datetime:
    return datetime.now(UTC)


class Database(Protocol):
    async def create_event(self, owner_id: str, data: dict) -> dict: ...
    async def list_events(self, owner_id: str) -> list[dict]: ...
    async def get_event(self, event_id: str) -> dict | None: ...
    async def create_run(self, event_id: str, owner_id: str) -> dict: ...
    async def get_run(self, run_id: str) -> dict | None: ...
    async def list_runs(self, event_id: str) -> list[dict]: ...
    async def list_owner_runs(self, owner_id: str) -> list[dict]: ...
    async def update_run(self, run_id: str, **fields: Any) -> None: ...
    async def add_trace(self, run_id: str, agent: str, event_type: str, payload: dict) -> None: ...
    async def list_trace(self, run_id: str) -> list[dict]: ...
    async def add_evidence(self, run_id: str, data: dict) -> dict: ...
    async def list_evidence(self, run_id: str) -> list[dict]: ...
    async def close(self) -> None: ...


class MemoryDB:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.trace: list[dict] = []
        self.evidence: list[dict] = []

    async def create_event(self, owner_id: str, data: dict) -> dict:
        event = {"id": str(uuid.uuid4()), "owner_id": owner_id, "created_at": _now(), **data}
        self.events[event["id"]] = event
        return event

    async def list_events(self, owner_id: str) -> list[dict]:
        rows = [e for e in self.events.values() if e["owner_id"] == owner_id]
        return sorted(rows, key=lambda e: e["created_at"], reverse=True)

    async def get_event(self, event_id: str) -> dict | None:
        return self.events.get(event_id)

    async def create_run(self, event_id: str, owner_id: str) -> dict:
        now = _now()
        run = {
            "id": str(uuid.uuid4()), "event_id": event_id, "owner_id": owner_id, "status": "running",
            "phase": "discovery", "pending_gate": None, "last_error": None, "created_at": now, "updated_at": now,
        }
        self.runs[run["id"]] = run
        return run

    async def get_run(self, run_id: str) -> dict | None:
        return self.runs.get(run_id)

    async def list_runs(self, event_id: str) -> list[dict]:
        rows = [r for r in self.runs.values() if r["event_id"] == event_id]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    async def list_owner_runs(self, owner_id: str) -> list[dict]:
        rows = [{**r, "event_name": self.events.get(r["event_id"], {}).get("name")}
                for r in self.runs.values() if r["owner_id"] == owner_id]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:50]

    async def update_run(self, run_id: str, **fields: Any) -> None:
        self.runs[run_id].update({k: v for k, v in fields.items() if k in RUN_FIELDS}, updated_at=_now())

    async def add_trace(self, run_id: str, agent: str, event_type: str, payload: dict) -> None:
        self.trace.append({
            "id": len(self.trace) + 1, "run_id": run_id, "ts": _now(), "agent": agent,
            "event_type": event_type, "payload": payload,
        })

    async def list_trace(self, run_id: str) -> list[dict]:
        return [t for t in self.trace if t["run_id"] == run_id]

    async def add_evidence(self, run_id: str, data: dict) -> dict:
        row = {"id": str(uuid.uuid4()), "run_id": run_id, "created_at": _now(), **data}
        self.evidence.append(row)
        return row

    async def list_evidence(self, run_id: str) -> list[dict]:
        return [e for e in self.evidence if e["run_id"] == run_id]

    async def close(self) -> None:
        return None


EVENT_COLUMNS = ("name", "college", "event_date", "expected_footfall", "audience", "budget_goal_inr", "lat", "lng")
EVIDENCE_COLUMNS = ("brand_id", "kind", "url", "storage_path", "mime_type", "note")


class PostgresDB:
    """Direct Postgres access. Uses the service connection, so RLS is bypassed: every API route
    checks ownership itself before reading or writing."""

    def __init__(self, pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresDB:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            database_url, min_size=1, max_size=5, open=False,
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        )
        await pool.open()
        return cls(pool)

    async def _fetch(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall() if cur.description else []

    async def create_event(self, owner_id: str, data: dict) -> dict:
        cols = ", ".join(EVENT_COLUMNS)
        marks = ", ".join(["%s"] * (len(EVENT_COLUMNS) + 1))
        rows = await self._fetch(
            f"insert into events (owner_id, {cols}) values ({marks}) returning *",
            (owner_id, *[data.get(c) for c in EVENT_COLUMNS]),
        )
        return _jsonable(rows[0])

    async def list_events(self, owner_id: str) -> list[dict]:
        rows = await self._fetch("select * from events where owner_id = %s order by created_at desc", (owner_id,))
        return [_jsonable(r) for r in rows]

    async def get_event(self, event_id: str) -> dict | None:
        rows = await self._fetch("select * from events where id = %s", (event_id,))
        return _jsonable(rows[0]) if rows else None

    async def create_run(self, event_id: str, owner_id: str) -> dict:
        rows = await self._fetch(
            "insert into runs (event_id, owner_id) values (%s, %s) returning *", (event_id, owner_id)
        )
        return _jsonable(rows[0])

    async def get_run(self, run_id: str) -> dict | None:
        rows = await self._fetch("select * from runs where id = %s", (run_id,))
        return _jsonable(rows[0]) if rows else None

    async def list_runs(self, event_id: str) -> list[dict]:
        rows = await self._fetch("select * from runs where event_id = %s order by created_at desc", (event_id,))
        return [_jsonable(r) for r in rows]

    async def list_owner_runs(self, owner_id: str) -> list[dict]:
        rows = await self._fetch(
            "select r.*, e.name as event_name from runs r join events e on e.id = r.event_id "
            "where r.owner_id = %s order by r.created_at desc limit 50", (owner_id,))
        return [_jsonable(r) for r in rows]

    async def update_run(self, run_id: str, **fields: Any) -> None:
        fields = {k: v for k, v in fields.items() if k in RUN_FIELDS}
        if not fields:
            return
        sets = ", ".join(f"{k} = %s" for k in fields)
        await self._fetch(f"update runs set {sets}, updated_at = now() where id = %s", (*fields.values(), run_id))

    async def add_trace(self, run_id: str, agent: str, event_type: str, payload: dict) -> None:
        await self._fetch(
            "insert into agent_trace (run_id, agent, event_type, payload) values (%s, %s, %s, %s)",
            (run_id, agent, event_type, json.dumps(payload, default=str)),
        )

    async def list_trace(self, run_id: str) -> list[dict]:
        rows = await self._fetch("select * from agent_trace where run_id = %s order by id", (run_id,))
        return [_jsonable(r) for r in rows]

    async def add_evidence(self, run_id: str, data: dict) -> dict:
        cols = ", ".join(EVIDENCE_COLUMNS)
        marks = ", ".join(["%s"] * (len(EVIDENCE_COLUMNS) + 1))
        rows = await self._fetch(
            f"insert into evidence (run_id, {cols}) values ({marks}) returning *",
            (run_id, *[data.get(c) for c in EVIDENCE_COLUMNS]),
        )
        return _jsonable(rows[0])

    async def list_evidence(self, run_id: str) -> list[dict]:
        rows = await self._fetch("select * from evidence where run_id = %s order by created_at", (run_id,))
        return [_jsonable(r) for r in rows]

    async def close(self) -> None:
        await self.pool.close()


def _jsonable(row: dict) -> dict:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}


async def build_db(settings) -> Database:
    if settings.database_url:
        return await PostgresDB.connect(settings.database_url)
    return MemoryDB()

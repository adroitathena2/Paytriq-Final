"""Builds the LangGraph StateGraph.

Topology: START -> supervisor -> (discovery | proposal | outreach | roi_audit | human_approval | END),
and every worker node returns to the supervisor. There is no fixed agent order: the supervisor picks
the next node each step (see graph/supervisor.py).
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from app import demo
from app.agents import discovery, outreach, proposal, roi_audit
from app.graph import approval, supervisor
from app.graph.deps import Deps
from app.graph.state import CHECKPOINT_TYPES, EventState

log = logging.getLogger(__name__)

WORKERS = {
    "discovery": discovery.run,
    "proposal": proposal.run,
    "outreach": outreach.run,
    "roi_audit": roi_audit.run,
    "human_approval": approval.run,
}


def _bind(fn, deps: Deps):
    async def node(state: EventState, config):
        run_deps = demo.scenario_deps(deps.for_run(config["configurable"]["thread_id"]), state)
        return await fn(state, run_deps)

    node.__name__ = getattr(fn, "__module__", "node")
    return node


def serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)


def build_graph(deps: Deps, checkpointer=None):
    graph = StateGraph(EventState)
    graph.add_node("supervisor", _bind(supervisor.run, deps), destinations=(*WORKERS, END))
    for name, fn in WORKERS.items():
        graph.add_node(name, _bind(fn, deps))
        graph.add_edge(name, "supervisor")
    graph.add_edge(START, "supervisor")
    return graph.compile(checkpointer=checkpointer or InMemorySaver(serde=serializer()))


async def build_checkpointer(settings):
    """Postgres checkpointer so paused runs survive restarts; in-memory fallback without DATABASE_URL.

    Returns ``(checkpointer, pool_or_None)``; the caller closes the pool on shutdown.
    """
    if not settings.database_url:
        log.warning("DATABASE_URL not set: using in-memory checkpointer (runs are lost on restart)")
        return InMemorySaver(serde=serializer()), None
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        settings.database_url, min_size=1, max_size=5, open=False,
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool, serde=serializer())
    await saver.setup()
    return saver, pool

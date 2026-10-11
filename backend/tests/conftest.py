"""Shared fixtures. Tests never touch the network: FakeLLM, fake tools and MemoryDB only."""

import pytest

from app.core.config import Settings
from app.core.db import MemoryDB
from app.core.trace import Tracer
from app.graph.deps import Deps


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, auth_disabled=True, demo_mode=True, demo_recipients="team1@example.com,team2@example.com",
        gemini_api_key="", database_url="", supabase_url="", supabase_service_role_key="",
        google_maps_api_key="", gmail_refresh_token="", public_api_url="http://testserver", tool_retry_backoff_s=0,
    )


@pytest.fixture
def db() -> MemoryDB:
    return MemoryDB()


@pytest.fixture
def tracer(db) -> Tracer:
    return Tracer(db=db, echo=False)


@pytest.fixture
def make_deps(db, tracer, settings, tmp_path):
    from app.core.storage import Storage

    def _make(llm, **tools) -> Deps:
        storage = tools.pop("storage", Storage(settings, local_dir=tmp_path / "storage"))
        return Deps(llm=llm, db=db, tracer=tracer, settings=settings, storage=storage, run_id="run-test", **tools)

    return _make

"""FastAPI entry point: wires settings, database, tools, LLM and the LangGraph graph together."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api import approvals, events, runs, trace
from app.api.common import Services
from app.core.config import Settings, get_settings
from app.core.db import build_db
from app.core.llm import build_llm
from app.core.storage import Storage
from app.core.trace import trace as tracer
from app.graph.build import build_checkpointer, build_graph
from app.graph.deps import Deps
from app.graph.runner import RunManager
from app.tools.pdf import PdfTool
from app.tools.places import PlacesTool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("paytriq")


def build_deps(settings: Settings, db) -> Deps:
    tracer.bind(db)
    storage = Storage(settings)
    return Deps(
        llm=build_llm(settings), db=db, tracer=tracer, settings=settings, storage=storage,
        places=PlacesTool(settings.google_maps_api_key, settings.overpass_url), pdf=PdfTool(storage),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(app.state, "services", None) is not None:  # injected by tests
        yield
        return
    settings = get_settings()
    db = await build_db(settings)
    deps = build_deps(settings, db)
    checkpointer, pool = await build_checkpointer(settings)
    app.state.services = Services(deps=deps, runner=RunManager(build_graph(deps, checkpointer), deps))
    log.info("Paytriq API ready (demo_mode=%s, model=%s)", settings.demo_mode, settings.gemini_model)
    yield
    await app.state.services.runner.shutdown()
    await db.close()
    if pool is not None:
        await pool.close()


def create_app(services: Services | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Paytriq API", version="0.1.0", lifespan=lifespan)
    app.state.services = services
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    for module in (events, runs, approvals, trace):
        app.include_router(module.router)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Something went wrong on our side. Please retry."})

    @app.get("/health")
    async def health():
        s = get_settings()
        return {"status": "ok", "demo_mode": s.demo_mode, "llm_configured": bool(s.gemini_api_key),
                "database": "postgres" if s.database_url else "memory"}

    @app.get("/files/{bucket}/{path:path}")
    async def local_file(bucket: str, path: str, request: Request):
        """Serves public files stored locally when Supabase Storage is not configured (development)."""
        file = request.app.state.services.deps.storage.local_file(bucket, path)
        if file is None:
            raise HTTPException(404, "File not found")
        return FileResponse(file)

    return app


app = create_app()

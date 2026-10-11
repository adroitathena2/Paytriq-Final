"""Dependencies handed to every agent node, so tests can inject fakes (FakeLLM, fake tools, MemoryDB)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.trace import Tracer


@dataclass
class Deps:
    llm: Any
    db: Any
    tracer: Tracer
    settings: Settings
    places: Any = None  # tools.places.PlacesTool
    pdf: Any = None  # tools.pdf.PdfTool
    gmail: Any = None  # tools.gmail.GmailTool
    vision: Any = None  # tools.vision.VisionTool
    storage: Any = None  # core.storage.Storage
    run_id: str = ""

    def for_run(self, run_id: str) -> Deps:
        return dataclasses.replace(self, run_id=run_id)

    async def log(self, agent: str, event_type: str, payload: dict) -> None:
        await self.tracer.log(self.run_id, agent, event_type, payload)

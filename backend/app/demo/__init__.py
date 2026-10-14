"""Demo mode: the agents run in a fixture world (businesses, inbox, evidence) instead of the real one.

``"demo"`` keeps the real LLM (Gemini) and Clef for every decision; ``"rehearsal"`` swaps in a scripted LLM
and a scripted Clef for offline practice and is marked as such everywhere. The scenario is stored on the event row and copied
into ``EventState.event["demo_scenario"]``, so it survives restarts.
"""

from __future__ import annotations

import dataclasses

from app.demo.rehearsal import rehearsal_clef, rehearsal_llm
from app.demo.world import DemoInbox, DemoPlaces
from app.tools.vision import VisionTool

SCENARIOS = ("demo", "rehearsal")


def scenario_deps(deps, state: dict):
    """The dependencies one agent step uses: fixture tools for demo events, unchanged otherwise."""
    scenario = (state.get("event") or {}).get("demo_scenario")
    if scenario not in SCENARIOS:
        return deps
    rehearsal = scenario == "rehearsal"
    llm = rehearsal_llm() if rehearsal else deps.llm
    return dataclasses.replace(deps, llm=llm, clef=rehearsal_clef() if rehearsal else deps.clef,
                               places=DemoPlaces(), gmail=DemoInbox(state),
                               vision=VisionTool(llm, deps.storage))

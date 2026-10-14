"""The compiled graph pauses at human gates (interrupt) and resumes with the organizer's decision."""

from langgraph.types import Command

from app.core.llm import FakeLLM
from app.graph.build import build_graph
from app.graph.messages import as_message
from tests.factories import brand, state


def route(ctx):
    """Scripted supervisor: open the shortlist gate, then stop."""
    if ctx["phase"] == "shortlist_review":
        return {"next": "human_approval", "gate": "shortlist", "reason": "organizer must pick brands"}
    return {"next": "END", "reason": "nothing left this segment"}


async def test_shortlist_gate_interrupts_and_resumes(make_deps):
    graph = build_graph(make_deps(FakeLLM(generate={"supervisor.route": route})))
    config = {"configurable": {"thread_id": "run-1"}}
    start = state(phase="shortlist_review", brands=[brand(1, 0.9), brand(2, 0.7), brand(3, 0.2)])

    await graph.ainvoke(start, config)
    snap = await graph.aget_state(config)
    [pending] = [i for t in snap.tasks for i in t.interrupts]
    assert pending.value["gate"] == "shortlist"
    assert pending.value["payload"]["suggested_brand_ids"] == ["b1", "b2"]

    resume = {"gate": "shortlist", "decision": "approve", "edits": {"selected_brand_ids": ["b2", "nope"]}}
    await graph.ainvoke(Command(resume=resume), config)
    values = (await graph.aget_state(config)).values
    assert values["selected_brand_ids"] == ["b2"]  # unknown ids are dropped
    assert values["phase"] == "proposals"
    human = [as_message(m) for m in values["messages"] if as_message(m).from_agent == "human"]
    assert human and human[0].content["decision"] == "approve"
    # Brand models survive the checkpointer round trip
    assert values["brands"][0].name == "Brand 1"


async def test_rejected_shortlist_becomes_a_request_for_discovery(make_deps):
    graph = build_graph(make_deps(FakeLLM(generate={"supervisor.route": route})))
    config = {"configurable": {"thread_id": "run-2"}}
    await graph.ainvoke(state(phase="shortlist_review", brands=[brand(1)]), config)
    resume = {"gate": "shortlist", "decision": "reject", "edits": {"feedback": "No cafes, we have one on campus"}}
    await graph.ainvoke(Command(resume=resume), config)
    values = (await graph.aget_state(config)).values
    request = [as_message(m) for m in values["messages"] if as_message(m).to_agent == "discovery"][0]
    assert request.content["feedback"] == "No cafes, we have one on campus"

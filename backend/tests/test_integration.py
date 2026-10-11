"""End-to-end run of the full LangGraph graph with fakes (no network).

Three segments, as in the live demo:
1. start -> discovery (self-replan, L1) -> shortlist gate -> proposals -> outreach critique asks for a
   revision (L2) -> v2 -> send gate -> emails sent.
2. poll replies -> pushback routed to Proposal (L3), decline routed to Discovery for a replacement (L4),
   one acceptance -> new proposals -> critique -> send gate -> sent.
3. evidence + audit -> ROI finds gaps and escalates (L5) -> supervisor arbitrates -> publish gate -> done.

The trace must contain every coordination loop.
"""

from app.core.llm import FakeLLM
from app.graph.build import build_graph
from app.graph.messages import new_message
from app.graph.runner import RunManager
from app.graph.supervisor import fallback_route
from app.tools.vision import VisionTool
from tests.factories import EVENT
from tests.fakes import (
    FakeGmail,
    FakePdf,
    FakePlaces,
    classify,
    critique,
    discovery_plan,
    discovery_replace,
    make_brand,
    outreach_plan,
    proposal_plan,
    proposal_write,
    roi_plan,
    score_by_name,
    vision_by_note,
)


def supervisor_policy(ctx):
    """Scripted supervisor: follow open requests and phases; on ROI gaps choose to publish with disclosure."""
    phase = ctx["phase"]
    for agent, requests in ctx["open_requests"].items():
        if requests:
            return {"next": agent, "reason": f"{agent} has {len(requests)} open request(s)"}
    if phase == "roi_review":
        return {"next": "human_approval", "gate": "roi_publish",
                "reason": "1 of 3 deliverables verified; publish with the gaps disclosed rather than wait"}
    table = {"discovery": ("discovery", None), "shortlist_review": ("human_approval", "shortlist"),
             "proposals": ("proposal", None), "review": ("outreach", None),
             "send_review": ("human_approval", "send"), "sending": ("outreach", None)}
    nxt, gate = table.get(phase, ("END", None))
    return {"next": nxt, "gate": gate, "reason": f"phase {phase}"}


def places_results(radius, categories):
    near = [make_brand("Good Brew Cafe", km=0.4), make_brand("Good Grind Cafe", km=0.8),
            make_brand("Meh Gym", "gym", km=1.2)]
    far = [make_brand("Good Fit Gym", "gym", km=3.1), make_brand("Good Bakes", "bakery", km=3.5),
           make_brand("Good Crumbs", "bakery", km=3.9)]
    return near if radius < 4 else near + far


async def test_full_run_exercises_every_coordination_loop(make_deps, db, settings):
    llm = FakeLLM(
        decide={"discovery.plan": discovery_plan, "discovery.replace": discovery_replace,
                "proposal.plan": proposal_plan, "outreach.plan": outreach_plan, "roi.plan": roi_plan},
        generate={"supervisor.route": supervisor_policy, "discovery.score": score_by_name,
                  "discovery.replan": {"radius_km": 4, "extra_categories": ["bakery"],
                                       "reasoning": "only 2 good-fit brands within 2 km"},
                  "proposal.write": proposal_write, "outreach.critique": critique,
                  "outreach.classify": classify, "roi.vision": vision_by_note},
    )
    gmail = FakeGmail(settings)
    deps = make_deps(llm, places=FakePlaces(places_results), pdf=FakePdf(), gmail=gmail)
    deps.vision = VisionTool(llm, deps.storage)  # real vision tool, fake model
    graph = build_graph(deps)
    runner = RunManager(graph, deps)
    run = await db.create_run("event-1", "user-1")
    rid = run["id"]

    async def values():
        return (await runner.get_state(rid))["values"]

    # Segment 1 ------------------------------------------------------------------------------------
    await runner.start(rid, "event-1", dict(EVENT))
    assert (await db.get_run(rid))["pending_gate"] == "shortlist"
    v = await values()
    assert len(v["brands"]) == 6
    pick = ["good-brew-cafe", "good-grind-cafe", "good-fit-gym"]
    await runner.resume(rid, {"gate": "shortlist", "decision": "approve", "edits": {"selected_brand_ids": pick}})
    assert (await db.get_run(rid))["pending_gate"] == "send"
    emails = (await runner.get_state(rid))["interrupt"]["payload"]["emails"]
    assert {e["brand_id"] for e in emails} == set(pick)
    await runner.resume(rid, {"gate": "send", "decision": "approve", "edits": {}})
    run_row = await db.get_run(rid)
    assert (run_row["status"], run_row["phase"]) == ("idle", "awaiting_replies")
    assert len(gmail.sent) == 3 and all(s["to"].endswith("@example.com") for s in gmail.sent)

    # Segment 2: replies ---------------------------------------------------------------------------
    v = await values()
    threads = {b: t["thread_id"] for b, t in v["outreach"].items()}
    gmail.reply(threads["good-brew-cafe"], "Too expensive for our budget right now.")
    gmail.reply(threads["good-grind-cafe"], "Thanks, but not interested.")
    gmail.reply(threads["good-fit-gym"], "We will go with Gold, see you there!")
    poll = new_message("human", "outreach", "request", {"action": "poll_replies"}, "check replies")
    await runner.trigger(rid, {"phase": "replies", "messages": [poll]}, "poll_replies")
    assert (await db.get_run(rid))["pending_gate"] == "send"
    v = await values()
    assert v["proposals"]["good-brew-cafe"]["version"] >= 2
    assert max(t["price_inr"] for t in v["proposals"]["good-brew-cafe"]["tiers"]) <= 15000
    replacement = v["selected_brand_ids"][-1]
    assert replacement not in pick and v["outreach"]["good-grind-cafe"]["status"] == "declined"
    await runner.resume(rid, {"gate": "send", "decision": "approve", "edits": {}})
    v = await values()
    assert v["outreach"]["good-brew-cafe"]["thread_id"] == threads["good-brew-cafe"]  # v2 sent in the same thread
    assert v["outreach"][replacement]["status"] == "sent"

    # Segment 3: ROI -------------------------------------------------------------------------------
    await deps.storage.upload("evidence", f"{rid}/banner.jpg", b"\xff\xd8x", "image/jpeg")
    evidence = [{"id": "ev1", "brand_id": "good-fit-gym", "kind": "photo", "storage_path": f"{rid}/banner.jpg",
                 "mime_type": "image/jpeg", "note": "clear: Logo on main stage banner"}]
    audit = new_message("human", "roi_audit", "request", {"action": "audit"}, "event is over")
    await runner.trigger(rid, {"phase": "roi", "messages": [audit], "evidence": evidence}, "audit")
    assert (await db.get_run(rid))["pending_gate"] == "roi_publish"
    await runner.resume(rid, {"gate": "roi_publish", "decision": "approve", "edits": {}})
    run_row = await db.get_run(rid)
    assert (run_row["status"], run_row["phase"]) == ("completed", "done")
    report = (await values())["roi_report"]
    assert report["published"] and len(report["disclosed_gaps"]) == 2

    # The trace shows every coordination loop --------------------------------------------------------
    trace = await db.list_trace(rid)
    msgs = [t["payload"] for t in trace if t["event_type"] == "message"]
    assert any(m["type"] == "replan" for m in msgs), "L1 self-replan"
    assert any(m["type"] == "revision_request" and m["content"].get("source") == "critique" for m in msgs), "L2"
    assert any(m["type"] == "revision_request" and m["content"].get("source") == "brand_reply" for m in msgs), "L3"
    assert any(m["type"] == "request" and m["content"].get("reason") == "replacement" for m in msgs), "L4"
    assert any(m["type"] == "escalation" and m["from_agent"] == "roi_audit" for m in msgs), "L5"
    decisions = [m for m in msgs if m["from_agent"] == "supervisor" and m["type"] == "decision"]
    assert all(d["content"]["source"] == "llm" for d in decisions)
    assert {d["content"]["next"] for d in decisions} >= {"discovery", "proposal", "outreach", "roi_audit",
                                                         "human_approval", "END"}
    # sanity: the deterministic fallback agrees the run is finished
    final_state = (await graph.aget_state(runner.config(rid))).values
    assert fallback_route(final_state).next == "END"

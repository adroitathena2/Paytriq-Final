"""End-to-end run of the full LangGraph graph with fakes (no network).

Three segments, as in the live demo:
1. start -> discovery (self-replan, L1) -> shortlist gate -> proposals; the wholesaler is infeasible and
   goes back to Discovery (L7) -> outreach critique asks for revisions (L2) -> v2 -> send gate -> sent.
2. poll replies -> pushback routed to Proposal (L3); a bounce and a decline routed to Discovery (L4), the
   bounced brand was ranked first (A1), both are cafes (L6); two acceptances (deal memory, A2 has too few
   deals) -> replacements -> send gate -> sent.
3. evidence + audit -> ROI finds gaps (the bakery sent no proof) and escalates (L5) -> A3 blocks publishing ->
   organizer publishes with the gaps disclosed -> publish gate -> calibration saved for the fully verified gym
   (L8), skipped for the bakery -> done.

The trace must contain every coordination loop and arbitration rule.
"""

from app.core.llm import FakeLLM
from app.graph.build import build_graph
from app.graph.messages import new_message
from app.graph.runner import RunManager
from app.graph.supervisor import fallback_route
from app.tools.vision import VisionTool
from scripts import verify_trace
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
                "reason": "one deliverable has no proof; publish with the gaps disclosed rather than wait"}
    table = {"discovery": ("discovery", None), "shortlist_review": ("human_approval", "shortlist"),
             "proposals": ("proposal", None), "review": ("outreach", None),
             "send_review": ("human_approval", "send"), "sending": ("outreach", None)}
    nxt, gate = table.get(phase, ("END", None))
    return {"next": nxt, "gate": gate, "reason": f"phase {phase}"}


def places_results(radius, categories):
    near = [make_brand("Good Beans Cafe", km=0.3), make_brand("Good Brew Cafe", km=0.4),
            make_brand("Good Grind Cafe", km=0.8), make_brand("Meh Gym", "gym", km=1.2),
            make_brand("Good Wholesale Mart", "wholesale", km=1.5)]
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
    assert len(v["brands"]) == 8
    pick = ["good-beans-cafe", "good-brew-cafe", "good-grind-cafe", "good-fit-gym", "good-wholesale-mart"]
    await runner.resume(rid, {"gate": "shortlist", "decision": "approve", "edits": {"selected_brand_ids": pick}})
    assert (await db.get_run(rid))["pending_gate"] == "send"
    v = await values()
    assert v["proposals"]["good-wholesale-mart"]["status"] == "infeasible"  # L7: replaced, never emailed
    l7_replacement = v["selected_brand_ids"][len(pick)]
    emails = (await runner.get_state(rid))["interrupt"]["payload"]["emails"]
    assert {e["brand_id"] for e in emails} == set(pick) - {"good-wholesale-mart"} | {l7_replacement}
    await runner.resume(rid, {"gate": "send", "decision": "approve", "edits": {}})
    run_row = await db.get_run(rid)
    assert (run_row["status"], run_row["phase"]) == ("idle", "awaiting_replies")
    assert len(gmail.sent) == 5 and all(s["to"].endswith("@example.com") for s in gmail.sent)

    # Segment 2: replies ---------------------------------------------------------------------------
    v = await values()
    threads = {b: t["thread_id"] for b, t in v["outreach"].items()}
    gmail.reply(threads["good-beans-cafe"], "Address not found", sender="mailer-daemon@googlemail.com", bounce=True)
    gmail.reply(threads["good-brew-cafe"], "Too expensive for our budget right now.")
    gmail.reply(threads["good-grind-cafe"], "Thanks, but not interested.")
    gmail.reply(threads["good-fit-gym"], "We will go with Gold, see you there!")
    gmail.reply(threads[l7_replacement], "We will go with Bronze, count us in.")
    poll = new_message("human", "outreach", "request", {"action": "poll_replies"}, "check replies")
    await runner.trigger(rid, {"phase": "replies", "messages": [poll]}, "poll_replies")
    assert (await db.get_run(rid))["pending_gate"] == "send"
    v = await values()
    assert v["proposals"]["good-brew-cafe"]["version"] >= 2
    assert max(t["price_inr"] for t in v["proposals"]["good-brew-cafe"]["tiers"]) <= 15000
    replacement = v["selected_brand_ids"][-1]
    assert replacement not in pick and v["outreach"]["good-grind-cafe"]["status"] == "declined"
    assert v["outreach"]["good-beans-cafe"]["status"] == "bounced"
    assert next(b for b in v["brands"] if b["id"] == "good-beans-cafe")["unreachable"]  # A1
    await runner.resume(rid, {"gate": "send", "decision": "approve", "edits": {}})
    v = await values()
    assert v["outreach"]["good-brew-cafe"]["thread_id"] == threads["good-brew-cafe"]  # v2 sent in the same thread
    assert v["outreach"][replacement]["status"] == "sent"

    # Segment 3: ROI: the gym proves every deliverable, the bakery uploads nothing ------------------
    gym = v["proposals"]["good-fit-gym"]
    promised = next(t for t in gym["tiers"] if t["name"] == "Gold")["deliverables"]
    await deps.storage.upload("evidence", f"{rid}/photo.jpg", b"\xff\xd8x", "image/jpeg")
    evidence = [{"id": f"ev{i}", "brand_id": "good-fit-gym", "kind": "photo", "storage_path": f"{rid}/photo.jpg",
                 "mime_type": "image/jpeg", "note": f"clear: {d}"} for i, d in enumerate(promised)]
    audit = new_message("human", "roi_audit", "request", {"action": "audit"}, "event is over")
    await runner.trigger(rid, {"phase": "roi", "messages": [audit], "evidence": evidence}, "audit")
    run_row = await db.get_run(rid)  # A3: gaps block publishing; no gate is offered
    assert (run_row["status"], run_row["phase"], run_row["pending_gate"]) == ("idle", "roi_blocked", None)
    choice = new_message("human", "supervisor", "decision", {"action": "publish_with_gaps"}, "publish, disclosed")
    await runner.trigger(rid, {"phase": "roi_review", "publish_with_gaps": True, "messages": [choice]},
                         "publish_with_gaps")
    assert (await db.get_run(rid))["pending_gate"] == "roi_publish"  # the normal approval still follows
    await runner.resume(rid, {"gate": "roi_publish", "decision": "approve", "edits": {}})
    run_row = await db.get_run(rid)
    assert (run_row["status"], run_row["phase"]) == ("completed", "done")
    report = (await values())["roi_report"]
    assert report["published"] and report["published_with_gaps"]
    assert {g["brand_id"] for g in report["disclosed_gaps"]} == {l7_replacement}  # the gym proved everything

    # The trace shows every coordination loop (L1-L8) and arbitration, by the same checks as
    # backend/scripts/verify_trace.py applies to a real exported trace ---------------------------------
    trace = await db.list_trace(rid)
    assert verify_trace.problems({"entries": trace}) == []
    found = verify_trace.matches(trace)
    assert found["L6"][0]["payload"]["content"]["category"] == "cafe"  # a bounce and a decline, both cafes
    assert found["L7"][0]["payload"]["content"]["brand_id"] == "good-wholesale-mart"
    assert [m["payload"]["content"]["category"] for m in found["L8"]] == ["gym"]  # only the fully verified sponsor
    skipped = [t["payload"]["content"] for t in trace if t["event_type"] == "message"
               and t["payload"]["content"].get("reason") == "calibration_skipped"]
    assert skipped == [{"reason": "calibration_skipped", "category": "bakery", "unverified_deliverables": 1}]
    assert found["A1"] and found["A2"] and found["A3"]
    msgs = [t["payload"] for t in trace if t["event_type"] == "message"]
    decisions = [m for m in msgs if m["from_agent"] == "supervisor" and m["type"] == "decision"]
    assert {d["content"]["source"] for d in decisions} <= {"llm", "arbitration"}
    arbitrations = [t["payload"] for t in trace if t["event_type"] == "decision" and t["payload"]["kind"] == "arbitration"]
    a3 = [a["resolution"] for a in arbitrations if a["rule"] == "A3"]
    assert a3[0].startswith("block") and a3[1].startswith("unblock: organizer"), "A3 block, then explicit disclosure"
    assert any(m["type"] == "escalation" and m["content"].get("reason") == "missing_proof" for m in msgs)
    assert {d["content"]["next"] for d in decisions} >= {"discovery", "proposal", "outreach", "roi_audit",
                                                         "human_approval", "END"}
    # sanity: the deterministic fallback agrees the run is finished
    final_state = (await graph.aget_state(runner.config(rid))).values
    assert fallback_route(final_state).next == "END"

"""Supervisor arbitration rules A1-A3 (graph/arbitration.py), run through supervisor.run."""

from langgraph.graph import END

from app.core.llm import FakeLLM, LLMError
from app.graph import supervisor
from app.graph.messages import new_message
from tests.factories import brand, proposal, state, thread


def llm(route=None) -> FakeLLM:
    return FakeLLM(generate={
        "supervisor.route": route or {"next": "END", "reason": "nothing else"},
        "arbitration.justify": {"text": "Justified by the facts."},
    })


def arbitrations(cmd, rule):
    return [e for e in cmd.update["decision_log"] if e["kind"] == "arbitration" and e["rule"] == rule]


def by_type(cmd, type_):
    return [m for m in cmd.update["messages"] if m.type == type_]


# --- A1 bounce vs ranking ------------------------------------------------------------------------

async def test_a1_bounced_top_brand_is_unreachable_and_replaced(make_deps, db):
    brands = [brand(1, 0.95), brand(2, 0.9), brand(3, 0.7), brand(4, 0.5)]
    s = state(phase="awaiting_replies", brands=brands, outreach={"b1": thread("b1", "bounced")})
    cmd = await supervisor.run(s, make_deps(llm({"next": "discovery", "reason": "replace"})))
    [entry] = arbitrations(cmd, "A1")
    assert entry["agents"] == ["discovery", "outreach"] and "#1" in entry["conflict"]
    assert entry["justification"] == "Justified by the facts."
    [request] = [m for m in by_type(cmd, "request") if m.to_agent == "discovery"]
    assert (request.content["reason"], request.content["source"], request.content["arbitration"]) == (
        "replacement", "arbitration", "A1")
    assert next(b for b in cmd.update["brands"] if b.id == "b1").unreachable
    assert any(t["event_type"] == "decision" and t["payload"].get("rule") == "A1" for t in db.trace)


async def test_a1_does_not_duplicate_outreachs_replacement_request(make_deps):
    asked = new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": "b1"}, "bounced")
    s = state(phase="replacement", brands=[brand(1, 0.95)], outreach={"b1": thread("b1", "bounced")}, messages=[asked])
    cmd = await supervisor.run(s, make_deps(llm()))
    [entry] = arbitrations(cmd, "A1")
    assert "request stands" in entry["resolution"]
    assert not by_type(cmd, "request")


async def test_a1_ignores_bounces_outside_the_top_three(make_deps):
    brands = [brand(1, 0.95), brand(2, 0.9), brand(3, 0.8), brand(4, 0.5)]
    s = state(phase="awaiting_replies", brands=brands, outreach={"b4": thread("b4", "bounced")})
    cmd = await supervisor.run(s, make_deps(llm()))
    assert not arbitrations(cmd, "A1")


# --- A2 price vs past deals ----------------------------------------------------------------------

async def seed_deals(db, prices, category="cafe"):
    db.runs["run-test"] = {"id": "run-test", "owner_id": "user-1"}
    for i, price in enumerate(prices):
        await db.add_deal("user-1", {"brand_id": f"old{i}", "category": category, "tier": "Silver", "price_inr": price})


async def test_a2_caps_price_above_past_deals(make_deps, db):
    await seed_deals(db, [10000, 12000, 14000])  # median 12,000 -> cap 15,000; the draft's Gold is 50,000
    s = state(phase="review", brands=[brand(1)], proposals={"b1": proposal("b1")})
    cmd = await supervisor.run(s, make_deps(llm({"next": "proposal", "reason": "revise"})))
    [entry] = arbitrations(cmd, "A2")
    assert entry["resolution"].startswith("Cap every tier at Rs 15,000")
    [revision] = by_type(cmd, "revision_request")
    assert (revision.content["source"], revision.content["max_price_inr"], revision.content["arbitration"]) == (
        "arbitration", 15000, "A2")
    assert cmd.update["proposals"]["b1"].status == "needs_revision"
    assert cmd.update["revision_count"] == {"b1": 1}  # counts toward the agent revision cap


async def test_a2_logs_insufficient_data_and_keeps_price(make_deps, db):
    await seed_deals(db, [10000, 12000])  # only 2 cafe deals
    s = state(phase="review", brands=[brand(1)], proposals={"b1": proposal("b1")})
    cmd = await supervisor.run(s, make_deps(llm({"next": "outreach", "reason": "critique"})))
    [entry] = arbitrations(cmd, "A2")
    assert entry["resolution"] == "insufficient data"
    assert not by_type(cmd, "revision_request") and "proposals" not in cmd.update
    again = await supervisor.run({**s, "decision_log": cmd.update["decision_log"]}, make_deps(llm()))
    assert len(arbitrations(again, "A2")) == 1  # logged once per brand, not every step


async def test_a2_leaves_prices_within_the_margin_alone(make_deps, db):
    await seed_deals(db, [40000, 45000, 50000])
    s = state(phase="review", brands=[brand(1)], proposals={"b1": proposal("b1")})
    cmd = await supervisor.run(s, make_deps(llm()))
    assert not arbitrations(cmd, "A2")


# --- A3 ROI gaps vs publishing -------------------------------------------------------------------

GAP = {"brand_id": "b1", "brand_name": "Brand 1", "deliverable": "Stall", "status": "missing", "explanation": "-"}


def roi_state(gaps, **kw):
    return state(phase="roi_review", roi_report={"generated_at": "t1", "disclosed_gaps": gaps, "published": False}, **kw)


async def test_a3_gaps_block_publishing_with_specific_escalation(make_deps):
    route = {"next": "human_approval", "gate": "roi_publish", "reason": "publish"}  # the LLM is not even asked
    cmd = await supervisor.run(roi_state([GAP]), make_deps(llm(route)))
    assert cmd.goto == END and cmd.update["phase"] == "roi_blocked"
    [escalation] = by_type(cmd, "escalation")
    assert (escalation.to_agent, escalation.content["reason"], escalation.content["arbitration"]) == (
        "human", "missing_proof", "A3")
    assert escalation.content["missing"] == [{"brand_id": "b1", "brand_name": "Brand 1", "deliverable": "Stall",
                                              "status": "missing"}]
    assert arbitrations(cmd, "A3")[0]["resolution"].startswith("block")
    assert cmd.update["messages"][-1].content["source"] == "arbitration"


def test_a3_llm_cannot_open_publish_gate_while_gaps_remain():
    decision = supervisor.RouteDecision(next="human_approval", gate="roi_publish", reason="publish anyway")
    final, rule = supervisor.apply_guardrails(roi_state([GAP]), decision)
    assert rule == "empty_gate" and final.next == "END"


async def test_a3_unblocks_when_organizer_publishes_with_gaps_disclosed(make_deps):
    blocked_log = [{"kind": "arbitration", "rule": "A3", "key": "t1:blocked", "resolution": "block publishing"}]
    cmd = await supervisor.run(roi_state([GAP], publish_with_gaps=True, decision_log=blocked_log), make_deps(llm()))
    assert (cmd.goto, cmd.update["pending_approval"]["gate"]) == ("human_approval", "roi_publish")
    assert arbitrations(cmd, "A3")[-1]["resolution"] == "unblock: organizer chose to publish with the gaps disclosed"


async def test_a3_unblocks_when_reaudit_finds_no_gaps(make_deps):
    blocked_log = [{"kind": "arbitration", "rule": "A3", "key": "t0:blocked", "resolution": "block publishing"}]
    s = state(phase="roi_review", decision_log=blocked_log,
              roi_report={"generated_at": "t2", "disclosed_gaps": [], "published": False})
    cmd = await supervisor.run(s, make_deps(llm()))
    assert (cmd.goto, cmd.update["pending_approval"]["gate"]) == ("human_approval", "roi_publish")
    assert arbitrations(cmd, "A3")[-1]["resolution"] == "unblock: a re-audit verified every deliverable"


async def test_justification_falls_back_to_template_when_llm_fails(make_deps):
    fake = FakeLLM(generate={"supervisor.route": {"next": "END", "reason": "-"},
                             "arbitration.justify": LLMError("429")})
    cmd = await supervisor.run(state(phase="awaiting_replies", brands=[brand(1, 0.9)],
                                     outreach={"b1": thread("b1", "bounced")}), make_deps(fake))
    assert "bounced" in arbitrations(cmd, "A1")[0]["justification"]

"""Proposal agent: new proposals, L2/L3 revisions with constraints, L7 feasibility (Clef) and degraded operation."""

import pytest

from app.agents import proposal
from app.agents.proposal import enforce_max_price, estimate_reach
from app.core.clef import ClefError, FakeClef
from app.core.llm import FakeLLM, LLMError
from app.graph.messages import new_message
from app.tools.pdf import PdfTool, render_proposal_pdf
from tests.factories import EVENT, brand, state
from tests.factories import proposal as make_proposal
from tests.fakes import FakePdf, proposal_plan, proposal_write


def llm(write=proposal_write) -> FakeLLM:
    return FakeLLM(decide={"proposal.plan": proposal_plan}, generate={"proposal.write": write})


async def test_new_proposals_for_selected_brands(make_deps):
    pdf = FakePdf()
    s = state(brands=[brand(1), brand(2), brand(3)], selected_brand_ids=["b1", "b2"], phase="proposals")
    update = await proposal.run(s, make_deps(llm(), pdf=pdf))

    assert set(update["proposals"]) == {"b1", "b2"}
    p = update["proposals"]["b1"]
    assert (p.version, p.status, p.pdf_url) == (1, "draft", "https://files.test/b1-v1.pdf")
    assert [t.name for t in p.tiers] == ["Bronze", "Silver", "Gold"]
    assert p.tiers[2].expected_reach == estimate_reach(EVENT, "Gold", p.tiers[2].deliverables)
    assert update["phase"] == "review"
    assert all(m.to_agent == "outreach" for m in update["messages"])  # drafts go to the critic


async def test_revision_request_from_outreach_creates_v2(make_deps):
    request = new_message("outreach", "proposal", "revision_request",
                          {"brand_id": "b1", "constraint": "Pitch is too long; mention the coffee crowd"}, "critique")
    fake = llm()
    s = state(brands=[brand(1)], selected_brand_ids=["b1"], proposals={"b1": make_proposal("b1")}, messages=[request])
    update = await proposal.run(s, make_deps(fake, pdf=FakePdf()))

    p = update["proposals"]["b1"]
    assert p.version == 2
    write_call = next(c for c in fake.calls if c["purpose"] == "proposal.write")
    assert write_call["context"]["revision_constraint"] == "Pitch is too long; mention the coffee crowd"
    assert write_call["context"]["previous_version"]["version"] == 1
    reply = update["messages"][0]
    assert reply.to_agent == "outreach" and reply.content["in_reply_to"] == [request.id]


async def test_price_pushback_is_honoured_even_if_llm_ignores_it(make_deps):
    request = new_message("outreach", "proposal", "revision_request",
                          {"brand_id": "b1", "constraint": "Brand said max Rs 12,000", "max_price_inr": 12000}, "pushback")

    def stubborn(ctx):  # keeps the old Gold price of 25k
        return proposal_write({**ctx, "max_price_inr": None})

    fake = llm(write=stubborn)
    s = state(brands=[brand(1)], proposals={"b1": make_proposal("b1")}, messages=[request])
    update = await proposal.run(s, make_deps(fake, pdf=FakePdf()))

    assert max(t.price_inr for t in update["proposals"]["b1"].tiers) <= 12000
    # check_pricing flagged the violation, so the agent drafted twice
    assert [c["purpose"] for c in fake.calls].count("proposal.write") == 2


async def test_llm_outage_falls_back_to_template(make_deps):
    fake = FakeLLM(decide={"proposal.plan": LLMError("503")})
    s = state(brands=[brand(1)], selected_brand_ids=["b1"])
    update = await proposal.run(s, make_deps(fake, pdf=FakePdf()))
    p = update["proposals"]["b1"]
    assert p.version == 1 and p.pdf_url
    assert "template" in update["messages"][0].reasoning
    assert update["errors"][0]["tool"] == "llm"


async def test_wholesaler_is_infeasible_and_returned_to_discovery(make_deps):
    """L7: no event deliverable fits a wholesaler -> no PDF, no LLM drafting, request to Discovery."""
    pdf, fake = FakePdf(), llm()
    s = state(brands=[brand(1), brand(2, category="wholesale")], selected_brand_ids=["b1", "b2"], phase="proposals")
    update = await proposal.run(s, make_deps(fake, pdf=pdf))

    assert [b for b, _ in pdf.rendered] == ["b1"]  # only the feasible brand got a PDF
    p = update["proposals"]["b2"]
    assert (p.status, p.pdf_url) == ("infeasible", None)
    [request] = [m for m in update["messages"] if m.to_agent == "discovery"]
    assert (request.type, request.content["reason"], request.content["brand_id"]) == ("request", "infeasible", "b2")
    assert "wholesale" in request.content["why"]
    assert all(c["context"].get("brand_id") != "b2" for c in fake.calls)


async def clef_entries(db) -> list[dict]:
    return [t["payload"] for t in await db.list_trace("run-test")
            if t["event_type"] == "decision" and t["payload"]["kind"] == "clef_judgement"]


@pytest.mark.parametrize(("category", "p_yes", "status", "source", "escalated"), [
    ("cafe", 0.1, "infeasible", "clef", False),            # Clef: no fit, even for a category the rule allows
    ("wholesale", 0.9, "draft", "clef", False),            # Clef: fits, even though the rule would reject it
    ("wholesale", 0.5, "infeasible", "clef", True),        # unsure: rule decides, organizer is asked
    ("cafe", ClefError("timeout"), "draft", "rule_fallback", False),
    ("wholesale", ClefError("timeout"), "infeasible", "rule_fallback", False),
])
async def test_l7_brand_fit_is_judged_by_clef(make_deps, db, category, p_yes, status, source, escalated):
    """L7 part 1: Clef judges brand fit; thresholds, rule fallback and escalation guard the answer."""
    clef, pdf = FakeClef({"proposal.feasibility": p_yes}), FakePdf()
    s = state(brands=[brand(1, category=category)], selected_brand_ids=["b1"], phase="proposals")
    update = await proposal.run(s, make_deps(llm(), pdf=pdf, clef=clef))

    assert update["proposals"]["b1"].status == status
    assert clef.calls[0]["state"]["brand"]["category"] == category
    assert clef.calls[0]["state"]["event"]["footfall"] == EVENT["expected_footfall"]
    [entry] = await clef_entries(db)
    assert (entry["purpose"], entry["source"], entry["escalated"]) == ("proposal.feasibility", source, escalated)
    assert entry["rule_answer"] == (category != "wholesale")
    escalations = [m for m in update["messages"] if m.type == "escalation"]
    assert len(escalations) == int(escalated)
    if escalated:
        assert (escalations[0].to_agent, escalations[0].content["reason"]) == ("human", "clef_unsure")
        assert escalations[0].content["p_yes"] == 0.5
    if status == "infeasible":
        [request] = [m for m in update["messages"] if m.to_agent == "discovery"]
        assert request.content["reason"] == "infeasible" and request.content["decided_by"] == source
        assert not pdf.rendered
    else:
        assert pdf.rendered == [("b1", 1)]


async def test_revisions_are_not_judged_again(make_deps):
    clef = FakeClef({"proposal.feasibility": AssertionError("must not be called")})
    request = new_message("outreach", "proposal", "revision_request", {"brand_id": "b1", "constraint": "shorter"}, "c")
    s = state(brands=[brand(1)], proposals={"b1": make_proposal("b1")}, messages=[request])
    update = await proposal.run(s, make_deps(llm(), pdf=FakePdf(), clef=clef))
    assert update["proposals"]["b1"].version == 2 and not clef.calls


async def test_bronze_reach_too_low_for_its_price_is_infeasible(make_deps):
    tiny = {**EVENT, "expected_footfall": 40}  # Bronze reaches ~10 people
    s = state(brands=[brand(1)], selected_brand_ids=["b1"], phase="proposals", event=tiny)
    pdf = FakePdf()
    update = await proposal.run(s, make_deps(llm(), pdf=pdf))
    assert update["proposals"]["b1"].status == "infeasible" and not pdf.rendered
    assert "per person" in update["messages"][0].content["why"]
    assert update["phase"] == "replacement"


async def test_nothing_to_do_keeps_phase(make_deps):
    update = await proposal.run(state(phase="awaiting_replies"), make_deps(llm(), pdf=FakePdf()))
    assert "phase" not in update


def test_reach_adds_social_audience():
    assert estimate_reach(EVENT, "Bronze", ["Logo on posters"]) == 450
    assert estimate_reach(EVENT, "Bronze", ["1 Instagram post"]) == 1650


def test_max_price_scales_the_ladder():
    tiers = make_proposal("b1").tiers
    capped = enforce_max_price(tiers, 20000)
    assert [t.price_inr for t in capped] == [4000, 10000, 20000]


async def test_pdf_renders_and_is_stored(make_deps, tmp_path):
    p = make_proposal("b1")
    data = render_proposal_pdf(EVENT, brand(1), p)
    assert data.startswith(b"%PDF") and len(data) > 1500
    url = await PdfTool(make_deps(llm()).storage).render_and_store("run-1", EVENT, brand(1), p)
    assert url == "http://testserver/files/proposals/run-1/b1-v1.pdf"

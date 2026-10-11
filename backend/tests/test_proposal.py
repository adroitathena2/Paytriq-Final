"""Proposal agent: new proposals, L2/L3 revisions with constraints, and degraded operation."""

from app.agents import proposal
from app.agents.proposal import enforce_max_price, estimate_reach
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

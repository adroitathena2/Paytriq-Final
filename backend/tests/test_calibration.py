"""L8 ROI calibration: ROI Audit measures delivered reach, an approved report saves it, Proposal uses it."""

from app.agents import proposal, roi_audit
from app.core.llm import FakeLLM
from app.graph import approval
from app.tools.vision import DeliverableCheck
from tests.factories import brand, state
from tests.fakes import FakePdf, proposal_plan, proposal_write

SPONSOR = {"brand_id": "b1", "name": "Brand 1", "category": "cafe", "tier": "Gold", "price_inr": 20000,
           "expected_reach": 1270, "deliverables": ["Logo on main stage banner", "Stall"]}


def check(deliverable, status):
    return DeliverableCheck(deliverable=deliverable, status=status, confidence=0.9, explanation="-")


def test_factor_uses_verified_deliverables_and_actual_footfall():
    s = state(actual_footfall=1000)  # 1,500 were expected
    cal = roi_audit.calibration(s, SPONSOR, [check("Logo on main stage banner", "verified"), check("Stall", "missing")])
    # Gold on-ground share 0.85 x 1,000 = 850, half the deliverables verified -> 420 of an estimated 1,270
    assert (cal["verified_reach"], cal["factor"], cal["footfall_source"]) == (420, 0.33, "actual")
    assert roi_audit.calibration(state(), SPONSOR, [])["factor"] == 0.0


def sponsor(factor, statuses=("verified", "verified"), brand_id="b1"):
    """A report row as build_report writes it: one check per deliverable, plus the calibration proposal."""
    checks = [check(d, s).model_dump() for d, s in zip(SPONSOR["deliverables"], statuses, strict=True)]
    return {**SPONSOR, "brand_id": brand_id, "checks": checks,
            "calibration": {"category": "cafe", "factor": factor}}


def report(factor, *sponsors):
    return {"generated_at": "t", "disclosed_gaps": [], "published": False, "actual_footfall": 1000,
            "sponsors": list(sponsors) or [sponsor(factor)]}


async def test_only_an_approved_report_calibrates(make_deps, db):
    db.runs["run-test"] = {"id": "run-test", "owner_id": "user-1"}
    deps = make_deps(FakeLLM())
    s = state(phase="roi_review", roi_report=report(0.5), pending_approval={"gate": "roi_publish"})

    await approval._roi(s, deps, "reject", {})
    assert await db.get_calibration("user-1", "cafe") is None  # unapproved reports never calibrate

    update = await approval._roi(s, deps, "approve", {})
    row = await db.get_calibration("user-1", "cafe")
    assert (row["factor"], row["n_events"]) == (0.5, 1)
    [msg] = update["messages"]
    assert (msg.from_agent, msg.to_agent, msg.type, msg.content["reason"]) == ("roi_audit", "proposal", "result",
                                                                                "calibration")
    await approval._roi({**s, "roi_report": report(0.7)}, deps, "approve", {})
    row = await db.get_calibration("user-1", "cafe")
    assert (round(row["factor"], 2), row["n_events"]) == (0.6, 2)  # running average


async def test_unverified_evidence_never_calibrates(make_deps, db):
    """Published with gaps: a sponsor with only ambiguous/missing checks says nothing about reach."""
    db.runs["run-test"] = {"id": "run-test", "owner_id": "user-1"}
    deps = make_deps(FakeLLM())
    gaps = report(0.0, sponsor(0.0, ("ambiguous", "missing")))
    gaps["disclosed_gaps"] = [{"brand_id": "b1", "deliverable": d, "status": "ambiguous"} for d in SPONSOR["deliverables"]]
    s = state(phase="roi_review", roi_report=gaps, publish_with_gaps=True, pending_approval={"gate": "roi_publish"})

    update = await approval._roi(s, deps, "approve", {})
    assert update["roi_report"]["published_with_gaps"]  # publishing itself is not blocked
    assert await db.get_calibration("user-1", "cafe") is None
    [msg] = update["messages"]
    assert (msg.from_agent, msg.to_agent, msg.type) == ("roi_audit", "proposal", "result")
    assert msg.content == {"reason": "calibration_skipped", "category": "cafe", "unverified_deliverables": 2}
    assert msg.reasoning == ("Not calibrating cafe: 2 deliverable(s) unverified; unverified evidence is not used "
                             "to change future reach estimates.")


async def test_only_fully_verified_sponsors_count_in_a_category(make_deps, db):
    db.runs["run-test"] = {"id": "run-test", "owner_id": "user-1"}
    deps = make_deps(FakeLLM())
    mixed = report(None, sponsor(0.8), sponsor(0.1, ("verified", "ambiguous"), brand_id="b2"))
    s = state(phase="roi_review", roi_report=mixed, pending_approval={"gate": "roi_publish"})

    update = await approval._roi(s, deps, "approve", {})
    row = await db.get_calibration("user-1", "cafe")
    assert (row["factor"], row["n_events"]) == (0.8, 1)  # b2's partly verified 0.1 is left out
    [msg] = update["messages"]
    assert (msg.content["reason"], msg.content["event_factor"]) == ("calibration", 0.8)


async def test_next_proposal_uses_calibration_and_says_so(make_deps, db):
    db.runs["run-test"] = {"id": "run-test", "owner_id": "user-1"}
    await db.save_calibration("user-1", "cafe", 0.5)
    llm = FakeLLM(decide={"proposal.plan": proposal_plan}, generate={"proposal.write": proposal_write})
    s = state(brands=[brand(1), brand(2, category="gym")], selected_brand_ids=["b1", "b2"], phase="proposals")
    update = await proposal.run(s, make_deps(llm, pdf=FakePdf()))

    cafe, gym = update["proposals"]["b1"], update["proposals"]["b2"]
    assert cafe.reach_note == "estimate calibrated from 1 past event"
    assert cafe.tiers[0].expected_reach == round(proposal.estimate_reach(s["event"], "Bronze",
                                                                          cafe.tiers[0].deliverables) * 0.5, -1)
    assert gym.reach_note is None  # no history for gyms
    write = next(c for c in llm.calls if c["purpose"] == "proposal.write" and c["context"]["brand"]["id"] == "b1")
    assert write["context"]["reach_calibration"] == {"factor": 0.5, "n_events": 1}


def test_pdf_shows_the_calibration_note():
    from app.tools.pdf import render_proposal_pdf
    from tests.factories import EVENT
    from tests.factories import proposal as make_proposal

    p = make_proposal("b1")
    p.reach_note = "estimate calibrated from 2 past events"
    assert render_proposal_pdf(EVENT, brand(1), p).startswith(b"%PDF")

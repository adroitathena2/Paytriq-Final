"""ROI Audit agent: verification with vision, honest gaps and the L5 escalation."""

from app.agents import roi_audit
from app.core.llm import FakeLLM, LLMError
from app.tools.vision import VisionTool, html_to_text
from tests.factories import brand, proposal, state, thread
from tests.fakes import roi_plan, vision_by_note

GOLD = ["Logo on main stage banner", "Stall", "Instagram post"]


def accepted(brand_id: str, tier: str = "Gold"):
    t = thread(brand_id, status="accepted")
    t.history.append({"type": "accepted", "tier": tier})
    return t


async def upload(storage, name: str) -> str:
    await storage.upload("evidence", f"run/{name}.jpg", b"\xff\xd8fake-jpeg", "image/jpeg")
    return f"run/{name}.jpg"


async def audit_state(storage, evidence_notes: dict[str, list[str]]):
    evidence = []
    for brand_id, notes in evidence_notes.items():
        for i, note in enumerate(notes):
            path = await upload(storage, f"{brand_id}-{i}")
            evidence.append({"id": f"ev-{brand_id}-{i}", "brand_id": brand_id, "kind": "photo",
                             "storage_path": path, "mime_type": "image/jpeg", "note": note})
    p1 = proposal("b1")
    p1.tiers[2].deliverables = GOLD
    return state(brands=[brand(1), brand(2)], proposals={"b1": p1, "b2": proposal("b2")},
                 outreach={"b1": accepted("b1"), "b2": accepted("b2", "Bronze")}, evidence=evidence, phase="roi")


def deps_for(make_deps, llm):
    deps = make_deps(llm)
    deps.vision = VisionTool(llm, deps.storage)
    return deps


def llm() -> FakeLLM:
    return FakeLLM(decide={"roi.plan": roi_plan}, generate={"roi.vision": vision_by_note})


async def test_fully_evidenced_event_has_no_gaps(make_deps):
    fake = llm()
    deps = deps_for(make_deps, fake)
    s = await audit_state(deps.storage, {
        "b1": ["clear: Logo on main stage banner", "clear: Stall", "clear: Instagram post"],
        "b2": ["clear: Logo on poster"]})
    update = await roi_audit.run(s, deps)
    report = update["roi_report"]
    assert report["overall_delivery_rate"] == 1.0 and report["disclosed_gaps"] == []
    assert [sp["tier"] for sp in report["sponsors"]] == ["Gold", "Bronze"]
    assert not [m for m in update["messages"] if m.type == "escalation"]
    assert update["phase"] == "roi_review"
    vision_calls = [c for c in fake.calls if c["purpose"] == "roi.vision"]
    assert vision_calls[0]["images"] == 3  # all three photos went to the vision model


async def test_missing_and_blurry_evidence_is_disclosed_and_escalated(make_deps):
    deps = deps_for(make_deps, llm())
    s = await audit_state(deps.storage, {"b1": ["clear: Logo on main stage banner", "blurry: Stall"]})
    update = await roi_audit.run(s, deps)
    report = update["roi_report"]

    gaps = {(g["brand_id"], g["deliverable"], g["status"]) for g in report["disclosed_gaps"]}
    assert gaps == {("b1", "Stall", "ambiguous"), ("b1", "Instagram post", "missing"),
                    ("b2", "Logo on poster", "missing")}
    assert report["overall_delivery_rate"] == 0.25  # 1 of 4, never rounded up
    [escalation] = [m for m in update["messages"] if m.type == "escalation"]
    assert escalation.to_agent == "human" and escalation.content["gaps"] == 3
    assert "Please upload a clear photo of: Stall" in escalation.content["requests"]


async def test_llm_outage_still_produces_an_honest_report(make_deps):
    fake = FakeLLM(decide={"roi.plan": LLMError("503")}, generate={"roi.vision": LLMError("503")})
    deps = deps_for(make_deps, fake)
    s = await audit_state(deps.storage, {"b1": ["clear: Stall"]})
    update = await roi_audit.run(s, deps)
    report = update["roi_report"]
    assert report["overall_delivery_rate"] == 0.0
    assert {g["status"] for g in report["disclosed_gaps"]} == {"ambiguous", "missing"}
    assert any(e["tool"] == "vision" for e in update["errors"])
    escalation = next(m for m in update["messages"] if m.type == "escalation")
    assert escalation.content["requests"]  # default, specific requests are generated


async def test_nothing_to_audit_without_accepted_sponsors(make_deps):
    update = await roi_audit.run(state(phase="roi"), deps_for(make_deps, llm()))
    assert update["phase"] == "awaiting_replies"
    assert update["messages"][0].type == "escalation"


async def test_vision_marks_unreported_deliverables_ambiguous(make_deps):
    fake = FakeLLM(generate={"roi.vision": {"checks": [
        {"deliverable": "Stall", "status": "verified", "confidence": 0.9, "explanation": "stall visible"}]}})
    deps = make_deps(fake)
    path = await upload(deps.storage, "x")
    checks = await VisionTool(fake, deps.storage).verify("Cafe", ["Stall", "Banner"], [
        {"id": "e1", "kind": "photo", "storage_path": path, "mime_type": "image/jpeg"}])
    assert [(c.deliverable, c.status) for c in checks] == [("Stall", "verified"), ("Banner", "ambiguous")]


async def test_vision_reads_links_and_survives_fetch_errors(make_deps, monkeypatch):
    async def broken(url, timeout=10.0):
        return "(could not fetch this link: ConnectError)"

    monkeypatch.setattr("app.tools.vision.fetch_link_text", broken)
    fake = FakeLLM(generate={"roi.vision": {"checks": []}})
    deps = make_deps(fake)
    await VisionTool(fake, deps.storage).verify("Cafe", ["Instagram post"], [
        {"id": "e1", "kind": "link", "url": "https://instagram.com/p/x"}])
    assert "could not fetch" in fake.calls[0]["context"]["links"][0]["page_text"]


def test_html_to_text_strips_scripts():
    assert html_to_text("<html><script>x()</script><p>Logo  <b>live</b></p></html>") == "Logo live"

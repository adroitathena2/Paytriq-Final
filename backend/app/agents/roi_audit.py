"""ROI Audit agent: after the event, verifies every promised deliverable and writes the ROI report.

Reasoning loop (agents/loop.py) tools: ``list_deliverables`` (what each accepted sponsor was promised and
how much evidence exists), ``verify_brand`` (Gemini Vision over that brand's photos and links),
``request_evidence`` (specific asks to the organizer) and ``compile_report`` (with the LLM's summary).

Honesty rules enforced in code after the loop:
- every accepted brand is verified, even if the LLM skipped one;
- anything not clearly verified (missing, ambiguous, not_verified) is listed as a disclosed gap and
  lowers the delivery rate, never rounded up;
- L5: when there are gaps, the agent sends an ``escalation`` to the organizer asking for the specific
  extra evidence instead of guessing. The supervisor's arbitration rule A3 then blocks publishing until
  new evidence passes a re-audit or the organizer explicitly chooses to publish with the gaps disclosed.

L8 calibration: for each sponsor the report also carries a reach factor = reach of the *verified*
deliverables at the actual footfall (if the organizer gave one) / the reach the proposal estimated. It is
only a proposal inside the report; it is saved for the Proposal agent (per organizer and category) when the
organizer approves publishing, so an unapproved report never calibrates anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.loop import AgentTool, run_tool_loop
from app.agents.proposal import estimate_reach
from app.core.llm import ToolSpec
from app.graph.messages import emit, open_requests
from app.graph.state import EventState
from app.tools.base import ToolInputError
from app.tools.vision import DeliverableCheck

AGENT = "roi_audit"
GAP_STATUSES = {"missing", "ambiguous", "not_verified"}

SYSTEM = """You are the ROI Audit agent of Paytriq. After the event you check, sponsor by sponsor, whether
every promised deliverable was actually delivered, using the organizer's uploaded photos and links.
Be rigorous and honest: a sponsor will rely on this report. Verify every accepted sponsor, then, if anything
is missing or unclear, ask the organizer for SPECIFIC extra evidence (which photo, of what, from where)
instead of guessing. Finally compile the report with a short, factual summary and call finish."""


def accepted_sponsors(state: EventState) -> list[dict]:
    """Accepted brands with the tier they accepted and that tier's deliverables."""
    brands = {b.id: b for b in state.get("brands", [])}
    out = []
    for brand_id, thread in state.get("outreach", {}).items():
        if thread.status != "accepted" or brand_id not in state.get("proposals", {}):
            continue
        tier_name = next((e["tier"] for e in reversed(thread.history) if e.get("type") == "accepted"), "Silver")
        proposal = state["proposals"][brand_id]
        tier = next((t for t in proposal.tiers if t.name == tier_name), proposal.tiers[0])
        out.append({"brand_id": brand_id, "name": brands[brand_id].name if brand_id in brands else brand_id,
                    "category": brands[brand_id].category if brand_id in brands else None, "tier": tier.name, "price_inr": tier.price_inr, "expected_reach": tier.expected_reach,
                    "deliverables": tier.deliverables})
    return out


class Workspace:
    def __init__(self, state: EventState) -> None:
        self.state = state
        self.sponsors = {s["brand_id"]: s for s in accepted_sponsors(state)}
        self.evidence = state.get("evidence", [])
        self.checks: dict[str, list[DeliverableCheck]] = {}
        self.requests: list[str] = []
        self.summary = ""

    def evidence_for(self, brand_id: str) -> list[dict]:
        return [e for e in self.evidence if e.get("brand_id") == brand_id]


def build_tools(deps, ws: Workspace) -> list[AgentTool]:
    async def list_deliverables() -> dict:
        return {"sponsors": [{**s, "evidence_items": len(ws.evidence_for(b))} for b, s in ws.sponsors.items()]}

    async def verify_brand(brand_id: str) -> dict:
        sponsor = ws.sponsors.get(brand_id)
        if sponsor is None:
            raise ToolInputError(f"{brand_id} is not an accepted sponsor")
        checks = await deps.vision.verify(sponsor["name"], sponsor["deliverables"], ws.evidence_for(brand_id))
        ws.checks[brand_id] = checks
        return {"brand_id": brand_id, "results": [f"{c.deliverable}: {c.status} ({c.explanation})" for c in checks]}

    async def request_evidence(requests: list[str]) -> dict:
        ws.requests += [r for r in requests if r.strip()]
        return {"queued_requests": len(ws.requests)}

    async def compile_report(summary: str) -> dict:
        ws.summary = summary
        return {"ok": True}

    return [
        AgentTool(ToolSpec(name="list_deliverables", description="What each accepted sponsor was promised."),
                  list_deliverables),
        AgentTool(ToolSpec(name="verify_brand", description="Check one sponsor's deliverables against its evidence.",
                           parameters={"type": "object", "properties": {"brand_id": {"type": "string"}},
                                       "required": ["brand_id"]}), verify_brand, timeout=150),
        AgentTool(ToolSpec(name="request_evidence", description="Ask the organizer for specific missing evidence.",
                           parameters={"type": "object", "properties": {
                               "requests": {"type": "array", "items": {"type": "string"}}}, "required": ["requests"]}),
                  request_evidence),
        AgentTool(ToolSpec(name="compile_report", description="Record the report summary (2-4 factual sentences).",
                           parameters={"type": "object", "properties": {"summary": {"type": "string"}},
                                       "required": ["summary"]}), compile_report),
    ]


def calibration(state: EventState, sponsor: dict, checks: list[DeliverableCheck]) -> dict:
    """L8: reach the verified deliverables achieved at the actual footfall, relative to the estimate."""
    actual = state.get("actual_footfall")
    footfall = actual or state["event"]["expected_footfall"]
    verified = [c.deliverable for c in checks if c.status == "verified"]
    reach = 0
    if verified and checks:
        full = estimate_reach({"expected_footfall": footfall}, sponsor["tier"], verified)
        reach = int(round(full * len(verified) / len(checks), -1))
    factor = round(reach / sponsor["expected_reach"], 2) if sponsor["expected_reach"] else None
    return {"category": sponsor.get("category"), "factor": factor, "verified_reach": reach,
            "estimated_reach": sponsor["expected_reach"], "footfall_used": footfall,
            "footfall_source": "actual" if actual else "expected"}


def build_report(state: EventState, ws: Workspace) -> dict:
    rows, gaps = [], []
    for brand_id, sponsor in ws.sponsors.items():
        checks = ws.checks.get(brand_id, [])
        delivered = sum(c.status == "verified" for c in checks)
        rows.append({**sponsor, "checks": [c.model_dump() for c in checks], "delivered": delivered,
                     "total": len(checks), "delivery_rate": round(delivered / len(checks), 2) if checks else 0.0,
                     "calibration": calibration(state, sponsor, checks)})
        gaps += [{"brand_id": brand_id, "brand_name": sponsor["name"], "deliverable": c.deliverable,
                  "status": c.status, "explanation": c.explanation} for c in checks if c.status in GAP_STATUSES]
    total = sum(r["total"] for r in rows)
    event = state["event"]
    return {
        "event": {k: event.get(k) for k in ("name", "college", "date", "expected_footfall")},
        "actual_footfall": state.get("actual_footfall"),
        "generated_at": datetime.now(UTC).isoformat(),
        "sponsors": rows,
        "overall_delivery_rate": round(sum(r["delivered"] for r in rows) / total, 2) if total else 0.0,
        "disclosed_gaps": gaps,
        "evidence_requests": ws.requests,
        "summary": ws.summary or f"{sum(r['delivered'] for r in rows)} of {total} promised deliverables verified.",
        "published": False,
    }


async def commit_calibration(deps, report: dict) -> list:
    """L8, called when the organizer approves publishing: save one factor per category (running average)
    and tell the Proposal agent. Failures are traced but never block publishing."""
    by_category: dict[str, list[float]] = {}
    for sponsor in report.get("sponsors", []):
        cal = sponsor.get("calibration") or {}
        if cal.get("category") and cal.get("factor") is not None:
            by_category.setdefault(cal["category"], []).append(cal["factor"])
    messages = []
    try:
        owner = await deps.owner_id()
        if owner is None:
            return []
        for category, factors in by_category.items():
            factor = round(sum(factors) / len(factors), 2)
            row = await deps.db.save_calibration(owner, category, factor)
            messages.append(await emit(deps, AGENT, "proposal", "result", {
                "reason": "calibration", "category": category, "event_factor": factor,
                "factor": round(row["factor"], 2), "n_events": row["n_events"],
                "actual_footfall": report.get("actual_footfall")},
                f"Approved report: {category} sponsors got {factor:.0%} of the estimated reach from verified "
                f"deliverables; future {category} estimates use x{row['factor']:.2f} ({row['n_events']} event(s))."))
    except Exception as exc:
        await deps.log(AGENT, "tool_error", {"tool": "db.save_calibration", "error": str(exc)})
    return messages


def default_request(gap: dict) -> str:
    if gap["status"] == "missing":
        return f"Upload a photo or link showing '{gap['deliverable']}' for {gap['brand_name']}."
    return f"Upload a clearer photo of '{gap['deliverable']}' for {gap['brand_name']} ({gap['explanation']})."


async def run(state: EventState, deps) -> dict:
    ws = Workspace(state)
    requests = open_requests(state, AGENT)
    errors = list(state.get("errors", []))
    if not ws.sponsors:
        msg = await emit(deps, AGENT, "human", "escalation", {"in_reply_to": [r.id for r in requests]},
                         "No sponsor has accepted yet, so there is nothing to audit.")
        return {"messages": [msg], "phase": "awaiting_replies"}

    previous = state.get("roi_report") or {}
    context = {"event": state["event"], "sponsors": list(ws.sponsors), "evidence_items": len(ws.evidence),
               "previous_gaps": previous.get("disclosed_gaps", [])}
    result = await run_tool_loop(deps, AGENT, "roi.plan", SYSTEM, context, build_tools(deps, ws))
    errors += result.errors

    for brand_id, sponsor in ws.sponsors.items():  # never skip a sponsor
        if brand_id in ws.checks:
            continue
        try:
            ws.checks[brand_id] = await deps.vision.verify(sponsor["name"], sponsor["deliverables"], ws.evidence_for(brand_id))
        except Exception as exc:
            errors.append({"agent": AGENT, "tool": "vision", "error": str(exc)})
            ws.checks[brand_id] = [DeliverableCheck(deliverable=d, status="ambiguous", confidence=0.0,
                                                    explanation="Automatic check failed; needs a manual look.")
                                   for d in sponsor["deliverables"]]

    report = build_report(state, ws)
    messages = []
    if report["disclosed_gaps"]:
        if not ws.requests:
            report["evidence_requests"] = ws.requests = [default_request(g) for g in report["disclosed_gaps"]]
        messages.append(await emit(deps, AGENT, "human", "escalation", {
            "gaps": len(report["disclosed_gaps"]), "requests": ws.requests,
            "overall_delivery_rate": report["overall_delivery_rate"]},
            f"{len(report['disclosed_gaps'])} deliverable(s) could not be verified; asking for specific evidence "
            "instead of guessing."))
    messages.append(await emit(deps, AGENT, "supervisor", "result", {
        "overall_delivery_rate": report["overall_delivery_rate"], "gaps": len(report["disclosed_gaps"]),
        "in_reply_to": [r.id for r in requests]}, report["summary"]))
    return {"roi_report": report, "messages": messages, "errors": errors, "phase": "roi_review"}

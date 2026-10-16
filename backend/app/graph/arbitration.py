"""Supervisor arbitration: conflicts between agents, resolved by fixed rules in code.

The supervisor calls ``arbitrate`` before every routing decision. Each rule looks for one kind of
conflict on the blackboard; the *resolution* is always decided by the rule (so it is predictable and
testable), and the LLM only writes a one-to-two-sentence justification for the organizer (with a
template sentence if the LLM is unavailable). Every arbitration is a ``decision_log`` entry
``{kind: "arbitration", rule, key, conflict, agents, resolution, justification}`` and a trace event.

- A1 bounce vs ranking: Discovery ranked a brand in its top 3, but Outreach's email to it bounced.
  Delivery evidence beats ranking: the brand is marked unreachable and Discovery is asked for a
  replacement (unless Outreach already asked).
- A2 price vs past deals: a new proposal is priced above 1.25 x the median accepted price for this
  organizer and category. With at least 3 past deals the price is capped there and Proposal gets a
  ``revision_request`` (source ``arbitration``); with fewer, the rule logs "insufficient data" and
  leaves the price alone.
- A3 ROI gaps vs publishing: the audit found missing or ambiguous proof. Publishing is blocked
  (phase ``roi_blocked``) with a specific "missing proof" escalation. It reopens only when a re-audit
  finds no gaps, or when the organizer explicitly chooses "Publish with gaps disclosed"; in both cases
  the normal ``roi_publish`` approval still follows. Nothing is ever published silently.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.core.llm import LLMError
from app.graph.messages import as_message, emit, log_decision
from app.graph.state import EventState

TOP_RANK = 3
PRICE_MARGIN = 1.25
MIN_DEALS = 3

JUSTIFY_SYSTEM = """You are the supervisor of Paytriq, a multi-agent sponsorship system. Two agents disagreed
and a fixed rule has already resolved the conflict. In one or two plain sentences, justify the resolution to
the event organizer using the concrete facts given. Do not change or question the resolution."""


class Justification(BaseModel):
    text: str


@dataclass
class Outcome:
    """What the arbitration rules changed in this supervisor step."""

    messages: list = field(default_factory=list)
    update: dict = field(default_factory=dict)  # state fields to overwrite
    route: tuple[str, str | None, str] | None = None  # forced (next, gate, reason)


def already(log: list[dict], rule: str, key: str) -> bool:
    return any(e.get("kind") == "arbitration" and e.get("rule") == rule and e.get("key") == key for e in log)


async def justify(deps, rule: str, conflict: str, resolution: str, template: str) -> str:
    try:
        out = await deps.llm.generate(purpose="arbitration.justify", system=JUSTIFY_SYSTEM, schema=Justification,
                                      context={"rule": rule, "conflict": conflict, "resolution": resolution})
        return out.text.strip()[:400] or template
    except LLMError:
        return template


async def settle(deps, log: list[dict], rule: str, key: str, conflict: str, agents: list[str], resolution: str,
                 template: str, use_llm: bool = True) -> str:
    justification = await justify(deps, rule, conflict, resolution, template) if use_llm else template
    await log_decision(deps, log, kind="arbitration", rule=rule, key=key, conflict=conflict, agents=agents,
                       resolution=resolution, justification=justification, next=None, reason=justification,
                       source="code")
    return justification


async def bounce_vs_ranking(state: EventState, deps, log: list[dict], out: Outcome) -> None:
    """A1: a top-ranked brand bounced -> mark unreachable and make sure Discovery replaces it."""
    ranked = sorted(state.get("brands", []), key=lambda b: b.fit_score, reverse=True)
    rank = {b.id: i + 1 for i, b in enumerate(ranked[:TOP_RANK])}
    messages = [as_message(m) for m in state.get("messages", [])]
    brands = None
    for brand_id, thread in state.get("outreach", {}).items():
        if thread.status != "bounced" or brand_id not in rank or already(log, "A1", brand_id):
            continue
        brands = brands or [b.model_copy() for b in out.update.get("brands", state.get("brands", []))]
        brand = next(b for b in brands if b.id == brand_id)
        brand.unreachable = True
        conflict = (f"Discovery ranked {brand.name} #{rank[brand_id]} (fit {brand.fit_score}), "
                    "but Outreach's email to it bounced.")
        asked = any(m.type == "request" and m.content.get("reason") == "replacement"
                    and m.content.get("brand_id") == brand_id for m in messages)
        if asked:
            resolution = f"Delivery evidence beats ranking: {brand.name} marked unreachable; Outreach's replacement request stands."
        else:
            resolution = f"Delivery evidence beats ranking: {brand.name} marked unreachable; Discovery asked for a replacement."
        template = f"The email to {brand.name} bounced, so its high ranking cannot turn into a deal; we replace it."
        why = await settle(deps, log, "A1", brand_id, conflict, ["discovery", "outreach"], resolution, template)
        if not asked:
            out.messages.append(await emit(deps, "supervisor", "discovery", "request", {
                "reason": "replacement", "brand_id": brand_id, "why": "email bounced", "source": "arbitration",
                "arbitration": "A1"}, why))
    if brands is not None:
        out.update["brands"] = brands


async def price_vs_deals(state: EventState, deps, log: list[dict], out: Outcome) -> None:
    """A2: cap a new proposal's price at 1.25 x the median of this owner's accepted deals in the category."""
    brands = {b.id: b for b in state.get("brands", [])}
    drafts = {k: p for k, p in state.get("proposals", {}).items() if p.status == "draft" and k in brands}
    if not drafts:
        return
    owner = await deps.owner_id()
    proposals = None
    for brand_id, proposal in drafts.items():
        brand = brands[brand_id]
        key = f"{brand_id}:v{proposal.version}"
        if already(log, "A2", key) or already(log, "A2", f"{brand_id}:insufficient"):
            continue
        price = max(t.price_inr for t in proposal.tiers)
        try:
            deals = await deps.db.list_deals(owner, brand.category) if owner else []
        except Exception:  # memory unavailable: same as having no history
            deals = []
        if len(deals) < MIN_DEALS:
            await settle(
                deps, log, "A2", f"{brand_id}:insufficient",
                f"Proposal v{proposal.version} prices {brand.name} up to Rs {price:,}; only {len(deals)} past accepted "
                f"{brand.category} deal(s) to compare with.", ["proposal", "supervisor"], "insufficient data",
                f"With fewer than {MIN_DEALS} past {brand.category} deals there is no reliable benchmark, "
                "so the proposed price stands.", use_llm=False)
            continue
        median = statistics.median(d["price_inr"] for d in deals)
        cap = int(round(PRICE_MARGIN * median, -2))
        if price <= cap:
            continue
        conflict = (f"Proposal v{proposal.version} prices {brand.name} up to Rs {price:,}, but {len(deals)} accepted "
                    f"{brand.category} deals have a median of Rs {median:,.0f}.")
        resolution = f"Cap every tier at Rs {cap:,} ({PRICE_MARGIN} x median) and ask Proposal for a revision."
        template = (f"Past {brand.category} sponsors accepted around Rs {median:,.0f}; pricing far above that "
                    "risks a refusal, so the top tier is capped.")
        why = await settle(deps, log, "A2", key, conflict, ["proposal", "supervisor"], resolution, template)
        proposals = proposals or {k: v.model_copy(deep=True) for k, v in state.get("proposals", {}).items()}
        proposals[brand_id].status = "needs_revision"
        out.messages.append(await emit(deps, "supervisor", "proposal", "revision_request", {
            "brand_id": brand_id, "source": "arbitration", "arbitration": "A2", "max_price_inr": cap,
            "from_version": proposal.version, "median_accepted_inr": median, "deals_compared": len(deals),
            "constraint": f"Keep every tier at or below Rs {cap:,} (past {brand.category} deals: median Rs {median:,.0f})."},
            why))
    if proposals is not None:
        out.update["proposals"] = proposals


async def roi_gaps(state: EventState, deps, log: list[dict], out: Outcome) -> None:
    """A3: missing or ambiguous proof blocks publishing until new evidence or an explicit organizer choice."""
    report = state.get("roi_report") or {}
    if state.get("phase") != "roi_review" or not report or report.get("published"):
        return
    gaps, key = report.get("disclosed_gaps", []), report.get("generated_at", "")
    gate = ("human_approval", "roi_publish")
    if not gaps:
        if any(e.get("rule") == "A3" and e.get("resolution", "").startswith("block") for e in log) \
                and not already(log, "A3", f"{key}:cleared"):
            await settle(deps, log, "A3", f"{key}:cleared", "Publishing was blocked by missing proof.",
                         ["roi_audit", "supervisor"], "unblock: a re-audit verified every deliverable",
                         "The new evidence passed the re-audit, so the report can go to the organizer for approval.",
                         use_llm=False)
            out.route = (*gate, "A3: the re-audit found no gaps; the report can be approved for publishing.")
        return
    if state.get("publish_with_gaps"):
        if not already(log, "A3", f"{key}:disclosed"):
            await settle(deps, log, "A3", f"{key}:disclosed", f"{len(gaps)} deliverable(s) still lack proof.",
                         ["roi_audit", "human"], "unblock: organizer chose to publish with the gaps disclosed",
                         "The organizer explicitly accepted publishing with every gap listed in the report.",
                         use_llm=False)
        out.route = (*gate, "A3: organizer chose to publish with gaps disclosed; approval still required.")
        return
    missing = "; ".join(f"{g['brand_name']}: '{g['deliverable']}' ({g['status']})" for g in gaps)
    conflict = f"ROI Audit could not verify {len(gaps)} promised deliverable(s): {missing}."
    resolution = "block publishing until new evidence passes a re-audit or the organizer publishes with gaps disclosed"
    template = "A sponsor will rely on this report, so it is not published while promised deliverables lack proof."
    why = await settle(deps, log, "A3", f"{key}:blocked", conflict, ["roi_audit", "human"], resolution, template)
    out.messages.append(await emit(deps, "supervisor", "human", "escalation", {
        "reason": "missing_proof", "arbitration": "A3",
        "missing": [{k: g[k] for k in ("brand_id", "brand_name", "deliverable", "status")} for g in gaps],
        "options": ["upload evidence and re-run the audit", "publish with gaps disclosed"]},
        f"Missing proof: {missing}. {why}"))
    out.update["phase"] = "roi_blocked"
    out.route = ("END", None, "A3: publishing blocked until the missing proof arrives or the organizer decides.")


async def arbitrate(state: EventState, deps, log: list[dict]) -> Outcome:
    out = Outcome()
    for rule in (bounce_vs_ranking, price_vs_deals, roi_gaps):
        await rule({**state, **out.update}, deps, log, out)
    return out

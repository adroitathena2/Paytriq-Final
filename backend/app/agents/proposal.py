"""Proposal agent: writes a brand-specific pitch with Bronze/Silver/Gold tiers and renders it as a PDF.

Work items come from the blackboard:
- every selected brand that has no proposal yet (new, version 1);
- every open ``revision_request`` addressed to the proposal agent. These come from Outreach's pre-send
  critique (L2), from a brand's pushback reply routed by the supervisor (L3), or from the organizer
  rejecting the send gate. Each produces the next version (v2, v3) honouring the constraint.

For each item the agent runs its own reasoning loop (agents/loop.py) with the tools ``draft_proposal``
(LLM writes pitch + tiers; expected reach is computed by the estimator), ``check_pricing`` (sanity
check against the brand's likely budget and any hard constraint, so it can redraft) and ``render_pdf``.
Hard constraints such as a maximum price are also enforced in code after the loop, and the agent
falls back to a plain template if the LLM is unavailable, so a run never stalls here.

L7 feasibility: before any PDF is rendered the agent checks that a deal can work at all, in two parts.
1. Brand fit is a judgement, made by Cloudflare Clef (core/clef.py) before any LLM call is spent on the brand:
   "can any student-event deliverable bring this business customers or brand value?". P(yes) <= 0.25 means
   no fit; between the thresholds the old rule (``NO_FIT_CATEGORIES``: wholesale, industrial, warehouse)
   decides and the organizer gets an ``escalation``; if Clef is unavailable the rule decides alone.
2. Cost per reach is arithmetic and stays in code: the Bronze tier's estimated reach must justify its
   price (at most ``MAX_COST_PER_REACH_INR`` per person).
If either fails, no PDF is rendered, the proposal is marked ``infeasible`` and Discovery gets a ``request``
(reason ``infeasible``, plus ``p_yes`` and ``decided_by`` when the fit judgement decided) to replace the brand.

L8 calibration: if this organizer's approved ROI reports have produced a reach factor for the brand's
category, every tier's expected reach is scaled by it and the proposal (and PDF) say
"estimate calibrated from N past events".
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from app.agents.loop import AgentTool, run_tool_loop
from app.core.clef import Judgement, judge, unsure_escalation
from app.core.llm import LLMError, ToolSpec
from app.graph.messages import emit, open_requests
from app.graph.state import Brand, EventState, Proposal, Tier
from app.tools.base import ToolInputError

AGENT = "proposal"
TIER_NAMES = ("Bronze", "Silver", "Gold")
# Rough sponsorship budget a local business of this kind can afford for one event (Gold tier ceiling, INR).
CATEGORY_BUDGET = {"cafe": 25000, "bakery": 15000, "salon": 20000, "stationery": 15000, "bookstore": 20000,
                   "restaurant": 40000, "gym": 40000, "coaching": 60000, "clothing": 50000, "electronics": 60000}
# Share of attendees expected to notice the brand on-ground, per tier.
ON_GROUND_SHARE = {"Bronze": 0.3, "Silver": 0.55, "Gold": 0.85}
# Business types that student-event deliverables (logos, stalls, posts aimed at students) do not help.
NO_FIT_CATEGORIES = {"wholesale": "it sells in bulk to shops, not to students",
                     "industrial": "it sells to factories, not to students",
                     "warehouse": "it has no walk-in customers"}
MAX_COST_PER_REACH_INR = 50  # Bronze price per person reached above this is not worth it for a local business
SOCIAL_RE = re.compile(r"\b(instagram|social|posts?|reels?|stor(y|ies)|linkedin)\b", re.IGNORECASE)

SYSTEM = """You are the Proposal agent of Paytriq. You write a sponsorship proposal for ONE local business
and one Indian college event: a short, specific pitch (why THIS brand gains from THIS audience) and three
tiers (Bronze, Silver, Gold) with concrete, verifiable deliverables (e.g. "Logo on the main stage banner",
"10x10 ft stall near registration", "1 Instagram post on the fest handle") and prices in INR that a business
of this size can afford. When revising, honour the constraint exactly and say what you changed.
Use your tools: draft_proposal, check_pricing (fix any warnings by drafting again), render_pdf, then finish."""


class TierDraft(BaseModel):
    name: Literal["Bronze", "Silver", "Gold"]
    price_inr: int
    deliverables: list[str]


class ProposalDraft(BaseModel):
    pitch: str
    tiers: list[TierDraft]
    changes_made: str = ""


def estimate_reach(event: dict, tier_name: str, deliverables: list[str]) -> int:
    """Expected reach = on-ground share of footfall for the tier, plus the organizers' social audience
    (approximated as 0.8 x footfall) when the tier includes a social media deliverable."""
    footfall = int(event.get("expected_footfall", 0))
    reach = footfall * ON_GROUND_SHARE[tier_name]
    if any(SOCIAL_RE.search(d) for d in deliverables):
        reach += footfall * 0.8
    return int(round(reach, -1))


def budget_for(brand: Brand) -> int:
    return CATEGORY_BUDGET.get(brand.category, 30000)


def pricing_warnings(tiers: list[Tier], brand: Brand, max_price: int | None) -> list[str]:
    warnings = []
    prices = [t.price_inr for t in tiers]
    if prices != sorted(prices) or len(set(prices)) != len(prices):
        warnings.append("Prices must strictly increase from Bronze to Gold.")
    if max_price and max(prices) > max_price:
        warnings.append(f"Constraint: no tier may cost more than Rs. {max_price:,}.")
    if max(prices) > budget_for(brand) * 1.5:
        warnings.append(f"Gold looks too expensive for a {brand.category}; typical ceiling ~Rs. {budget_for(brand):,}.")
    if any(len(t.deliverables) == 0 for t in tiers):
        warnings.append("Every tier needs at least one deliverable.")
    return warnings


FIT_PURPOSE = "proposal.feasibility"
FIT_QUESTION = ("Can any typical student-event sponsorship deliverable (logo on banners, stall, social media posts, "
                "discount coupons for students) realistically bring this business customers or brand value?")


async def brand_fit(deps, event: dict, brand: Brand) -> tuple[str | None, Judgement]:
    """L7 part 1: Clef judges whether any event deliverable fits this business; returns (why not, judgement)."""
    state = {"brand": {"name": brand.name, "category": brand.category, "address": brand.address,
                       "website": brand.website, "description": brand.fit_reason},
             "event": {"name": event.get("name"), "type": "Indian college fest", "college": event.get("college"),
                       "audience": event.get("audience"), "footfall": event.get("expected_footfall")}}
    j = await judge(deps, agent=AGENT, purpose=FIT_PURPOSE, state=state, question=FIT_QUESTION,
                    rule_answer=brand.category not in NO_FIT_CATEGORIES)
    if j.answer:
        return None, j
    why = NO_FIT_CATEGORIES.get(brand.category, "no student-event deliverable would bring it customers or brand value")
    return f"No event deliverable fits a {brand.category} business: {why}.", j


def infeasibility(brand: Brand, tiers: list[Tier] | None) -> str | None:
    """L7 part 2: why the Bronze price is not worth its reach, or None if it is (pure arithmetic)."""
    bronze = next((t for t in tiers or [] if t.name == "Bronze"), None)
    if bronze:
        per_person = bronze.price_inr / max(bronze.expected_reach, 1)
        if per_person > MAX_COST_PER_REACH_INR:
            return (f"Bronze reaches only ~{bronze.expected_reach} people for Rs {bronze.price_inr:,} "
                    f"(Rs {per_person:.0f} per person, limit Rs {MAX_COST_PER_REACH_INR}); not worth it for {brand.name}.")
    return None


def to_tiers(draft: ProposalDraft, event: dict) -> list[Tier]:
    by_name = {t.name: t for t in draft.tiers}
    missing = [n for n in TIER_NAMES if n not in by_name]
    if missing:
        raise ToolInputError(f"draft is missing tiers {missing}")
    return [Tier(name=n, price_inr=max(500, int(by_name[n].price_inr)), deliverables=by_name[n].deliverables,
                 expected_reach=estimate_reach(event, n, by_name[n].deliverables)) for n in TIER_NAMES]


def calibrate(proposal: Proposal, cal: dict | None) -> Proposal:
    """Scale expected reach by the organizer's calibration factor for this category (L8)."""
    if not cal:
        return proposal
    proposal.tiers = [t.model_copy(update={"expected_reach": int(round(t.expected_reach * cal["factor"], -1))})
                      for t in proposal.tiers]
    n = cal["n_events"]
    proposal.reach_note = f"estimate calibrated from {n} past event{'' if n == 1 else 's'}"
    return proposal


async def load_calibration(deps, brand: Brand) -> dict | None:
    try:
        owner = await deps.owner_id()
        return await deps.db.get_calibration(owner, brand.category) if owner else None
    except Exception:  # no calibration memory: plain estimates are still fine
        return None


def enforce_max_price(tiers: list[Tier], max_price: int) -> list[Tier]:
    """Scale prices down proportionally so Gold equals the cap; keeps the tier ladder intact."""
    top = max(t.price_inr for t in tiers)
    if top <= max_price:
        return tiers
    factor = max_price / top
    return [t.model_copy(update={"price_inr": int(round(t.price_inr * factor, -2))}) for t in tiers]


def template_proposal(event: dict, brand: Brand, version: int, max_price: int | None) -> Proposal:
    gold = min(budget_for(brand), max_price or budget_for(brand))
    deliverables = {
        "Bronze": ["Logo on event posters"],
        "Silver": ["Logo on event posters", "Stall space at the venue"],
        "Gold": ["Logo on the main stage banner", "Stall space at the venue", "1 Instagram post on the event handle"],
    }
    tiers = [Tier(name=n, price_inr=int(round(gold * f, -2)), deliverables=deliverables[n],
                  expected_reach=estimate_reach(event, n, deliverables[n]))
             for n, f in (("Bronze", 0.3), ("Silver", 0.6), ("Gold", 1.0))]
    pitch = (f"{event['name']} at {event['college']} brings {event['expected_footfall']:,} students "
             f"({event['audience']}) to within {brand.distance_km} km of {brand.name}.")
    return Proposal(brand_id=brand.id, version=version, tiers=tiers, pitch=pitch, status="draft")


class Item:
    """One unit of work: a new proposal or a revision of an existing one."""

    def __init__(self, brand: Brand, previous: Proposal | None, request=None) -> None:
        self.brand = brand
        self.previous = previous
        self.request = request
        self.version = (previous.version + 1) if previous else 1
        self.constraint = request.content.get("constraint") if request else None
        self.max_price = request.content.get("max_price_inr") if request else None
        self.draft: Proposal | None = None
        self.changes = ""
        self.calibration: dict | None = None
        self.fit: dict = {}  # {"p_yes", "decided_by"} when the L7 fit judgement rejected the brand
        self.messages: list = []  # e.g. an escalation when Clef was unsure about the brand


def build_tools(deps, state: EventState, item: Item) -> list[AgentTool]:
    event = state["event"]

    async def draft_proposal(guidance: str = "") -> dict:
        draft = await deps.llm.generate(
            purpose="proposal.write", system=SYSTEM, schema=ProposalDraft,
            context={"event": event, "brand": item.brand.model_dump(), "typical_budget_inr": budget_for(item.brand),
                     "sponsorship_goal_inr": event.get("budget_goal_inr"), "version": item.version,
                     "previous_version": item.previous.model_dump() if item.previous else None,
                     "revision_constraint": item.constraint, "max_price_inr": item.max_price,
                     "reach_calibration": item.calibration and {k: item.calibration[k] for k in ("factor", "n_events")},
                     "own_guidance": guidance},
        )
        tiers = to_tiers(draft, event)
        item.draft = calibrate(Proposal(brand_id=item.brand.id, version=item.version, tiers=tiers, pitch=draft.pitch),
                               item.calibration)
        item.changes = draft.changes_made
        return {"tiers": [f"{t.name}: Rs. {t.price_inr} ({len(t.deliverables)} deliverables, reach {t.expected_reach})"
                          for t in tiers], "pitch_words": len(draft.pitch.split())}

    async def check_pricing() -> dict:
        if item.draft is None:
            raise ToolInputError("draft a proposal first")
        warnings = pricing_warnings(item.draft.tiers, item.brand, item.max_price)
        return {"ok_to_render": not warnings, "warnings": warnings}

    async def render_pdf() -> dict:
        if item.draft is None:
            raise ToolInputError("draft a proposal first")
        reason = infeasibility(item.brand, item.draft.tiers)
        if reason:  # L7: never render a proposal that cannot work
            raise ToolInputError(f"infeasible, not rendered: {reason}")
        item.draft.pdf_url = await deps.pdf.render_and_store(deps.run_id, event, item.brand, item.draft)
        return {"pdf_url": item.draft.pdf_url}

    return [
        AgentTool(ToolSpec(name="draft_proposal", description="Write the pitch and the three tiers with the LLM.",
                           parameters={"type": "object", "properties": {"guidance": {"type": "string"}}}),
                  draft_proposal, timeout=120),
        AgentTool(ToolSpec(name="check_pricing", description="Check tier prices against budget and constraints."),
                  check_pricing),
        AgentTool(ToolSpec(name="render_pdf", description="Render the current draft to PDF and store it."),
                  render_pdf, timeout=60),
    ]


def work_items(state: EventState) -> list[Item]:
    brands = {b.id: b for b in state.get("brands", [])}
    proposals = state.get("proposals", {})
    items: dict[str, Item] = {}
    for request in open_requests(state, AGENT):
        brand_id = request.content.get("brand_id")
        if brand_id in brands and request.type == "revision_request":
            items[brand_id] = Item(brands[brand_id], proposals.get(brand_id), request)
    for brand_id in state.get("selected_brand_ids", []):
        if brand_id in brands and brand_id not in proposals and brand_id not in items:
            items[brand_id] = Item(brands[brand_id], None)
    return list(items.values())


async def process(state: EventState, deps, item: Item) -> tuple[Proposal, list[dict], str]:
    """Write or revise one proposal. An infeasible one comes back with status ``infeasible`` and the reason."""
    if item.previous is None:  # judged once per brand; a revision means it already passed
        reason, j = await brand_fit(deps, state["event"], item.brand)
        if j.escalate:
            item.messages.append(await unsure_escalation(
                deps, AGENT, FIT_PURPOSE, j, f"whether {item.brand.name} ({item.brand.category}) can gain anything "
                f"from this event", {"brand_id": item.brand.id}))
        if reason:  # no deliverable fits this business: do not spend LLM calls on it
            proposal = template_proposal(state["event"], item.brand, item.version, item.max_price)
            proposal.status = "infeasible"
            item.fit = {"p_yes": j.p_yes, "decided_by": j.source}
            return proposal, [], reason
    item.calibration = await load_calibration(deps, item.brand)
    task = "revise" if item.previous else "write"
    context = {"task": f"{task} the proposal for {item.brand.name}", "brand_id": item.brand.id,
               "version": item.version, "revision_constraint": item.constraint, "max_price_inr": item.max_price}
    result = await run_tool_loop(deps, AGENT, "proposal.plan", SYSTEM, context, build_tools(deps, state, item))
    note = item.changes or result.summary
    proposal = item.draft
    if proposal is None:  # the LLM never produced a usable draft: keep the run moving with a template
        proposal = calibrate(template_proposal(state["event"], item.brand, item.version, item.max_price),
                             item.calibration)
        note = "LLM unavailable or no valid draft; used the standard template."
    rendered_prices = [t.price_inr for t in proposal.tiers] if proposal.pdf_url else None
    if item.max_price:
        proposal.tiers = enforce_max_price(proposal.tiers, int(item.max_price))
    reason = infeasibility(item.brand, proposal.tiers)
    if reason:
        proposal.pdf_url, proposal.status = None, "infeasible"
        return proposal, result.errors, reason
    if rendered_prices != [t.price_inr for t in proposal.tiers]:  # never rendered, or prices changed since
        try:
            proposal.pdf_url = await deps.pdf.render_and_store(deps.run_id, state["event"], item.brand, proposal)
        except Exception as exc:
            result.errors.append({"agent": AGENT, "tool": "render_pdf", "error": str(exc)})
    proposal.status = "draft"
    return proposal, result.errors, note


async def run(state: EventState, deps) -> dict:
    items = work_items(state)
    proposals = {k: v.model_copy(deep=True) for k, v in state.get("proposals", {}).items()}
    errors = list(state.get("errors", []))
    messages = []
    for item in items:
        try:
            proposal, item_errors, note = await process(state, deps, item)
        except LLMError as exc:  # defensive: process already degrades, but never crash the node
            proposal, item_errors, note = template_proposal(state["event"], item.brand, item.version, item.max_price), [], str(exc)
        proposals[item.brand.id] = proposal
        errors += item_errors
        messages += item.messages
        if proposal.status == "infeasible":  # L7: hand the brand back to Discovery instead of pitching it
            content = {"reason": "infeasible", "brand_id": item.brand.id, "why": note, **item.fit}
            if item.request:
                content["in_reply_to"] = [item.request.id]
            messages.append(await emit(deps, AGENT, "discovery", "request", content, note))
            continue
        content = {"brand_id": item.brand.id, "version": proposal.version, "pdf_url": proposal.pdf_url,
                   "prices": {t.name: t.price_inr for t in proposal.tiers}}
        if item.request:
            content["in_reply_to"] = [item.request.id]
            content["constraint"] = item.constraint
        # Revisions go back to whoever asked; new drafts go to Outreach for its pre-send critique.
        to = item.request.from_agent if item.request and item.request.from_agent != "human" else "outreach"
        messages.append(await emit(deps, AGENT, to, "result", content, note or f"Proposal v{proposal.version} ready."))
    if not items:
        messages.append(await emit(deps, AGENT, "supervisor", "result", {"note": "no proposal work pending"},
                                   "Nothing to write or revise."))
        return {"messages": messages}
    drafted = any(p.status == "draft" for p in proposals.values())
    return {"proposals": proposals, "messages": messages, "errors": errors,
            "phase": "review" if drafted else "replacement"}

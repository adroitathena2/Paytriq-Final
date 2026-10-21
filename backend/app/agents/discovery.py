"""Discovery & Ranking agent: finds businesses near campus and ranks them for this event's audience.

Reasoning loop (agents/loop.py): the LLM chooses between ``search_places`` (which categories, what
radius), ``score_brands`` (LLM fit scoring for the audience) and ``find_contact`` (look up an email on
the brand's website), observing each result, until it calls ``finish``.

Adaptation:
- L1 self-replan. The first pass searches at most ``START_RADIUS_KM``. After a pass, if fewer than
  ``GOOD_TARGET`` brands score >= ``GOOD_FIT``, the agent asks the LLM for a replan (wider radius, at most
  4 km; adjacent categories), posts a ``replan`` message explaining why, and runs another pass with the
  radius cap raised to the plan's radius. At most ``MAX_REPLANS`` times. So every widening of the search
  is a traced ``replan`` decision, never a silent step inside one pass.
- Tool failure: a failed search is an observation; the LLM can retry differently. Places itself falls
  back from Google to OpenStreetMap. If nothing at all is found, the agent escalates to the organizer.
- Organizer feedback: a rejected shortlist arrives as a ``request`` with feedback that is put in front
  of the LLM and used when (re)scoring.
- L4 replacement: when Outreach reports a decline/bounce, the supervisor sends Discovery here with a
  ``request`` (reason ``replacement``); the agent picks the next-best unused brand (searching again if
  needed) and replies with the replacement. Every open request gets its own replacement brand.
- L6 category feedback: when Outreach reports two negative outcomes in one category (``request`` with
  reason ``category_feedback``), every brand of that category is down-weighted (x ``CATEGORY_PENALTY``),
  the list is re-ranked, and replacements (preferring other categories) are pulled until the organizer's
  shortlist size is met again.
- L7 infeasible: when Proposal finds no workable deal for a brand (reason ``infeasible``), it is replaced
  like a decline.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.loop import AgentTool, run_tool_loop
from app.core.llm import LLMError, ToolSpec
from app.graph.messages import emit, open_requests
from app.graph.state import Brand, EventState
from app.tools.base import ToolInputError
from app.tools.places import CATEGORIES

AGENT = "discovery"
GOOD_FIT = 0.6
GOOD_TARGET = 5
MAX_REPLANS = 2
MAX_RADIUS_KM = 4.0
START_RADIUS_KM = 2.0
CATEGORY_PENALTY = 0.6
REPLACE_REASONS = ("replacement", "infeasible")
ADJACENT = {
    "cafe": ["bakery", "restaurant"], "restaurant": ["cafe", "bakery"], "gym": ["salon", "clothing"],
    "salon": ["gym", "clothing"], "coaching": ["bookstore", "stationery"], "bookstore": ["stationery", "coaching"],
    "stationery": ["bookstore", "electronics"], "clothing": ["salon", "electronics"],
    "electronics": ["stationery", "clothing"], "bakery": ["cafe", "restaurant"],
}

SYSTEM = f"""You are the Discovery & Ranking agent of Paytriq. You find local businesses near an Indian
college campus that would sponsor a student event, and rank how well each fits the event's audience.
Goal: at least {GOOD_TARGET} brands with fit_score >= {GOOD_FIT}, ideally with a contact email.
Work step by step with your tools: choose categories that match the audience, search, score, and look up
contacts for promising brands that lack an email. If a tool fails, adapt (different categories, smaller
query) rather than repeating the same call. Always refer to brands by their id (e.g. "demo-bean-theory"),
as shown in tool results. Call finish with a short summary when done.
Known categories: {", ".join(sorted(CATEGORIES))}. Never search wider than the max_radius_km you are given;
if that is not enough, finish and the agent will decide whether to replan wider."""


class BrandScore(BaseModel):
    id: str
    fit_score: float = Field(ge=0, le=1)
    fit_reason: str


class ScoreBatch(BaseModel):
    scores: list[BrandScore]


class ReplanPlan(BaseModel):
    radius_km: float
    extra_categories: list[str]
    reasoning: str


class Workspace:
    """Mutable working memory for one agent turn."""

    def __init__(self, state: EventState) -> None:
        self.event = state["event"]
        self.candidates: dict[str, Brand] = {b.id: b.model_copy() for b in state.get("brands", [])}
        self.scored: set[str] = {b.id for b in state.get("brands", []) if b.fit_reason}
        self.searches: list[dict] = []
        self.picked: str | None = None
        self.pick_reason = ""
        self.radius_cap = MAX_RADIUS_KM  # discovery passes lower this to START_RADIUS_KM until a replan
        self.penalised: set[str] = set()  # categories down-weighted after negative outreach outcomes

    def lookup(self, key: str) -> Brand | None:
        """Find a candidate by id, or by exact name (case-insensitive) when the LLM passes the name."""
        if key in self.candidates:
            return self.candidates[key]
        return next((b for b in self.candidates.values() if b.name.lower() == key.strip().lower()), None)

    def good(self) -> list[Brand]:
        return sorted((b for b in self.candidates.values() if b.fit_score >= GOOD_FIT and not b.unreachable),
                      key=lambda b: b.fit_score, reverse=True)

    def ranked(self) -> list[Brand]:
        """Best first; unreachable brands and penalised categories go to the end."""
        return sorted(self.candidates.values(), reverse=True, key=lambda b: (
            not b.unreachable, b.category not in self.penalised, b.fit_score, -b.distance_km))


def build_tools(deps, ws: Workspace, guidance: str = "", excluded: set[str] | None = None) -> list[AgentTool]:
    excluded = excluded or set()

    async def search_places(categories: list[str], radius_km: float = 2.0) -> dict:
        radius = max(0.5, min(float(radius_km), ws.radius_cap))
        result = await deps.places.search(ws.event["lat"], ws.event["lng"], radius, list(categories))
        new = [p for p in result["places"] if p.id not in ws.candidates]
        for p in new:
            ws.candidates[p.id] = p
        ws.searches.append({"categories": categories, "radius_km": radius, "source": result["source"], "found": len(new)})
        out = {"source": result["source"], "fallback_reason": result["fallback_reason"], "radius_km": radius,
               "new_brands": len(new), "total_candidates": len(ws.candidates),
               "sample": [f"{p.id}: {p.name} ({p.category}, {p.distance_km} km)" for p in new[:8]]}
        if float(radius_km) > ws.radius_cap:
            out["note"] = (f"radius capped at {ws.radius_cap} km for this pass; finish if that is not enough "
                           "and the agent will decide whether to replan wider")
        return out

    async def score_brands(rescore_all: bool = False) -> dict:
        todo = [b for b in ws.candidates.values() if rescore_all or b.id not in ws.scored][:40]
        if not todo:
            return {"scored": 0, "good_fit": len(ws.good()), "note": "nothing new to score"}
        batch = await deps.llm.generate(
            purpose="discovery.score",
            system="Score each business 0-1 for how well sponsoring this student event fits it: audience overlap, "
                   "proximity, plausible budget, likelihood to want student footfall. One short reason each.",
            context={"event": ws.event, "organizer_guidance": guidance,
                     "brands": [{"id": b.id, "name": b.name, "category": b.category, "distance_km": b.distance_km,
                                 "has_email": bool(b.contact_email)} for b in todo]},
            schema=ScoreBatch,
        )
        for s in batch.scores:
            if s.id in ws.candidates:
                ws.candidates[s.id].fit_score = round(s.fit_score, 2)
                ws.candidates[s.id].fit_reason = s.fit_reason
                ws.scored.add(s.id)
        good = ws.good()
        return {"scored": len(batch.scores), "good_fit": len(good),
                "top": [f"{b.id}: {b.name} {b.fit_score}" for b in good[:6]]}

    async def find_contact(brand_id: str) -> dict:
        brand = ws.lookup(brand_id)
        if brand is None:
            raise ToolInputError(f"unknown brand_id {brand_id}; use an id from the search results")
        brand_id = brand.id
        if brand.contact_email:
            return {"brand_id": brand_id, "email": brand.contact_email, "note": "already known"}
        if not brand.website:
            return {"brand_id": brand_id, "email": None, "note": "no website listed"}
        email = await deps.places.find_contact(brand.website)
        brand.contact_email = email
        return {"brand_id": brand_id, "email": email}

    async def list_alternatives() -> dict:
        options = [b for b in ws.ranked() if b.id not in excluded][:6]
        return {"options": [{"id": b.id, "name": b.name, "category": b.category, "fit_score": b.fit_score,
                             "has_email": bool(b.contact_email), "distance_km": b.distance_km} for b in options]}

    async def pick_replacement(brand_id: str, why: str = "") -> dict:
        brand = ws.lookup(brand_id)
        if brand is None or brand.id in excluded:
            available = [b.id for b in ws.ranked() if b.id not in excluded][:6]
            raise ToolInputError(f"{brand_id} is not an available alternative; available ids: {', '.join(available)}")
        ws.picked, ws.pick_reason = brand.id, why
        return {"picked": brand.name, "brand_id": brand.id}

    string_list = {"type": "array", "items": {"type": "string"}}
    tools = [
        AgentTool(ToolSpec(name="search_places", description="Search businesses near the campus.", parameters={
            "type": "object", "properties": {
                "categories": {**string_list, "description": f"Any of: {', '.join(sorted(CATEGORIES))}"},
                "radius_km": {"type": "number", "description": "0.5 up to max_radius_km for this pass"}},
            "required": ["categories", "radius_km"]}), search_places, timeout=150),
        AgentTool(ToolSpec(name="score_brands", description="Score fit (0-1) of unscored candidates with the LLM.",
                           parameters={"type": "object", "properties": {"rescore_all": {"type": "boolean"}}}),
                  score_brands, timeout=120),
        AgentTool(ToolSpec(name="find_contact", description="Look up a contact email on the brand's website.",
                           parameters={"type": "object", "properties": {"brand_id": {"type": "string"}},
                                       "required": ["brand_id"]}), find_contact, timeout=25),
    ]
    if excluded:
        tools += [
            AgentTool(ToolSpec(name="list_alternatives", description="Best unused candidates for a replacement."),
                      list_alternatives),
            AgentTool(ToolSpec(name="pick_replacement", description="Choose the replacement brand.", parameters={
                "type": "object", "properties": {"brand_id": {"type": "string"}, "why": {"type": "string"}},
                "required": ["brand_id"]}), pick_replacement),
        ]
    return tools


async def plan_replan(deps, ws: Workspace, attempt: int, radius: float, categories: list[str]) -> ReplanPlan:
    try:
        plan = await deps.llm.generate(
            purpose="discovery.replan",
            system=f"Too few good-fit brands were found. Plan a wider search: a radius up to {MAX_RADIUS_KM} km and "
                   "adjacent categories likely to sponsor this audience. Explain the reasoning in one sentence.",
            context={"event": ws.event, "good_fit": len(ws.good()), "target": GOOD_TARGET, "attempt": attempt,
                     "searches_so_far": ws.searches, "known_categories": sorted(CATEGORIES)},
            schema=ReplanPlan,
        )
    except LLMError:
        extra = sorted({a for c in categories for a in ADJACENT.get(c, [])} - set(categories))
        plan = ReplanPlan(radius_km=MAX_RADIUS_KM, extra_categories=extra,
                          reasoning="LLM unavailable; widening to the maximum radius with adjacent categories.")
    plan.radius_km = max(radius, min(plan.radius_km, MAX_RADIUS_KM))
    plan.extra_categories = [c for c in plan.extra_categories if c in CATEGORIES]
    return plan


async def run(state: EventState, deps) -> dict:
    requests = open_requests(state, AGENT)
    replacements = [r for r in requests if r.content.get("reason") in REPLACE_REASONS]
    categories = [r for r in requests if r.content.get("reason") == "category_feedback"]
    if replacements or categories:  # other requests (e.g. organizer feedback) stay open for the next turn
        return await _replace_all(state, deps, replacements, categories)
    feedback = " ".join(r.content.get("feedback", "") for r in requests if r.content.get("feedback"))
    return await _discover(state, deps, feedback, [r.id for r in requests])


async def _discover(state: EventState, deps, feedback: str, request_ids: list[str]) -> dict:
    ws = Workspace(state)
    ws.radius_cap = START_RADIUS_KM  # only a replan may widen the search (L1)
    tools = build_tools(deps, ws, guidance=feedback)
    messages, errors = [], list(state.get("errors", []))
    context: dict = {"task": "discover_and_rank", "event": ws.event, "organizer_feedback": feedback or None,
                     "known_brands": len(ws.candidates), "max_radius_km": ws.radius_cap}
    radius, categories_used = START_RADIUS_KM, []

    for attempt in range(MAX_REPLANS + 1):
        result = await run_tool_loop(deps, AGENT, "discovery.plan", SYSTEM, context, tools)
        errors += result.errors
        categories_used = sorted({c for s in ws.searches for c in s["categories"]})
        radius = max([s["radius_km"] for s in ws.searches] or [radius])
        if len(ws.good()) >= GOOD_TARGET or attempt == MAX_REPLANS or result.llm_failed:
            break
        plan = await plan_replan(deps, ws, attempt + 1, radius, categories_used)
        messages.append(await emit(deps, AGENT, AGENT, "replan", {
            "attempt": attempt + 1, "good_fit": len(ws.good()), "target": GOOD_TARGET,
            "new_radius_km": plan.radius_km, "extra_categories": plan.extra_categories,
        }, plan.reasoning))
        ws.radius_cap = plan.radius_km
        context = {**context, "max_radius_km": ws.radius_cap, "replan": {"attempt": attempt + 1, "radius_km": plan.radius_km,
                                          "categories": sorted(set(categories_used) | set(plan.extra_categories)),
                                          "why": plan.reasoning}}

    brands = ws.ranked()
    summary = {"found": len(brands), "good_fit": len(ws.good()), "searches": ws.searches,
               "in_reply_to": request_ids}
    if not brands:
        llm_down = any(e.get("tool") == "llm" for e in errors[-3:])
        reason = ("The language model is unavailable, so discovery could not run. Check GEMINI_API_KEY or retry later."
                  if llm_down else "No businesses could be found; the map services may be down. Please retry later.")
        messages.append(await emit(deps, AGENT, "human", "escalation", {**summary, "errors": errors[-3:]}, reason))
        return {"messages": messages, "errors": errors, "phase": "discovery_failed"}
    messages.append(await emit(deps, AGENT, "supervisor", "result", summary,
                               f"Ranked {len(brands)} brands; {len(ws.good())} are a good fit."))
    return {"brands": brands, "messages": messages, "errors": errors, "phase": "shortlist_review"}


def dropped(state: EventState, brand_id: str) -> bool:
    """A selected brand that no longer counts toward the shortlist (declined, bounced, infeasible, unreachable)."""
    thread = state.get("outreach", {}).get(brand_id)
    proposal = state.get("proposals", {}).get(brand_id)
    brand = next((b for b in state.get("brands", []) if b.id == brand_id), None)
    return bool((thread and thread.status in ("declined", "bounced"))
                or (proposal and proposal.status in ("declined", "infeasible")) or (brand and brand.unreachable))


async def _pick_one(state: EventState, deps, ws: Workspace, request, used: set[str]) -> tuple[str | None, list]:
    """One reasoning loop that picks a replacement for one request; falls back to the best unused brand."""
    ws.picked, ws.pick_reason = None, ""
    gone = request.content.get("brand_id")
    context = {"task": "find_replacement", "event": ws.event, "replace_brand": gone,
               "reason": request.content.get("reason"), "why": request.content.get("why"),
               "penalised_categories": sorted(ws.penalised),
               "instructions": "Pick the best unused brand, preferably with an email and a different angle from the "
                               "one being replaced; avoid penalised categories. Search again only if no option is good."}
    result = await run_tool_loop(deps, AGENT, "discovery.replace", SYSTEM, context, build_tools(deps, ws, excluded=used))
    if ws.picked is None:  # the LLM did not decide: best unused good-fit brand
        fallback = next((b for b in ws.ranked() if b.id not in used and b.fit_score >= GOOD_FIT and not b.unreachable),
                        None)
        if fallback:
            ws.picked, ws.pick_reason = fallback.id, "Highest-ranked unused brand (automatic choice)."
    return ws.picked, result.errors


async def _replace_all(state: EventState, deps, replacements: list, categories: list) -> dict:
    ws = Workspace(state)
    errors, messages = list(state.get("errors", [])), []
    selected = list(state.get("selected_brand_ids", []))
    used = set(selected) | set(state.get("outreach", {})) | {r.content.get("brand_id") for r in replacements}

    # L6: down-weight a category that keeps failing, so the replacements below come from other categories.
    for request in categories:
        category = request.content.get("category")
        hit = [b for b in ws.candidates.values() if b.category == category]
        for b in hit:
            b.fit_score = round(b.fit_score * CATEGORY_PENALTY, 2)
        ws.penalised.add(category)
        messages.append(await emit(deps, AGENT, "supervisor", "result", {
            "reason": "category_penalty", "category": category, "factor": CATEGORY_PENALTY, "rescored": len(hit),
            "evidence": request.content.get("evidence", [])},
            f"{category} brands had {request.content.get('negative', 2)} negative outcomes; scores x{CATEGORY_PENALTY} "
            "and re-ranked so replacements come from other categories."))

    # L4 / L7: one replacement per request.
    picks: list[str] = []
    for request in replacements:
        gone = request.content.get("brand_id")
        picked, loop_errors = await _pick_one(state, deps, ws, request, used)
        errors += loop_errors
        content = {"replacement_for": gone, "brand_id": picked, "reason": request.content.get("reason"),
                   "in_reply_to": [request.id]}
        if picked is None:
            messages.append(await emit(deps, AGENT, "human", "escalation", content,
                                       f"No suitable replacement for {gone} is left; consider widening the search."))
            continue
        used.add(picked)
        picks.append(picked)
        messages.append(await emit(deps, AGENT, "supervisor", "result", content,
                                   ws.pick_reason or f"Replacing {gone} with {ws.candidates[picked].name}."))

    # L6: top up until the organizer's shortlist size is met again.
    if categories:
        new_selected = selected + picks
        target = state.get("target_brand_count") or len(selected)
        active = [b for b in new_selected if not dropped(state, b)]
        extra = [b for b in ws.ranked() if b.id not in used and not b.unreachable
                 and b.fit_score >= GOOD_FIT][:max(0, target - len(active))]
        picks += [b.id for b in extra]
        messages.append(await emit(deps, AGENT, "supervisor", "result", {
            "reason": "category_feedback", "categories": sorted(ws.penalised), "added": [b.id for b in extra],
            "target": target, "in_reply_to": [r.id for r in categories]},
            f"Re-ranked after category feedback; added {len(extra)} brand(s) to keep {target} active sponsors in play."))

    update = {"messages": messages, "errors": errors, "brands": ws.ranked()}
    if picks:
        update.update(phase="proposals", selected_brand_ids=selected + picks)
    return update

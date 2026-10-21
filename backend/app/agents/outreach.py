"""Outreach agent: critiques proposals before sending, sends approved emails, and handles replies.

Its reasoning loop (agents/loop.py) is offered only the tools that fit the current situation:

- Draft proposals waiting -> ``critique_proposals`` (LLM checks tone, length, price vs brand size and
  writes the email), then per brand either ``request_revision`` (L2: a revision_request to the Proposal
  agent, max one per brand per version) or ``prepare_emails`` (queues drafts for the organizer's send gate).
- Send gate approved -> ``send_approved`` (Gmail; in DEMO_MODE redirected to team inboxes). This is the
  only place email leaves the system and it re-checks the approval itself. A Gmail failure keeps the
  email as a draft and escalates to the organizer.
- Polling requested -> ``poll_replies`` then ``classify_replies`` (interested, accepted, pushback_price,
  pushback_deliverables, declined, out_of_office, unrelated), and per brand: ``route_pushback`` (L3:
  revision_request with the brand's constraint), ``request_replacement`` (L4: decline or bounce, asks
  Discovery), ``mark_accepted``, or ``draft_nudge`` for threads silent for NUDGE_AFTER_DAYS.

Every outcome (accepted, declined, bounced, silent after a nudge) is recorded per brand in ``feedback``.
L6: when one category collects ``CATEGORY_NEGATIVE_LIMIT`` negative outcomes, Outreach asks Cloudflare Clef
(core/clef.py) "given these outcomes, should we stop prioritising this category?". The counter only decides
when to ask. On yes (P(yes) >= 0.75, or the rule fallback when Clef is unavailable or unsure) Outreach tells
Discovery (``request`` with reason ``category_feedback``, the evidence, ``p_yes`` and ``decided_by``) so it stops
pitching that category; when Clef is unsure the organizer also gets an ``escalation``. A "no" is asked again
only after a new negative outcome in that category. Reply classification stays on Gemini.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from app.agents.loop import AgentTool, run_tool_loop
from app.core.clef import judge, unsure_escalation
from app.core.llm import ToolSpec
from app.graph.messages import as_message, emit, open_requests
from app.graph.state import EventState, OutreachThread
from app.tools.base import ToolError, ToolInputError
from app.tools.gmail import demo_subject
from app.tools.pdf import render_proposal_pdf

AGENT = "outreach"
LIVE_STATUSES = {"sent", "nudged", "replied", "pushback"}
# Statuses that mean the organizer is still waiting on a brand to decide. "nudged" is deliberately not
# here: the follow-up has been sent and the silence recorded, and a later reply is still picked up by
# poll_replies because polling uses LIVE_STATUSES. Treating "nudged" as waiting kept demo runs stuck on
# "Waiting for replies" forever when a fixture brand had no scripted reply.
WAITING_STATUSES = {"sent", "replied", "pushback"}
NEGATIVE_OUTCOMES = {"declined", "bounced", "silent"}
PUSHBACK_LABELS = {"pushback_price", "pushback_deliverables"}
CATEGORY_NEGATIVE_LIMIT = 2  # when to ask Clef about a category, not the answer
CATEGORY_PURPOSE = "outreach.category_feedback"
CATEGORY_QUESTION = "Given these outcomes, should we stop prioritising this business category for this event?"
ReplyLabel = Literal["interested", "accepted", "pushback_price", "pushback_deliverables", "declined",
                     "out_of_office", "unrelated"]

SYSTEM = """You are the Outreach agent of Paytriq. You protect the organizer's reputation with local
businesses. Before anything is sent you critique each proposal like a sceptical shop owner would: is the
pitch specific and short, are the deliverables concrete, is the price realistic for this business? Ask the
Proposal agent for a revision only when it would clearly improve the odds (one revision per brand).
After sending, you read replies, classify them, and choose the next action for each brand: route price or
deliverable pushback to the Proposal agent with the brand's exact constraint, ask Discovery for a
replacement when a brand declines or the email bounced, mark acceptances, and nudge silent brands.
Use one tool per step and call finish with a short summary when the situation is handled."""


class Critique(BaseModel):
    brand_id: str
    verdict: Literal["ok", "revise"]
    issues: list[str]
    revision_constraint: str = ""
    max_price_inr: int | None = None
    email_subject: str
    email_body: str


class CritiqueBatch(BaseModel):
    critiques: list[Critique]


class ReplyClass(BaseModel):
    brand_id: str
    label: ReplyLabel
    summary: str
    accepted_tier: Literal["Bronze", "Silver", "Gold"] | None = None
    constraint: str | None = None
    max_price_inr: int | None = None


class ReplyBatch(BaseModel):
    replies: list[ReplyClass]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def last_entry(thread: OutreachThread, type_: str) -> dict | None:
    return next((e for e in reversed(thread.history) if e.get("type") == type_), None)


def critique_revisions_used(state: EventState, brand_id: str) -> int:
    """Pre-send revision requests this agent already made for this brand (the limit is one)."""
    return sum(1 for m in map(as_message, state.get("messages", []))
               if m.from_agent == AGENT and m.type == "revision_request" and m.content.get("brand_id") == brand_id
               and m.content.get("source") == "critique")


class Workspace:
    def __init__(self, state: EventState) -> None:
        self.state = state
        self.event = state["event"]
        self.brands = {b.id: b for b in state.get("brands", [])}
        self.proposals = {k: v.model_copy(deep=True) for k, v in state.get("proposals", {}).items()}
        self.threads = {k: v.model_copy(deep=True) for k, v in state.get("outreach", {}).items()}
        self.critiques: dict[str, Critique] = {}
        self.replies: dict[str, dict] = {}
        self.classes: dict[str, ReplyClass] = {}
        self.messages: list = []
        self.errors: list[dict] = []
        self.sent: list[str] = []
        self.send_attempted = False
        self.feedback: dict[str, list[str]] = {k: list(v) for k, v in state.get("feedback", {}).items()}
        self.category_checks: dict[str, int] = {}  # L6 categories Clef said to keep this step

    def record_outcome(self, brand_id: str, outcome: str) -> None:
        outcomes = self.feedback.setdefault(brand_id, [])
        if outcome not in outcomes:
            outcomes.append(outcome)

    def thread(self, brand_id: str) -> OutreachThread:
        return self.threads.setdefault(brand_id, OutreachThread(brand_id=brand_id))

    def drafts_to_review(self) -> list[str]:
        return [b for b, p in self.proposals.items() if p.status == "draft"]

    def send_approved(self) -> bool:
        pending = self.state.get("pending_approval") or {}
        return pending.get("gate") == "send" and pending.get("status") == "approved"

    def silent_threads(self, after_days: float) -> list[str]:
        out = []
        for brand_id, t in self.threads.items():
            sent = last_entry(t, "sent")
            if t.status in ("sent", "nudged") and sent and not last_entry(t, "reply"):
                age = (datetime.now(UTC) - datetime.fromisoformat(sent["ts"])).total_seconds() / 86400
                if age >= after_days:
                    out.append(brand_id)
        return out


def classification_for(ws: Workspace, brand_id: str) -> dict | None:
    """The latest reply classification, from this turn's LLM result or the thread's stored history.

    Reading the history matters for recovery: after a deploy, a run whose reply was classified in an
    earlier segment still carries that label, so the safety net can finish the action without polling
    Gmail (and without re-classifying) again.
    """
    cls = ws.classes.get(brand_id)
    if cls is not None:
        return cls.model_dump()
    thread = ws.threads.get(brand_id)
    entry = next((e for e in reversed(thread.history) if e.get("type") == "classified"), None) if thread else None
    return dict(entry) if entry else None


async def route_pushback(deps, ws: Workspace, brand_id: str, constraint: str = "",
                         max_price_inr: int | None = None, cls: dict | None = None) -> dict:
    """L3: send a brand's price/deliverable constraint to the Proposal agent as a revision request."""
    cls = cls if cls is not None else classification_for(ws, brand_id)
    if brand_id not in ws.proposals:
        raise ToolInputError(f"no proposal for {brand_id}")
    content = {"brand_id": brand_id, "constraint": constraint or (cls or {}).get("constraint") or "",
               "source": "brand_reply", "reply_label": (cls or {}).get("label"),
               "from_version": ws.proposals[brand_id].version}
    cap = max_price_inr or (cls or {}).get("max_price_inr")
    if cap:
        content["max_price_inr"] = int(cap)
    reasoning = (cls or {}).get("summary") or constraint
    ws.messages.append(await emit(deps, AGENT, "proposal", "revision_request", content, reasoning))
    ws.thread(brand_id).status = "pushback"
    ws.proposals[brand_id].status = "needs_revision"
    return {"routed_to": "proposal", "constraint": content["constraint"]}


async def mark_accepted(deps, ws: Workspace, brand_id: str, tier: str = "",
                        cls: dict | None = None) -> dict:
    cls = cls if cls is not None else classification_for(ws, brand_id)
    tier = tier or (cls or {}).get("accepted_tier") or "Silver"
    if tier not in ("Bronze", "Silver", "Gold"):
        raise ToolInputError("tier must be Bronze, Silver or Gold")
    thread = ws.thread(brand_id)
    thread.status = "accepted"
    thread.history.append({"type": "accepted", "tier": tier, "ts": now_iso()})
    if brand_id in ws.proposals:
        ws.proposals[brand_id].status = "accepted"
    ws.record_outcome(brand_id, "accepted")
    deal = await record_deal(deps, ws, brand_id, tier)
    return {"accepted": brand_id, "tier": tier, "deal_recorded": deal is not None}


def build_tools(deps, ws: Workspace, polling: bool) -> list[AgentTool]:
    s = deps.settings

    async def critique_proposals(brand_ids: list[str]) -> dict:
        todo = [b for b in brand_ids if b in ws.proposals and b in ws.brands]
        if not todo:
            raise ToolInputError("no draft proposals with those ids")
        batch = await deps.llm.generate(
            purpose="outreach.critique", system=SYSTEM, schema=CritiqueBatch,
            context={"event": ws.event, "items": [{
                "brand": ws.brands[b].model_dump(), "proposal": ws.proposals[b].model_dump(),
                "thread_status": ws.threads[b].status if b in ws.threads else None,
                "last_reply": (last_entry(ws.threads[b], "reply") or {}).get("text") if b in ws.threads else None,
                "revisions_left": max(0, 1 - critique_revisions_used(ws.state, b)),
            } for b in todo], "instructions": "One critique per brand. Write a short, warm, specific email "
                                              "(under 150 words) that introduces the attached proposal."},
        )
        for c in batch.critiques:
            if c.brand_id in todo:
                ws.critiques[c.brand_id] = c
        return {"verdicts": {c.brand_id: {"verdict": c.verdict, "issues": c.issues,
                                          "revisions_left": max(0, 1 - critique_revisions_used(ws.state, c.brand_id))}
                             for c in batch.critiques}}

    async def request_revision(brand_id: str, constraint: str, max_price_inr: int | None = None) -> dict:
        if brand_id not in ws.proposals:
            raise ToolInputError(f"unknown brand {brand_id}")
        if critique_revisions_used(ws.state, brand_id) >= 1 or any(
                m.content.get("brand_id") == brand_id for m in ws.messages if m.type == "revision_request"):
            return {"accepted": False, "note": "revision limit reached for this brand; prepare the email instead"}
        content = {"brand_id": brand_id, "constraint": constraint, "source": "critique",
                   "from_version": ws.proposals[brand_id].version}
        if max_price_inr:
            content["max_price_inr"] = int(max_price_inr)
        critique = ws.critiques.get(brand_id)
        reasoning = "; ".join(critique.issues) if critique and critique.issues else constraint
        ws.messages.append(await emit(deps, AGENT, "proposal", "revision_request", content, reasoning))
        ws.proposals[brand_id].status = "needs_revision"
        return {"accepted": True}

    async def prepare_emails(brand_ids: list[str]) -> dict:
        prepared = []
        for brand_id in brand_ids:
            critique, proposal = ws.critiques.get(brand_id), ws.proposals.get(brand_id)
            if critique is None or proposal is None or proposal.status != "draft":
                continue
            thread = ws.thread(brand_id)
            kind = "revised" if thread.status in LIVE_STATUSES else "initial"
            thread.history.append({"type": "email_draft", "kind": kind, "subject": critique.email_subject,
                                   "body": critique.email_body, "version": proposal.version, "state": "pending",
                                   "ts": now_iso()})
            proposal.status = "ready"
            prepared.append(brand_id)
        if not prepared:
            raise ToolInputError("critique the proposals first; only critiqued drafts can be prepared")
        return {"prepared": prepared, "note": "waiting for the organizer's send approval"}

    async def send_approved() -> dict:
        if not ws.send_approved():  # guardrail: never send without the organizer's approval
            raise ToolError("no approved send gate; emails cannot be sent")
        return await _send_all(deps, ws)

    async def poll_replies() -> dict:
        summary = {}
        for brand_id, thread in ws.threads.items():
            if thread.status not in LIVE_STATUSES or not thread.thread_id:
                continue
            seen = {e.get("gmail_message_id") for e in thread.history}
            messages = await deps.gmail.get_thread(thread.thread_id)
            new = [m for m in messages if m["id"] not in seen and not deps.gmail.is_own(m["from"])]
            if not new:
                continue
            latest = new[-1]
            for m in new:
                thread.history.append({"type": "reply", "gmail_message_id": m["id"], "from": m["from"],
                                       "text": m["text"][:2000], "bounce": m["is_bounce"], "ts": now_iso()})
            if any(m["is_bounce"] for m in new):
                thread.status = "bounced"
                summary[brand_id] = "BOUNCED (delivery failed)"
                continue
            thread.status = "replied"
            message_id_header = latest.get("message_id_header")
            if message_id_header:
                thread.history[-1]["message_id_header"] = message_id_header
            ws.replies[brand_id] = {"text": latest["text"][:1500], "from": latest["from"]}
            summary[brand_id] = latest["text"][:300]
        silent = ws.silent_threads(s.nudge_after_days)
        for brand_id in silent:  # already nudged and still silent: count it as a negative outcome
            if ws.threads[brand_id].status == "nudged":
                ws.record_outcome(brand_id, "silent")
        return {"new_replies": summary, "silent_threads": silent, "nudge_after_days": s.nudge_after_days}

    async def classify_replies() -> dict:
        if not ws.replies:
            return {"classified": {}, "note": "no new replies to classify"}
        batch = await deps.llm.generate(
            purpose="outreach.classify", system=SYSTEM, schema=ReplyBatch,
            context={"event": ws.event, "replies": [
                {"brand_id": b, "brand": ws.brands[b].name if b in ws.brands else b, "reply": r["text"],
                 "current_prices": {t.name: t.price_inr for t in ws.proposals[b].tiers} if b in ws.proposals else None}
                for b, r in ws.replies.items()],
                "labels": list(ReplyLabel.__args__)},
        )
        for c in batch.replies:
            if c.brand_id in ws.replies:
                ws.classes[c.brand_id] = c
                thread = ws.thread(c.brand_id)
                thread.last_reply_class = c.label
                thread.history.append({"type": "classified", **c.model_dump(), "ts": now_iso()})
        return {"classified": {c.brand_id: {"label": c.label, "summary": c.summary, "constraint": c.constraint,
                                            "max_price_inr": c.max_price_inr, "accepted_tier": c.accepted_tier}
                               for c in batch.replies}}

    async def route_pushback_tool(brand_id: str, constraint: str = "", max_price_inr: int | None = None) -> dict:
        return await route_pushback(deps, ws, brand_id, constraint, max_price_inr)

    async def request_replacement(brand_id: str, why: str) -> dict:
        await replace_brand(deps, ws, brand_id, why)
        return {"requested": True}

    async def mark_accepted_tool(brand_id: str, tier: str = "") -> dict:
        return await mark_accepted(deps, ws, brand_id, tier)

    async def draft_nudge(brand_id: str, body: str) -> dict:
        if brand_id not in ws.silent_threads(s.nudge_after_days):
            raise ToolInputError(f"{brand_id} is not a silent thread past {s.nudge_after_days} days")
        sent = last_entry(ws.thread(brand_id), "sent") or {}
        ws.thread(brand_id).history.append({"type": "email_draft", "kind": "nudge", "subject": "Re: " + sent.get(
            "subject", "Sponsorship proposal"), "body": body, "version": ws.proposals[brand_id].version,
            "state": "pending", "ts": now_iso()})
        return {"nudge_drafted": brand_id}

    ids = {"type": "array", "items": {"type": "string"}}
    brand = {"brand_id": {"type": "string"}}
    tools = []
    if ws.drafts_to_review():
        tools += [
            AgentTool(ToolSpec(name="critique_proposals", description="Critique draft proposals and write emails.",
                               parameters={"type": "object", "properties": {"brand_ids": ids}, "required": ["brand_ids"]}),
                      critique_proposals, timeout=150),
            AgentTool(ToolSpec(name="request_revision", description="Ask the Proposal agent to revise (max 1 per brand).",
                               parameters={"type": "object", "properties": {
                                   **brand, "constraint": {"type": "string"}, "max_price_inr": {"type": "integer"}},
                                   "required": ["brand_id", "constraint"]}), request_revision),
            AgentTool(ToolSpec(name="prepare_emails", description="Queue critiqued emails for the organizer's approval.",
                               parameters={"type": "object", "properties": {"brand_ids": ids}, "required": ["brand_ids"]}),
                      prepare_emails),
        ]
    if ws.send_approved():
        tools.append(AgentTool(ToolSpec(name="send_approved", description="Send the emails the organizer approved."),
                               send_approved, timeout=120))
    if polling:
        tools += [
            AgentTool(ToolSpec(name="poll_replies", description="Fetch new replies for all live threads."),
                      poll_replies, timeout=90),
            AgentTool(ToolSpec(name="classify_replies", description="Classify every new reply with the LLM."),
                      classify_replies, timeout=120),
            AgentTool(ToolSpec(name="route_pushback", description="Send a brand's pushback to the Proposal agent.",
                               parameters={"type": "object", "properties": {
                                   **brand, "constraint": {"type": "string"}, "max_price_inr": {"type": "integer"}},
                                   "required": ["brand_id"]}), route_pushback_tool),
            AgentTool(ToolSpec(name="request_replacement", description="Ask Discovery for a replacement brand.",
                               parameters={"type": "object", "properties": {**brand, "why": {"type": "string"}},
                                           "required": ["brand_id", "why"]}), request_replacement),
            AgentTool(ToolSpec(name="mark_accepted", description="Record that a brand accepted a tier.",
                               parameters={"type": "object", "properties": {**brand, "tier": {"type": "string"}},
                                           "required": ["brand_id"]}), mark_accepted_tool),
            AgentTool(ToolSpec(name="draft_nudge", description="Draft a polite follow-up for a silent thread.",
                               parameters={"type": "object", "properties": {**brand, "body": {"type": "string"}},
                                           "required": ["brand_id", "body"]}), draft_nudge),
        ]
    return tools


async def record_deal(deps, ws: Workspace, brand_id: str, tier: str) -> dict | None:
    """Remember an accepted deal (latest proposal version, so the price is after any counter-offer).

    The supervisor's A2 arbitration compares future prices with these. A failed write is recorded as an
    error and never blocks the acceptance itself.
    """
    brand, proposal = ws.brands.get(brand_id), ws.proposals.get(brand_id)
    price = next((t.price_inr for t in proposal.tiers if t.name == tier), None) if proposal else None
    if brand is None or price is None:
        return None
    try:
        owner = await deps.owner_id()
        if owner is None:
            return None
        return await deps.db.add_deal(owner, {"event_id": ws.state.get("event_id"), "brand_id": brand_id,
                                              "category": brand.category, "tier": tier, "price_inr": price})
    except Exception as exc:
        ws.errors.append({"agent": AGENT, "tool": "db.add_deal", "error": str(exc), "brand_id": brand_id})
        return None


async def _send_all(deps, ws: Workspace) -> dict:
    ws.send_attempted = True
    order = list(ws.state.get("selected_brand_ids", []))
    sent, failed = [], []
    for brand_id, thread in ws.threads.items():
        for entry in thread.history:
            if entry.get("type") != "email_draft" or entry.get("state") != "approved":
                continue
            brand, proposal = ws.brands.get(brand_id), ws.proposals.get(brand_id)
            try:
                if deps.gmail is None:
                    raise ToolError("Gmail tool not available")
                to, redirected = deps.gmail.recipient_for(brand.contact_email if brand else None,
                                                          order.index(brand_id) if brand_id in order else 0)
                subject = demo_subject(brand.name, entry["subject"]) if redirected else entry["subject"]
                attachment = None
                if entry["kind"] != "nudge" and proposal and brand:
                    attachment = (f"{brand.name}-proposal-v{proposal.version}.pdf",
                                  render_proposal_pdf(ws.event, brand, proposal))
                body = entry["body"] + (f"\n\nProposal: {proposal.pdf_url}" if proposal and proposal.pdf_url else "")
                reply_to = (last_entry(thread, "reply") or {}).get("message_id_header")
                result = await deps.gmail.send(to, subject, body, attachment, thread.thread_id, reply_to)
            except Exception as exc:  # keep it as a draft, tell the organizer, continue with the others
                entry["state"] = "failed"
                entry["error"] = str(exc)
                failed.append(brand_id)
                ws.errors.append({"agent": AGENT, "tool": "gmail.send", "error": str(exc), "brand_id": brand_id})
                continue
            entry["state"] = "sent"
            thread.thread_id = result["thread_id"]
            thread.status = "nudged" if entry["kind"] == "nudge" else "sent"
            thread.history.append({"type": "sent", "kind": entry["kind"], "gmail_message_id": result["message_id"],
                                   "to": to, "redirected_to_demo": redirected, "subject": subject, "ts": now_iso()})
            if proposal and entry["kind"] != "nudge":
                proposal.status = "sent"
            sent.append(brand_id)
    ws.sent += sent
    if failed:
        ws.messages.append(await emit(deps, AGENT, "human", "escalation", {"failed_brand_ids": failed,
                                      "errors": [e["error"] for e in ws.errors[-len(failed):]]},
                                      "Some emails could not be sent; they are kept as drafts. Check Gmail settings."))
    return {"sent": sent, "failed": failed}


async def handle_unrouted_replies(deps, ws: Workspace) -> list[str]:
    """Safety net for L3/L4/L6: finish every classified reply the LLM did not act on.

    The LLM picks one tool per step within a budget of 6 calls, so a busy inbox (poll + classify + one action
    per reply) can run out before every brand is handled. A reply that is never acted on leaves the thread
    "replied": the accept is not recorded, pushback never reaches Proposal, a decline never reaches Discovery
    and the run sits on "Waiting for replies" forever because another poll finds nothing new.

    This reads the classification the reply loop stored in thread history (``classification_for``), so it also
    repairs runs that got stuck before a deploy: clicking Continue (or Check for replies) is enough to finish
    them. Like the approved-send fallback in ``run``, it completes the step in code. Returns what it handled.
    """
    handled: list[str] = []
    for brand_id, thread in ws.threads.items():
        cls = classification_for(ws, brand_id)
        label = (cls or {}).get("label")
        if thread.status == "replied" and label == "accepted" \
                and not any(e.get("type") == "accepted" for e in thread.history):
            await mark_accepted(deps, ws, brand_id, cls=cls)
            handled.append(f"{brand_id} (accepted)")
        elif thread.status == "replied" and label in PUSHBACK_LABELS:
            await route_pushback(deps, ws, brand_id, cls=cls)
            handled.append(f"{brand_id} (pushback -> proposal)")
        elif thread.status == "replied" and label == "declined":
            await replace_brand(deps, ws, brand_id, (cls or {}).get("summary") or "declined", fallback=True)
            handled.append(f"{brand_id} (declined -> replacement)")
        elif thread.status == "bounced" and "bounced" not in ws.feedback.get(brand_id, []) \
                and any(e.get("type") == "reply" and e.get("bounce") for e in thread.history):
            await replace_brand(deps, ws, brand_id, "email bounced", fallback=True)
            handled.append(f"{brand_id} (bounced -> replacement)")
    return handled


async def replace_brand(deps, ws: Workspace, brand_id: str, why: str, fallback: bool = False) -> None:
    """L4: record the decline or bounce for this brand and ask Discovery for a replacement."""
    thread = ws.thread(brand_id)
    if thread.status != "bounced":
        thread.status = "declined"
    ws.record_outcome(brand_id, thread.status)
    if brand_id in ws.proposals:
        ws.proposals[brand_id].status = "declined"
    content = {"reason": "replacement", "brand_id": brand_id, "why": why}
    reasoning = why
    if fallback:
        content["source"] = "outreach_fallback"
        reasoning = f"{why} (handled in code: the reasoning loop ended before acting on it)"
    ws.messages.append(await emit(deps, AGENT, "discovery", "request", content, reasoning))


def category_checks_so_far(state: EventState) -> dict[str, int]:
    """Categories Clef already said "keep" for, with the negative-outcome count at that time (L6)."""
    checks: dict[str, int] = {}
    for m in map(as_message, state.get("messages", [])):
        if m.from_agent == AGENT:
            for category, count in (m.content.get("category_checks") or {}).items():
                checks[category] = max(count, checks.get(category, 0))
    return checks


async def category_feedback(deps, ws: Workspace) -> None:
    """L6: when a category reaches ``CATEGORY_NEGATIVE_LIMIT`` negative outcomes, ask Clef whether to stop
    prioritising it, and tell Discovery only if the answer is yes (once per category).

    The counter only decides when to ask. A "no" is remembered in this step's result message
    (``content.category_checks``) and asked again only after a new negative outcome in that category.
    """
    told = {as_message(m).content.get("category") for m in ws.state.get("messages", [])
            if as_message(m).content.get("reason") == "category_feedback" and as_message(m).from_agent == AGENT}
    checked = category_checks_so_far(ws.state)
    by_category: dict[str, list[dict]] = {}
    positive: dict[str, list[dict]] = {}
    negatives: dict[str, int] = {}
    for brand_id, outcomes in ws.feedback.items():
        brand = ws.brands.get(brand_id)
        if brand is None:
            continue
        bad = [o for o in outcomes if o in NEGATIVE_OUTCOMES]
        if bad:
            by_category.setdefault(brand.category, []).append({"brand_id": brand_id, "name": brand.name,
                                                               "outcome": bad[-1]})
            negatives[brand.category] = negatives.get(brand.category, 0) + len(bad)
        if "accepted" in outcomes:
            positive.setdefault(brand.category, []).append({"brand": brand.name, "outcome": "accepted"})
    for category, evidence in by_category.items():
        if len(evidence) < CATEGORY_NEGATIVE_LIMIT or category in told:
            continue
        if negatives[category] <= checked.get(category, 0):
            continue  # Clef already said keep it, and nothing new went wrong since
        j = await judge(deps, agent=AGENT, purpose=CATEGORY_PURPOSE, question=CATEGORY_QUESTION, rule_answer=True,
                        state={"category": category,
                               "evidence": [{"brand": e["name"], "outcome": e["outcome"]} for e in evidence],
                               "positive_outcomes": positive.get(category, []),
                               "event_audience": ws.event.get("audience")})
        if j.escalate:
            ws.messages.append(await unsure_escalation(
                deps, AGENT, CATEGORY_PURPOSE, j, f"whether to stop prioritising {category} brands",
                {"category": category}))
        if not j.answer:
            ws.category_checks[category] = negatives[category]
            continue
        ws.messages.append(await emit(deps, AGENT, "discovery", "request", {
            "reason": "category_feedback", "category": category, "negative": len(evidence), "evidence": evidence,
            "p_yes": j.p_yes, "decided_by": j.source},
            f"{len(evidence)} {category} brands said no or never answered "
            f"({', '.join(e['name'] + ': ' + e['outcome'] for e in evidence)}); stop prioritising {category}."))


def next_phase(ws: Workspace) -> str:
    if any(e.get("type") == "email_draft" and e.get("state") == "pending" for t in ws.threads.values() for e in t.history):
        return "send_review"
    if any(p.status in ("draft", "needs_revision") for p in ws.proposals.values()):
        return "proposals" if any(p.status == "needs_revision" for p in ws.proposals.values()) else "review"
    if any(m.to_agent == "discovery" for m in ws.messages):
        return "replacement"
    if any(t.status in WAITING_STATUSES for t in ws.threads.values()):
        return "awaiting_replies"
    return "outreach_idle"


async def run(state: EventState, deps) -> dict:
    ws = Workspace(state)
    requests = open_requests(state, AGENT)
    polling = state.get("phase") == "replies" or any(r.content.get("action") == "poll_replies" for r in requests)
    tools = build_tools(deps, ws, polling)
    context = {
        "event": ws.event,
        "drafts_to_review": ws.drafts_to_review(),
        "send_gate_approved": ws.send_approved(),
        "polling_requested": polling,
        "threads": {b: {"status": t.status, "last_reply_class": t.last_reply_class} for b, t in ws.threads.items()},
        "requests": [r.content for r in requests],
    }
    errors = list(state.get("errors", []))
    summary = "Nothing for outreach to do right now."
    if tools:
        result = await run_tool_loop(deps, AGENT, "outreach.plan", SYSTEM, context, tools)
        errors += result.errors
        summary = result.summary
        if ws.send_approved() and not ws.send_attempted:
            # The organizer already approved these emails: send them even if the LLM skipped the step or is down.
            await _send_all(deps, ws)
            summary = f"{summary} Sent the organizer-approved emails.".strip()
    # Run even with no tools: this is what repairs a run whose classified replies the LLM never acted on.
    handled = await handle_unrouted_replies(deps, ws)
    if handled:
        summary = f"{summary} Finished replies the loop did not reach: {', '.join(handled)}.".strip()
    await category_feedback(deps, ws)
    errors += ws.errors

    content = {"sent": ws.sent, "threads": {b: t.status for b, t in ws.threads.items()},
               "in_reply_to": [r.id for r in requests]}
    if ws.category_checks:
        content["category_checks"] = ws.category_checks
    messages = [*ws.messages, await emit(deps, AGENT, "supervisor", "result", content, summary or "Outreach step done.")]
    update = {"outreach": ws.threads, "proposals": ws.proposals, "messages": messages, "errors": errors,
              "phase": next_phase(ws), "feedback": ws.feedback}
    if ws.send_approved():
        update["pending_approval"] = None  # the approval is consumed by this send
    return update

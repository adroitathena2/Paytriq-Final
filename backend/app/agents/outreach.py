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
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from app.agents.loop import AgentTool, run_tool_loop
from app.core.llm import ToolSpec
from app.graph.messages import as_message, emit, open_requests
from app.graph.state import EventState, OutreachThread
from app.tools.base import ToolError, ToolInputError
from app.tools.gmail import demo_subject
from app.tools.pdf import render_proposal_pdf

AGENT = "outreach"
LIVE_STATUSES = {"sent", "nudged", "replied", "pushback"}
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

    async def route_pushback(brand_id: str, constraint: str = "", max_price_inr: int | None = None) -> dict:
        cls = ws.classes.get(brand_id)
        if brand_id not in ws.proposals:
            raise ToolInputError(f"no proposal for {brand_id}")
        content = {"brand_id": brand_id, "constraint": constraint or (cls.constraint if cls else "") or "",
                   "source": "brand_reply", "reply_label": cls.label if cls else None,
                   "from_version": ws.proposals[brand_id].version}
        cap = max_price_inr or (cls.max_price_inr if cls else None)
        if cap:
            content["max_price_inr"] = int(cap)
        reasoning = cls.summary if cls else constraint
        ws.messages.append(await emit(deps, AGENT, "proposal", "revision_request", content, reasoning))
        ws.thread(brand_id).status = "pushback"
        ws.proposals[brand_id].status = "needs_revision"
        return {"routed_to": "proposal", "constraint": content["constraint"]}

    async def request_replacement(brand_id: str, why: str) -> dict:
        thread = ws.thread(brand_id)
        if thread.status != "bounced":
            thread.status = "declined"
        if brand_id in ws.proposals:
            ws.proposals[brand_id].status = "declined"
        ws.messages.append(await emit(deps, AGENT, "discovery", "request",
                                      {"reason": "replacement", "brand_id": brand_id, "why": why}, why))
        return {"requested": True}

    async def mark_accepted(brand_id: str, tier: str = "") -> dict:
        cls = ws.classes.get(brand_id)
        tier = tier or (cls.accepted_tier if cls else None) or "Silver"
        if tier not in ("Bronze", "Silver", "Gold"):
            raise ToolInputError("tier must be Bronze, Silver or Gold")
        thread = ws.thread(brand_id)
        thread.status = "accepted"
        thread.history.append({"type": "accepted", "tier": tier, "ts": now_iso()})
        if brand_id in ws.proposals:
            ws.proposals[brand_id].status = "accepted"
        return {"accepted": brand_id, "tier": tier}

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
                                   "required": ["brand_id"]}), route_pushback),
            AgentTool(ToolSpec(name="request_replacement", description="Ask Discovery for a replacement brand.",
                               parameters={"type": "object", "properties": {**brand, "why": {"type": "string"}},
                                           "required": ["brand_id", "why"]}), request_replacement),
            AgentTool(ToolSpec(name="mark_accepted", description="Record that a brand accepted a tier.",
                               parameters={"type": "object", "properties": {**brand, "tier": {"type": "string"}},
                                           "required": ["brand_id"]}), mark_accepted),
            AgentTool(ToolSpec(name="draft_nudge", description="Draft a polite follow-up for a silent thread.",
                               parameters={"type": "object", "properties": {**brand, "body": {"type": "string"}},
                                           "required": ["brand_id", "body"]}), draft_nudge),
        ]
    return tools


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


def next_phase(ws: Workspace) -> str:
    if any(e.get("type") == "email_draft" and e.get("state") == "pending" for t in ws.threads.values() for e in t.history):
        return "send_review"
    if any(p.status in ("draft", "needs_revision") for p in ws.proposals.values()):
        return "proposals" if any(p.status == "needs_revision" for p in ws.proposals.values()) else "review"
    if any(m.to_agent == "discovery" for m in ws.messages):
        return "replacement"
    if any(t.status in LIVE_STATUSES for t in ws.threads.values()):
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
    if tools:
        result = await run_tool_loop(deps, AGENT, "outreach.plan", SYSTEM, context, tools)
        errors += result.errors
        summary = result.summary
        if ws.send_approved() and not ws.send_attempted:
            # The organizer already approved these emails: send them even if the LLM skipped the step or is down.
            await _send_all(deps, ws)
            summary = f"{summary} Sent the organizer-approved emails.".strip()
    else:
        summary = "Nothing for outreach to do right now."
    errors += ws.errors

    content = {"sent": ws.sent, "threads": {b: t.status for b, t in ws.threads.items()},
               "in_reply_to": [r.id for r in requests]}
    messages = [*ws.messages, await emit(deps, AGENT, "supervisor", "result", content, summary or "Outreach step done.")]
    update = {"outreach": ws.threads, "proposals": ws.proposals, "messages": messages, "errors": errors,
              "phase": next_phase(ws)}
    if ws.send_approved():
        update["pending_approval"] = None  # the approval is consumed by this send
    return update

"""LLM-driven supervisor: decides which agent acts next.

The LLM sees a compact snapshot of the blackboard (phase, open inter-agent requests, recent messages,
errors, pending approval) and returns ``{"next", "gate", "reason"}``. Hard guardrails in code then
check the choice:

- at most ``MAX_STEPS`` supervisor steps between two human/external touchpoints, then escalate;
- an approval gate can only be opened when there is something to approve;
- the same ordered pair of agents may hand off at most ``MAX_PAIR_LOOPS`` times, then escalate.

(Emails are never sent without an approved ``pending_approval``: that check lives in the Outreach
agent's send tool, where the email actually leaves the system.)

If the LLM is unavailable, ``fallback_route`` picks a sensible step from the phase so the run degrades
instead of crashing; the trace records that the fallback was used.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel

from app.core.llm import LLMError
from app.graph.messages import as_message, emit, open_requests, recent
from app.graph.state import EventState

MAX_STEPS = 25
MAX_PAIR_LOOPS = 3
AGENTS = ("discovery", "proposal", "outreach", "roi_audit")
Next = Literal["discovery", "proposal", "outreach", "roi_audit", "human_approval", "END"]
Gate = Literal["shortlist", "send", "roi_publish"]


class RouteDecision(BaseModel):
    next: Next
    gate: Gate | None = None
    reason: str


SYSTEM = """You are the supervisor of Paytriq, a multi-agent system that finds and closes local sponsors
for Indian college events and proves ROI afterwards. You never do the work yourself; you decide which
agent acts next, based on the shared blackboard.

Agents:
- discovery: finds and ranks nearby businesses; also finds a replacement brand when one declines/bounces,
  and replans when the organizer rejects the shortlist.
- proposal: writes brand-specific pitches and Bronze/Silver/Gold tiers (PDF); handles revision_requests.
- outreach: critiques proposals before sending (may request a revision), sends approved emails,
  polls and classifies replies, routes pushback and declines.
- roi_audit: after the event, verifies promised deliverables against uploaded evidence.
- human_approval: pauses for the organizer. Gates: "shortlist" (approve brands before proposals),
  "send" (approve emails before anything is sent), "roi_publish" (approve the ROI report).
- END: stop and wait for the organizer or for external events (replies, evidence).

Rules:
- Open requests addressed to an agent are work for that agent; prefer resolving them.
- Never route to outreach to send emails unless pending_approval is an approved "send" gate.
- When roi_audit reported gaps, arbitrate: either "human_approval" with gate "roi_publish" (publish with
  the gaps disclosed) or "END" (wait for the organizer to upload the missing evidence). Weigh how much was
  verified, how critical the missing items are, and what the escalation asked for. Explain the trade-off.
- Keep the reason to one or two sentences that a teammate could check against the blackboard."""


def snapshot(state: EventState) -> dict:
    """Compact, LLM-friendly view of the state."""
    proposals = state.get("proposals", {})
    threads = state.get("outreach", {})
    report = state.get("roi_report") or {}
    pending = state.get("pending_approval")
    return {
        "phase": state.get("phase"),
        "step_count": state.get("step_count", 0),
        "max_steps": MAX_STEPS,
        "event": {k: state.get("event", {}).get(k) for k in ("name", "date", "expected_footfall")},
        "counts": {
            "brands_found": len(state.get("brands", [])),
            "good_fit_brands": sum(1 for b in state.get("brands", []) if b.fit_score >= 0.6),
            "selected_brands": len(state.get("selected_brand_ids", [])),
            "proposals_by_status": dict(Counter(p.status for p in proposals.values())),
            "threads_by_status": dict(Counter(t.status for t in threads.values())),
            "emails_waiting_for_approval": len(pending_emails(state)),
            "evidence_items": len(state.get("evidence", [])),
        },
        "open_requests": {a: [m.model_dump(mode="json") for m in open_requests(state, a)] for a in AGENTS},
        "open_escalations_to_human": [m.content for m in open_requests(state, "human")],
        "pending_approval": {"gate": pending.get("gate"), "status": pending.get("status")} if pending else None,
        "roi": {"gaps": len(report.get("disclosed_gaps", [])), "delivery_rate": report.get("overall_delivery_rate"),
                "published": report.get("published", False)} if report else None,
        "recent_messages": recent(state, 8),
        "recent_errors": state.get("errors", [])[-5:],
    }


def pending_emails(state: EventState) -> list[dict]:
    """Email drafts that are ready and wait for the organizer's send approval."""
    out = []
    for brand_id, thread in state.get("outreach", {}).items():
        for entry in thread.history:
            if entry.get("type") == "email_draft" and entry.get("state") == "pending":
                out.append({"brand_id": brand_id, **entry})
    return out


def fallback_route(state: EventState) -> RouteDecision:
    """Deterministic routing used only when the LLM cannot be reached."""
    for agent in ("discovery", "proposal", "outreach", "roi_audit"):
        if open_requests(state, agent):
            return RouteDecision(next=agent, reason=f"Fallback: {agent} has open requests.")
    phase = state.get("phase", "")
    table: dict[str, tuple[str, str | None]] = {
        "discovery": ("discovery", None),
        "shortlist_review": ("human_approval", "shortlist"),
        "proposals": ("proposal", None),
        "review": ("outreach", None),
        "send_review": ("human_approval", "send"),
        "sending": ("outreach", None),
        "replies": ("outreach", None),
        "roi": ("roi_audit", None),
        "roi_review": ("human_approval", "roi_publish"),
    }
    nxt, gate = table.get(phase, ("END", None))
    return RouteDecision(next=nxt, gate=gate, reason=f"Fallback: phase '{phase}' maps to {nxt}.")


def _decisions_since_human(state: EventState) -> list[str]:
    seq: list[str] = []
    for m in map(as_message, state.get("messages", [])):
        if m.from_agent == "human":
            seq = []
        elif m.from_agent == "supervisor" and m.type == "decision" and "next" in m.content:
            seq.append(m.content["next"])
    return seq


def pair_loop_count(state: EventState, nxt: str) -> int:
    """How many times the hand-off (previous agent -> nxt) already happened since the last human input."""
    seq = _decisions_since_human(state)
    if not seq:
        return 0
    pairs = Counter(zip(seq, seq[1:], strict=False))
    return pairs[(seq[-1], nxt)]


def gate_has_content(state: EventState, gate: str | None) -> bool:
    if gate == "shortlist":
        return bool(state.get("brands"))
    if gate == "send":
        return bool(pending_emails(state))
    if gate == "roi_publish":
        return bool(state.get("roi_report"))
    return False


def apply_guardrails(state: EventState, decision: RouteDecision) -> tuple[RouteDecision, str | None]:
    """Return the (possibly overridden) decision and the name of the guardrail that fired, if any."""
    step = state.get("step_count", 0) + 1
    if step > MAX_STEPS:
        return RouteDecision(next="END", reason=f"Step budget of {MAX_STEPS} used up; asking the organizer."), "max_steps"
    if decision.next == "human_approval" and not gate_has_content(state, decision.gate):
        fallback = fallback_route(state)
        if fallback.next == "human_approval" and not gate_has_content(state, fallback.gate):
            fallback = RouteDecision(next="END", reason="Nothing to approve and no work left.")
        return fallback, "empty_gate"
    if decision.next in AGENTS and pair_loop_count(state, decision.next) >= MAX_PAIR_LOOPS:
        return RouteDecision(
            next="END", reason=f"Hand-off to {decision.next} repeated {MAX_PAIR_LOOPS} times; escalating to the organizer."
        ), "pair_loop"
    return decision, None


def gate_payload(state: EventState, gate: str) -> dict:
    brands = {b.id: b for b in state.get("brands", [])}
    if gate == "shortlist":
        ranked = sorted(state.get("brands", []), key=lambda b: b.fit_score, reverse=True)
        suggested = [b.id for b in ranked if b.fit_score >= 0.6][:5] or [b.id for b in ranked[:3]]
        return {"suggested_brand_ids": suggested, "brands": [b.model_dump() for b in ranked]}
    if gate == "send":
        emails = []
        for e in pending_emails(state):
            brand = brands.get(e["brand_id"])
            proposal = state.get("proposals", {}).get(e["brand_id"])
            emails.append({**e, "brand_name": brand.name if brand else e["brand_id"],
                           "pdf_url": proposal.pdf_url if proposal else None})
        return {"emails": emails}
    return {"report": state.get("roi_report")}


END_PHASES = {"roi_review": "awaiting_evidence"}


async def run(state: EventState, deps) -> Command:
    try:
        decision = await deps.llm.generate(
            purpose="supervisor.route", system=SYSTEM, context=snapshot(state), schema=RouteDecision
        )
        source = "llm"
    except LLMError as exc:
        await deps.log("supervisor", "llm_error", {"purpose": "supervisor.route", "error": str(exc)})
        decision, source = fallback_route(state), "fallback"

    final, guardrail = apply_guardrails(state, decision)
    if guardrail:
        await deps.log("supervisor", "guardrail", {"rule": guardrail, "proposed": decision.model_dump(),
                                                   "applied": final.model_dump()})
    messages = [await emit(
        deps, "supervisor", final.next, "decision",
        {"next": final.next, "gate": final.gate, "source": source, "guardrail": guardrail}, final.reason,
    )]
    if guardrail in ("max_steps", "pair_loop"):
        messages.append(await emit(deps, "supervisor", "human", "escalation",
                                   {"reason": guardrail, "phase": state.get("phase")}, final.reason))

    update: dict = {"messages": messages, "step_count": state.get("step_count", 0) + 1}
    if final.next == "human_approval":
        update["pending_approval"] = {"gate": final.gate, "status": "waiting", "payload": gate_payload(state, final.gate)}
        return Command(goto="human_approval", update=update)
    if final.next == "END":
        update["phase"] = END_PHASES.get(state.get("phase", ""), state.get("phase", ""))
        return Command(goto=END, update=update)
    return Command(goto=final.next, update=update)

"""Human-in-the-loop gate node.

The supervisor routes here with ``pending_approval = {"gate", "status": "waiting", "payload"}``.
``interrupt()`` pauses the graph (state is checkpointed) until ``POST /runs/{id}/approve`` resumes it
with ``{"gate", "decision": "approve" | "reject", "edits": {...}}``. The organizer's decision is posted
on the message bus as a message from ``human``; rejections become work for the right agent.

Note: LangGraph re-runs this node from the top on resume, so nothing before ``interrupt()`` has side effects.
"""

from __future__ import annotations

from langgraph.types import interrupt

from app.agents import roi_audit
from app.graph.messages import emit, open_requests
from app.graph.state import EventState


async def run(state: EventState, deps) -> dict:
    pending = state.get("pending_approval") or {}
    gate = pending.get("gate")
    answer = interrupt({"gate": gate, "payload": pending.get("payload", {})})

    decision = answer.get("decision", "reject")
    edits = answer.get("edits") or {}
    await deps.log("human", "resume", {"gate": gate, "decision": decision, "edits": edits})
    escalations = [m.id for m in open_requests(state, "human")]

    if gate == "shortlist":
        update = await _shortlist(state, deps, decision, edits)
    elif gate == "send":
        update = await _send(state, deps, decision, edits)
    else:
        update = await _roi(state, deps, decision, edits)

    update["messages"].insert(0, await emit(
        deps, "human", "supervisor", "decision",
        {"gate": gate, "decision": decision, "edits": edits, "in_reply_to": escalations},
        f"Organizer chose to {decision} the {gate} gate.",
    ))
    update["step_count"] = 0  # a human touchpoint starts a fresh step budget
    return update


async def _shortlist(state: EventState, deps, decision: str, edits: dict) -> dict:
    payload = state["pending_approval"].get("payload", {})
    if decision == "approve":
        known = {b.id for b in state.get("brands", [])}
        chosen = [b for b in edits.get("selected_brand_ids") or payload.get("suggested_brand_ids", []) if b in known]
        return {"selected_brand_ids": chosen, "target_brand_count": len(chosen), "phase": "proposals",
                "pending_approval": None, "messages": []}
    feedback = edits.get("feedback") or "Organizer rejected the shortlist without a comment."
    msg = await emit(deps, "human", "discovery", "request", {"reason": "human_feedback", "feedback": feedback},
                     "Organizer wants a different shortlist.")
    return {"phase": "discovery", "pending_approval": None, "messages": [msg]}


async def _send(state: EventState, deps, decision: str, edits: dict) -> dict:
    exclude = set(edits.get("exclude") or [])
    email_edits = edits.get("emails") or {}
    feedback = edits.get("feedback") or {}
    outreach = {k: v.model_copy(deep=True) for k, v in state.get("outreach", {}).items()}
    messages = []
    for brand_id, thread in outreach.items():
        for entry in thread.history:
            if entry.get("type") != "email_draft" or entry.get("state") != "pending":
                continue
            if decision == "approve" and brand_id not in exclude:
                entry.update({k: v for k, v in email_edits.get(brand_id, {}).items() if k in ("subject", "body")})
                entry["state"] = "approved"
            elif decision == "approve":
                entry["state"] = "skipped"
            else:
                entry["state"] = "discarded"
    if decision == "approve":
        return {"outreach": outreach, "phase": "sending", "messages": messages,
                "pending_approval": {"gate": "send", "status": "approved"}}

    proposals = {k: v.model_copy() for k, v in state.get("proposals", {}).items()}
    if isinstance(feedback, str):
        feedback = dict.fromkeys(state.get("selected_brand_ids", []), feedback) if feedback else {}
    for brand_id, text in feedback.items():
        if brand_id in proposals:
            proposals[brand_id].status = "needs_revision"
            messages.append(await emit(deps, "human", "proposal", "revision_request",
                                       {"brand_id": brand_id, "constraint": text, "source": "organizer"},
                                       "Organizer asked for changes before sending."))
    return {"outreach": outreach, "proposals": proposals, "pending_approval": None, "messages": messages,
            "phase": "proposals" if messages else "outreach_paused"}


async def _roi(state: EventState, deps, decision: str, edits: dict) -> dict:
    report = dict(state.get("roi_report") or {})
    if decision == "approve":
        report.update(published=True, organizer_note=edits.get("note"),
                      published_with_gaps=bool(report.get("disclosed_gaps")))
        # L8: only an approved report calibrates future reach estimates
        messages = await roi_audit.commit_calibration(deps, report)
        return {"roi_report": report, "phase": "done", "pending_approval": None, "messages": messages}
    return {"phase": "awaiting_evidence", "pending_approval": None, "messages": []}

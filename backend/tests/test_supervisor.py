from langgraph.graph import END

from app.core.llm import FakeLLM, LLMError
from app.graph import supervisor
from app.graph.messages import new_message
from app.graph.supervisor import MAX_STEPS, RouteDecision, apply_guardrails, fallback_route
from tests.factories import brand, proposal, state, thread


def decision_msgs(*targets):
    return [new_message("supervisor", t, "decision", {"next": t}, "r") for t in targets]


def test_fallback_prefers_open_requests_over_phase():
    s = state(phase="awaiting_replies",
              messages=[new_message("outreach", "discovery", "request", {"reason": "replacement"}, "declined")])
    assert fallback_route(s).next == "discovery"


def test_fallback_maps_phase_to_gate():
    d = fallback_route(state(phase="shortlist_review"))
    assert (d.next, d.gate) == ("human_approval", "shortlist")


def test_step_budget_forces_end():
    s = state(step_count=MAX_STEPS)
    final, rule = apply_guardrails(s, RouteDecision(next="proposal", reason="more work"))
    assert (final.next, rule) == ("END", "max_steps")


def test_gate_without_content_is_overridden():
    s = state(phase="proposals")  # no email drafts waiting
    final, rule = apply_guardrails(s, RouteDecision(next="human_approval", gate="send", reason="send now"))
    assert rule == "empty_gate"
    assert final.next == "proposal"


def test_same_pair_loop_is_capped():
    s = state(messages=decision_msgs("proposal", "outreach", "proposal", "outreach", "proposal", "outreach",
                                     "proposal"))
    final, rule = apply_guardrails(s, RouteDecision(next="outreach", reason="again"))
    assert (final.next, rule) == ("END", "pair_loop")


def test_human_input_resets_pair_loop_counter():
    msgs = decision_msgs("proposal", "outreach", "proposal", "outreach", "proposal", "outreach", "proposal")
    msgs.append(new_message("human", "supervisor", "decision", {"gate": "send"}, "approved"))
    msgs += decision_msgs("proposal")
    final, rule = apply_guardrails(state(messages=msgs), RouteDecision(next="outreach", reason="ok"))
    assert rule is None and final.next == "outreach"


async def test_llm_decision_opens_gate_with_payload(make_deps):
    llm = FakeLLM(generate={"supervisor.route": {"next": "human_approval", "gate": "shortlist", "reason": "review"}})
    s = state(phase="shortlist_review", brands=[brand(1, 0.9), brand(2, 0.3)])
    cmd = await supervisor.run(s, make_deps(llm))
    assert cmd.goto == "human_approval"
    assert cmd.update["pending_approval"]["payload"]["suggested_brand_ids"] == ["b1"]
    msg = cmd.update["messages"][0]
    assert (msg.type, msg.content["source"]) == ("decision", "llm")


async def test_llm_failure_degrades_to_fallback_and_is_traced(make_deps, db):
    llm = FakeLLM(generate={"supervisor.route": LLMError("503")})
    s = state(phase="review", outreach={"b1": thread("b1")})
    cmd = await supervisor.run(s, make_deps(llm))
    assert cmd.goto == "outreach"
    assert cmd.update["messages"][0].content["source"] == "fallback"
    assert any(t["event_type"] == "llm_error" for t in db.trace)


def revision(from_agent: str, brand_id: str = "b1", source: str = "critique"):
    return new_message(from_agent, "proposal", "revision_request", {"brand_id": brand_id, "source": source}, "r")


async def test_every_step_appends_to_decision_log_and_trace(make_deps, db):
    llm = FakeLLM(generate={"supervisor.route": {"next": "human_approval", "gate": "send", "reason": "send now"}})
    s = state(phase="proposals", decision_log=[{"kind": "route", "next": "discovery"}])
    cmd = await supervisor.run(s, make_deps(llm))
    log = cmd.update["decision_log"]
    assert [e["kind"] for e in log] == ["route", "guardrail", "route"]  # earlier entry kept, guardrail + route added
    assert (log[1]["rule"], log[1]["proposed"], log[1]["source"]) == ("empty_gate", "human_approval", "llm")
    assert (log[2]["next"], log[2]["source"]) == ("proposal", "llm") and log[2]["ts"]
    assert [t["payload"]["kind"] for t in db.trace if t["event_type"] == "decision"] == ["guardrail", "route"]


async def test_third_agent_revision_is_held_and_escalated(make_deps):
    llm = FakeLLM(generate={"supervisor.route": {"next": "proposal", "reason": "revise"}})
    third = revision("outreach", source="brand_reply")
    s = state(phase="replies", brands=[brand(1)], proposals={"b1": proposal("b1", "needs_revision", 3)},
              messages=[revision("outreach"), revision("outreach", source="brand_reply"), third])
    cmd = await supervisor.run(s, make_deps(llm))
    escalation = next(m for m in cmd.update["messages"] if m.type == "escalation")
    assert (escalation.to_agent, escalation.content["reason"]) == ("human", "revision_cap")
    assert escalation.content["in_reply_to"] == [third.id]  # the request is closed, Proposal won't pick it up
    assert cmd.update["proposals"]["b1"].status == "on_hold"
    assert cmd.update["revision_count"] == {"b1": 2}
    assert any(e["rule"] == "revision_cap" for e in cmd.update["decision_log"])


async def test_organizer_revisions_bypass_the_cap(make_deps):
    llm = FakeLLM(generate={"supervisor.route": {"next": "proposal", "reason": "revise"}})
    s = state(phase="proposals", brands=[brand(1)], proposals={"b1": proposal("b1", "needs_revision", 3)},
              messages=[revision("outreach"), revision("outreach"), revision("human", source="organizer")])
    cmd = await supervisor.run(s, make_deps(llm))
    assert not any(m.type == "escalation" for m in cmd.update["messages"])
    assert "proposals" not in cmd.update and cmd.update["revision_count"] == {"b1": 2}
    assert cmd.goto == "proposal"


async def test_end_after_clean_roi_review_waits_for_evidence(make_deps):
    llm = FakeLLM(generate={"supervisor.route": {"next": "END", "reason": "organizer wants to add photos first"}})
    cmd = await supervisor.run(state(phase="roi_review", roi_report={"disclosed_gaps": [], "generated_at": "t"}),
                               make_deps(llm))
    assert cmd.goto == END
    assert cmd.update["phase"] == "awaiting_evidence"

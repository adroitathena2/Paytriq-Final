from langgraph.graph import END

from app.core.llm import FakeLLM, LLMError
from app.graph import supervisor
from app.graph.messages import new_message
from app.graph.supervisor import MAX_STEPS, RouteDecision, apply_guardrails, fallback_route
from tests.factories import brand, state, thread


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


async def test_end_while_waiting_for_evidence_sets_phase(make_deps):
    llm = FakeLLM(generate={"supervisor.route": {"next": "END", "reason": "two key photos missing; wait"}})
    cmd = await supervisor.run(state(phase="roi_review", roi_report={"disclosed_gaps": [1]}), make_deps(llm))
    assert cmd.goto == END
    assert cmd.update["phase"] == "awaiting_evidence"

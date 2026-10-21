"""Outreach agent: pre-send critique (L2), approval-gated sending, reply handling (L3, L4), category feedback
judged by Clef (L6) and nudges."""

from datetime import UTC, datetime, timedelta

import pytest

from app.agents import outreach
from app.core.clef import ClefError, FakeClef
from app.core.llm import FakeLLM, LLMError
from app.graph.messages import new_message
from tests.factories import brand, proposal, state, thread
from tests.fakes import FakeGmail, classify, critique, outreach_plan


def llm() -> FakeLLM:
    return FakeLLM(decide={"outreach.plan": outreach_plan},
                   generate={"outreach.critique": critique, "outreach.classify": classify})


def by_type(update, type_):
    return [m for m in update["messages"] if m.type == type_]


async def test_critique_requests_revision_for_overpriced_proposal(make_deps, settings):
    cheap = proposal("b2")
    cheap.tiers = [t.model_copy(update={"price_inr": p}) for t, p in zip(cheap.tiers, (3000, 6000, 12000), strict=True)]
    s = state(brands=[brand(1), brand(2)], selected_brand_ids=["b1", "b2"],
              proposals={"b1": proposal("b1"), "b2": cheap}, phase="review")
    update = await outreach.run(s, make_deps(llm(), gmail=FakeGmail(settings)))

    [revision] = by_type(update, "revision_request")
    assert (revision.to_agent, revision.content["brand_id"], revision.content["max_price_inr"]) == ("proposal", "b1", 20000)
    assert revision.reasoning == "Gold price is high for a small cafe"
    assert update["proposals"]["b1"].status == "needs_revision"
    assert update["proposals"]["b2"].status == "ready"
    draft = update["outreach"]["b2"].history[-1]
    assert (draft["type"], draft["state"], draft["kind"]) == ("email_draft", "pending", "initial")
    assert update["phase"] == "send_review"  # b2 waits for approval while b1 is revised


async def test_only_one_critique_revision_per_brand(make_deps, settings):
    earlier = new_message("outreach", "proposal", "revision_request", {"brand_id": "b1", "source": "critique"}, "r")
    s = state(brands=[brand(1)], proposals={"b1": proposal("b1", version=2)}, messages=[earlier], phase="review")
    update = await outreach.run(s, make_deps(llm(), gmail=FakeGmail(settings)))
    assert not by_type(update, "revision_request")
    assert update["proposals"]["b1"].status == "ready"


async def test_tools_never_offer_sending_without_approval(make_deps, settings):
    fake = llm()
    s = state(brands=[brand(1)], proposals={"b1": proposal("b1")}, phase="review")
    await outreach.run(s, make_deps(fake, gmail=FakeGmail(settings)))
    offered = {t for c in fake.calls if c["op"] == "decide" for t in c["tools"]}
    assert "send_approved" not in offered


def approved_state(**kw):
    t = thread("b1")
    t.history.append({"type": "email_draft", "kind": "initial", "subject": "Sponsor TechFest", "body": "Hi",
                      "version": 1, "state": "approved", "ts": datetime.now(UTC).isoformat()})
    return state(brands=[brand(1)], selected_brand_ids=["b1"], proposals={"b1": proposal("b1", status="ready")},
                 outreach={"b1": t}, pending_approval={"gate": "send", "status": "approved"}, phase="sending", **kw)


async def test_approved_email_is_sent_to_demo_inbox_with_pdf(make_deps, settings):
    gmail = FakeGmail(settings)
    update = await outreach.run(approved_state(), make_deps(llm(), gmail=gmail))
    [sent] = gmail.sent
    assert sent["to"] == "team1@example.com"  # DEMO_MODE redirect
    assert sent["subject"] == "[Paytriq demo -> Brand 1] Sponsor TechFest"
    assert sent["attachment"][1].startswith(b"%PDF")
    assert update["outreach"]["b1"].status == "sent" and update["outreach"]["b1"].thread_id == "thread-1"
    assert update["proposals"]["b1"].status == "sent"
    assert update["pending_approval"] is None and update["phase"] == "awaiting_replies"


async def test_approved_emails_are_sent_even_when_llm_is_down(make_deps, settings):
    gmail = FakeGmail(settings)
    deps = make_deps(FakeLLM(decide={"outreach.plan": LLMError("quota")}), gmail=gmail)
    update = await outreach.run(approved_state(), deps)
    assert len(gmail.sent) == 1 and update["outreach"]["b1"].status == "sent"


async def test_gmail_failure_keeps_draft_and_escalates(make_deps, settings):
    update = await outreach.run(approved_state(), make_deps(llm(), gmail=FakeGmail(settings, fail_send=True)))
    draft = update["outreach"]["b1"].history[0]
    assert draft["state"] == "failed" and "invalid_grant" in draft["error"]
    [escalation] = by_type(update, "escalation")
    assert escalation.to_agent == "human"
    assert update["errors"][-1]["tool"] == "gmail.send"


def live_state(gmail: FakeGmail, sent_days_ago: float = 0.5):
    threads, ids = {}, ["b1", "b2", "b3", "b4"]
    sent_ts = (datetime.now(UTC) - timedelta(days=sent_days_ago)).isoformat()
    for i, b in enumerate(ids):
        t = thread(b, status="sent", thread_id=f"thread-{i + 1}")
        t.history.append({"type": "sent", "kind": "initial", "gmail_message_id": f"m-{i}", "subject": "Sponsor",
                          "ts": sent_ts})
        threads[b] = t
    poll = new_message("human", "outreach", "request", {"action": "poll_replies"}, "check replies")
    return state(brands=[brand(i) for i in range(1, 5)], selected_brand_ids=ids,
                 proposals={b: proposal(b, status="sent") for b in ids}, outreach=threads,
                 phase="replies", messages=[poll]), poll


async def test_replies_are_classified_and_routed(make_deps, settings):
    gmail = FakeGmail(settings)
    gmail.reply("thread-1", "Looks nice but too expensive for our budget.\n\nOn Mon, X wrote:\n> old")
    gmail.reply("thread-2", "Sorry, not interested this year.")
    gmail.reply("thread-3", "We will go with Silver!")
    gmail.reply("thread-4", "Delivery failed", sender="mailer-daemon@googlemail.com", bounce=True)
    s, poll = live_state(gmail)
    update = await outreach.run(s, make_deps(llm(), gmail=gmail))
    threads = update["outreach"]

    [pushback] = by_type(update, "revision_request")  # L3
    assert pushback.content["brand_id"] == "b1" and pushback.content["max_price_inr"] == 15000
    assert pushback.content["source"] == "brand_reply" and threads["b1"].status == "pushback"
    replacements = {m.content["brand_id"] for m in by_type(update, "request")
                    if m.content.get("reason") == "replacement"}  # L4
    assert replacements == {"b2", "b4"}
    # a decline and a bounce, both cafes: L6 tells Discovery about the category as well
    assert any(m.content.get("category") == "cafe" for m in by_type(update, "request"))
    assert threads["b2"].status == "declined" and threads["b4"].status == "bounced"
    assert threads["b3"].status == "accepted" and threads["b3"].history[-1]["tier"] == "Silver"
    assert update["proposals"]["b3"].status == "accepted"
    assert update["phase"] == "proposals"
    assert update["messages"][-1].content["in_reply_to"] == [poll.id]


async def test_two_gym_declines_send_category_feedback_to_discovery(make_deps, settings):
    """L6 (Outreach side): outcomes are recorded per brand; 2 negatives in one category -> category_feedback."""
    gmail = FakeGmail(settings)
    gmail.reply("thread-1", "Sorry, not interested this year.")
    gmail.reply("thread-2", "Thanks, but not interested.")
    gmail.reply("thread-3", "We will go with Silver!")
    s, _ = live_state(gmail)
    s["brands"] = [brand(1, category="gym"), brand(2, category="gym"), brand(3), brand(4)]
    update = await outreach.run(s, make_deps(llm(), gmail=gmail))

    assert update["feedback"] == {"b1": ["declined"], "b2": ["declined"], "b3": ["accepted"]}
    [category] = [m for m in by_type(update, "request") if m.content.get("reason") == "category_feedback"]
    assert (category.to_agent, category.content["category"], category.content["negative"]) == ("discovery", "gym", 2)
    assert {e["brand_id"] for e in category.content["evidence"]} == {"b1", "b2"}

    again = await outreach.run({**s, "feedback": update["feedback"], "messages": [*s["messages"], category]},
                               make_deps(llm(), gmail=gmail))
    assert not [m for m in again["messages"] if m.content.get("reason") == "category_feedback"]  # told once


async def test_declines_are_handled_even_when_the_tool_budget_runs_out(make_deps, settings):
    """Regression (2026-10-19 trace): with many replies, poll + classify + one action per brand needs more
    than the 6 tool calls an agent turn allows. Declines the LLM never got to must still be recorded and
    replaced, or the threads stay "replied", nothing reaches Discovery and L6 never fires."""
    gmail = FakeGmail(settings)
    replies = ["Looks nice but too expensive for our budget.", "We will go with Silver!", "We will go with Gold!",
               "We will go with Bronze!", "Sorry, not interested this year.", "Thanks, but not interested."]
    ids = [f"b{i}" for i in range(1, 7)]
    sent_ts = (datetime.now(UTC) - timedelta(days=0.5)).isoformat()
    threads = {}
    for i, (b, text) in enumerate(zip(ids, replies, strict=True)):
        threads[b] = thread(b, status="sent", thread_id=f"thread-{i + 1}")
        threads[b].history.append({"type": "sent", "kind": "initial", "gmail_message_id": f"m-{i}",
                                   "subject": "Sponsor", "ts": sent_ts})
        gmail.reply(f"thread-{i + 1}", text)
    poll = new_message("human", "outreach", "request", {"action": "poll_replies"}, "check replies")
    s = state(brands=[brand(1), brand(2), brand(3), brand(4), brand(5, category="gym"), brand(6, category="gym")],
              selected_brand_ids=ids, proposals={b: proposal(b, status="sent") for b in ids}, outreach=threads,
              phase="replies", messages=[poll])
    update = await outreach.run(s, make_deps(llm(), gmail=gmail))

    out = update["outreach"]
    assert out["b5"].status == "declined" and out["b6"].status == "declined"
    assert update["feedback"]["b5"] == ["declined"] and update["feedback"]["b6"] == ["declined"]
    assert update["proposals"]["b5"].status == "declined"
    replacements = {m.content["brand_id"] for m in by_type(update, "request")
                    if m.content.get("reason") == "replacement"}
    assert replacements == {"b5", "b6"}  # L4 for both gyms
    [category] = [m for m in by_type(update, "request") if m.content.get("reason") == "category_feedback"]
    assert category.content["category"] == "gym"  # L6
    assert update["phase"] == "proposals"  # b1's pushback is waiting on the Proposal agent


def gym_workspace(feedback: dict, messages=()) -> outreach.Workspace:
    brands = [brand(1, category="gym"), brand(2, category="gym"), brand(3, category="gym"), brand(4)]
    return outreach.Workspace(state(brands=brands, feedback=feedback, messages=list(messages)))


async def clef_entries(db) -> list[dict]:
    return [t["payload"] for t in await db.list_trace("run-test")
            if t["event_type"] == "decision" and t["payload"]["kind"] == "clef_judgement"]


@pytest.mark.parametrize(("p_yes", "sent", "source", "escalated"), [
    (0.9, True, "clef", False),                       # Clef: yes, stop prioritising gyms
    (0.1, False, "clef", False),                      # Clef: no, keep gyms (e.g. one gym accepted)
    (0.5, True, "clef", True),                        # unsure: rule (send) + ask the organizer
    (ClefError("HTTP 503"), True, "rule_fallback", False),
])
async def test_l6_category_feedback_is_judged_by_clef(make_deps, db, p_yes, sent, source, escalated):
    clef = FakeClef({"outreach.category_feedback": p_yes})
    ws = gym_workspace({"b1": ["declined"], "b2": ["bounced"], "b3": ["accepted"]})
    await outreach.category_feedback(make_deps(llm(), clef=clef), ws)

    asked = clef.calls[0]["state"]
    assert asked["category"] == "gym" and {e["outcome"] for e in asked["evidence"]} == {"declined", "bounced"}
    assert asked["positive_outcomes"] == [{"brand": "Brand 3", "outcome": "accepted"}]
    assert asked["event_audience"] == state()["event"]["audience"]
    [entry] = await clef_entries(db)
    assert (entry["purpose"], entry["source"], entry["escalated"], entry["rule_answer"]) == (
        "outreach.category_feedback", source, escalated, True)
    requests = [m for m in ws.messages if m.content.get("reason") == "category_feedback"]
    assert len(requests) == int(sent)
    if sent:
        assert requests[0].content["decided_by"] == source
        assert requests[0].content["p_yes"] == (None if source == "rule_fallback" else p_yes)
    else:
        assert ws.category_checks == {"gym": 2}
    escalations = [m for m in ws.messages if m.type == "escalation"]
    assert [m.content["reason"] for m in escalations] == (["clef_unsure"] if escalated else [])


async def test_l6_a_no_is_only_asked_again_after_a_new_negative(make_deps, db):
    clef = FakeClef({"outreach.category_feedback": [0.1, 0.9]})
    deps = make_deps(llm(), clef=clef)
    feedback = {"b1": ["declined"], "b2": ["bounced"]}
    first = gym_workspace(feedback)
    await outreach.category_feedback(deps, first)
    assert first.category_checks == {"gym": 2} and not first.messages
    # Outreach's step result carries the remembered "no"
    remembered = new_message("outreach", "supervisor", "result", {"category_checks": first.category_checks}, "done")

    same = gym_workspace(feedback, [remembered])  # next poll, nothing new: not asked again
    await outreach.category_feedback(deps, same)
    assert len(clef.calls) == 1 and not same.messages

    worse = gym_workspace({**feedback, "b3": ["silent"]}, [remembered])  # a third gym went silent
    await outreach.category_feedback(deps, worse)
    assert len(clef.calls) == 2
    [request] = worse.messages
    assert (request.content["category"], request.content["negative"], request.content["decided_by"]) == ("gym", 3, "clef")
    assert len(await clef_entries(db)) == 2


async def test_acceptance_is_remembered_as_a_deal(make_deps, settings, db):
    db.runs["run-test"] = {"id": "run-test", "owner_id": "user-1"}
    gmail = FakeGmail(settings)
    gmail.reply("thread-3", "We will go with Silver!")
    s, _ = live_state(gmail)
    s["proposals"]["b3"].tiers[1].price_inr = 9000  # Silver after a counter-offer
    await outreach.run(s, make_deps(llm(), gmail=gmail))
    [deal] = await db.list_deals("user-1", "cafe")
    assert (deal["brand_id"], deal["tier"], deal["price_inr"], deal["event_id"]) == ("b3", "Silver", 9000, "event-1")
    assert await db.list_deals("user-2", "cafe") == []


async def test_deal_store_failure_does_not_block_acceptance(make_deps, settings, db):
    db.runs["run-test"] = {"id": "run-test", "owner_id": "user-1"}

    async def broken(*_):
        raise RuntimeError("db down")

    db.add_deal = broken
    gmail = FakeGmail(settings)
    gmail.reply("thread-3", "We will go with Gold!")
    s, _ = live_state(gmail)
    update = await outreach.run(s, make_deps(llm(), gmail=gmail))
    assert update["outreach"]["b3"].status == "accepted"
    assert any(e["tool"] == "db.add_deal" for e in update["errors"])


async def test_silent_thread_gets_a_nudge_draft(make_deps, settings):
    gmail = FakeGmail(settings)
    s, _ = live_state(gmail, sent_days_ago=4)
    update = await outreach.run(s, make_deps(llm(), gmail=gmail))
    nudges = [e for t in update["outreach"].values() for e in t.history if e.get("kind") == "nudge"]
    assert len(nudges) >= 1 and nudges[0]["state"] == "pending"
    assert update["phase"] == "send_review"  # nudges also need the organizer's approval


async def test_nothing_to_do_reports_instead_of_failing(make_deps, settings):
    update = await outreach.run(state(phase="awaiting_replies"), make_deps(llm(), gmail=FakeGmail(settings)))
    assert update["messages"][-1].reasoning == "Nothing for outreach to do right now."

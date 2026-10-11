"""Outreach agent: pre-send critique (L2), approval-gated sending, reply handling (L3, L4) and nudges."""

from datetime import UTC, datetime, timedelta

from app.agents import outreach
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
    replacements = {m.content["brand_id"] for m in by_type(update, "request") if m.to_agent == "discovery"}  # L4
    assert replacements == {"b2", "b4"}
    assert threads["b2"].status == "declined" and threads["b4"].status == "bounced"
    assert threads["b3"].status == "accepted" and threads["b3"].history[-1]["tier"] == "Silver"
    assert update["proposals"]["b3"].status == "accepted"
    assert update["phase"] == "proposals"
    assert update["messages"][-1].content["in_reply_to"] == [poll.id]


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

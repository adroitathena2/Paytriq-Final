"""Discovery & Ranking agent: normal pass, L1 self-replan, tool failure, L4 replacement, L6 category feedback."""

from app.agents import discovery
from app.core.llm import FakeLLM, LLMError
from app.graph.messages import new_message
from tests.factories import brand, state
from tests.fakes import (FakePlaces, call, discovery_plan, discovery_replace, failing, make_brand, score_by_name,
                         tools_used)

REPLAN = {"radius_km": 4, "extra_categories": ["bakery"], "reasoning": "only 2 good cafes within 2 km; widen"}


def llm(**extra_generate) -> FakeLLM:
    return FakeLLM(decide={"discovery.plan": discovery_plan, "discovery.replace": discovery_replace},
                   generate={"discovery.score": score_by_name, "discovery.replan": REPLAN, **extra_generate})


def types(update) -> list[str]:
    return [m.type for m in update["messages"]]


async def test_enough_good_brands_means_no_replan(make_deps):
    places = FakePlaces(lambda r, c: [make_brand(f"Good Cafe {i}") for i in range(6)])
    update = await discovery.run(state(), make_deps(llm(), places=places))
    assert update["phase"] == "shortlist_review"
    assert len(update["brands"]) == 6 and update["brands"][0].fit_score == 0.85
    assert "replan" not in types(update)
    assert places.calls == [{"radius_km": 2.0, "categories": ["cafe", "gym"]}]


async def test_too_few_good_brands_triggers_self_replan(make_deps, db):
    def results(radius, categories):
        if radius < 4:
            return [make_brand("Good Cafe 1"), make_brand("Good Cafe 2"), make_brand("Meh Gym")]
        return [make_brand(f"Good Bakery {i}", "bakery", km=3.2) for i in range(4)]

    places = FakePlaces(results)
    update = await discovery.run(state(), make_deps(llm(), places=places))

    [replan] = [m for m in update["messages"] if m.type == "replan"]
    assert replan.content["good_fit"] == 2 and replan.content["new_radius_km"] == 4
    assert replan.reasoning == REPLAN["reasoning"]
    assert places.calls[1]["radius_km"] == 4 and "bakery" in places.calls[1]["categories"]
    assert sum(b.fit_score >= 0.6 for b in update["brands"]) == 6
    assert any(t["event_type"] == "message" and t["payload"]["type"] == "replan" for t in db.trace)


async def test_replans_are_capped_and_radius_never_exceeds_4km(make_deps):
    places = FakePlaces(lambda r, c: [make_brand(f"Meh Shop {r}-{len(c)}")])
    big = {**REPLAN, "radius_km": 25}
    update = await discovery.run(state(), make_deps(llm(**{"discovery.replan": big}), places=places))
    assert types(update).count("replan") == discovery.MAX_REPLANS
    assert max(c["radius_km"] for c in places.calls) == 4
    assert update["phase"] == "shortlist_review"  # still delivers what it found


async def test_replan_without_llm_widens_deterministically(make_deps):
    places = FakePlaces(lambda r, c: [make_brand("Good Cafe"), make_brand(f"Meh {r}")])
    deps = make_deps(llm(**{"discovery.replan": LLMError("quota")}), places=places)
    update = await discovery.run(state(), deps)
    replan = next(m for m in update["messages"] if m.type == "replan")
    assert replan.content["new_radius_km"] == 4
    assert {"bakery", "restaurant"} <= set(replan.content["extra_categories"])


def eager_widener(ctx: dict) -> dict:
    """An LLM that asks for 3.5 km straight away, as Gemini did in the 2026-10-19 trace."""
    used = tools_used(ctx)
    if not used:
        return call("search_places", categories=["cafe", "gym"], radius_km=3.5)
    if "score_brands" not in used:
        return call("score_brands")
    return call("finish", summary="done")


async def test_first_pass_cannot_widen_without_a_replan(make_deps):
    """L1: widening past 2 km is always a traced replan, never a silent step inside the first pass."""
    def results(radius, categories):
        near = [make_brand("Good Cafe 1"), make_brand("Good Cafe 2"), make_brand("Meh Gym")]
        return near if radius <= 2 else near + [make_brand(f"Good Bakery {i}", "bakery", km=3.2) for i in range(3)]

    fake = FakeLLM(decide={"discovery.plan": eager_widener},
                   generate={"discovery.score": score_by_name, "discovery.replan": REPLAN})
    places = FakePlaces(results)
    update = await discovery.run(state(), make_deps(fake, places=places))

    assert places.calls[0]["radius_km"] == discovery.START_RADIUS_KM
    first_search = next(c for c in fake.calls if c["purpose"] == "discovery.plan" and c["context"]["observations"])
    assert "capped at 2.0 km" in first_search["context"]["observations"][0]["result"]["note"]
    [replan] = [m for m in update["messages"] if m.type == "replan"]
    assert replan.content["good_fit"] == 2
    assert places.calls[1]["radius_km"] == 3.5  # allowed once the replan raised the cap to 4 km
    assert sum(b.fit_score >= 0.6 for b in update["brands"]) == 5


async def test_tool_results_carry_ids_and_names_are_resolved(make_deps):
    """The LLM sees brand ids in search/score results; a name passed instead of an id still works."""
    def by_name(ctx: dict) -> dict:
        used = tools_used(ctx)
        if not used:
            return call("search_places", categories=["cafe"], radius_km=2)
        if "score_brands" not in used:
            return call("score_brands")
        if "find_contact" not in used:
            return call("find_contact", brand_id="Good Cafe 0")
        return call("finish", summary="done")

    fake = FakeLLM(decide={"discovery.plan": by_name}, generate={"discovery.score": score_by_name})
    places = FakePlaces(lambda r, c: [make_brand(f"Good Cafe {i}") for i in range(5)])
    await discovery.run(state(), make_deps(fake, places=places))

    observations = fake.calls[-1]["context"]["observations"]
    search, score, contact = (o["result"] for o in observations)
    assert search["sample"][0].startswith("good-cafe-0: Good Cafe 0")
    assert score["top"][0].startswith("good-cafe-")
    assert contact["ok"] and contact["brand_id"] == "good-cafe-0"


async def test_wrong_replacement_id_lists_the_valid_ones(make_deps):
    """A mistyped id gets an error naming the available ids, so the LLM can correct itself."""
    def typo_then_fix(ctx: dict) -> dict:
        used = tools_used(ctx)
        if not used:
            return call("pick_replacement", brand_id="b-two")
        if used == ["pick_replacement"]:
            return call("pick_replacement", brand_id="b2", why="fixed the id")
        return call("finish", summary="done")

    request = new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": "b1"}, "bounced")
    s = state(brands=[brand(1, 0.9), brand(2, 0.8)], selected_brand_ids=["b1"], messages=[request])
    fake = FakeLLM(decide={"discovery.replace": typo_then_fix})
    update = await discovery.run(s, make_deps(fake, places=FakePlaces(lambda r, c: [])))

    error = fake.calls[1]["context"]["observations"][0]["result"]["error"]
    assert "available ids: b2" in error
    assert update["messages"][-1].content["brand_id"] == "b2"
    assert update["messages"][-1].reasoning == "fixed the id"


async def test_failed_search_is_observed_and_agent_adapts(make_deps, db):
    # fails on the first call and on its automatic retry, so the agent must adapt
    places = FakePlaces(failing(2, lambda r, c: [make_brand(f"Good Books {i}", "bookstore") for i in range(5)]))
    update = await discovery.run(state(), make_deps(llm(), places=places))
    assert update["phase"] == "shortlist_review"
    assert places.calls[1] == places.calls[0]  # automatic retry of the same call
    assert places.calls[2]["categories"] == ["bookstore"]  # the LLM chose a different search
    assert update["errors"][0]["tool"] == "search_places"
    assert any(t["event_type"] == "tool_error" for t in db.trace)


async def test_total_failure_escalates_instead_of_crashing(make_deps):
    places = FakePlaces(failing(99, lambda r, c: []))
    update = await discovery.run(state(), make_deps(llm(), places=places))
    assert update["phase"] == "discovery_failed"
    escalation = update["messages"][-1]
    assert (escalation.type, escalation.to_agent) == ("escalation", "human")
    assert "map services" in escalation.reasoning


async def test_llm_outage_is_named_as_the_cause(make_deps):
    deps = make_deps(FakeLLM(decide={"discovery.plan": LLMError("GEMINI_API_KEY is not set")}),
                     places=FakePlaces(lambda r, c: []))
    update = await discovery.run(state(), deps)
    assert update["phase"] == "discovery_failed"
    assert "language model is unavailable" in update["messages"][-1].reasoning


async def test_organizer_feedback_reaches_the_scoring_prompt(make_deps):
    request = new_message("human", "discovery", "request", {"reason": "human_feedback", "feedback": "no gyms"}, "r")
    fake = llm()
    places = FakePlaces(lambda r, c: [make_brand(f"Good Cafe {i}") for i in range(5)])
    update = await discovery.run(state(messages=[request]), make_deps(fake, places=places))
    score_call = next(c for c in fake.calls if c["purpose"] == "discovery.score")
    assert score_call["context"]["organizer_guidance"] == "no gyms"
    assert update["messages"][-1].content["in_reply_to"] == [request.id]


async def test_replacement_picks_next_best_unused_brand(make_deps):
    request = new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": "b1"}, "declined")
    s = state(brands=[brand(1, 0.9), brand(2, 0.8), brand(3, 0.75), brand(4, 0.4)],
              selected_brand_ids=["b1", "b2"], messages=[request], phase="replies")
    update = await discovery.run(s, make_deps(llm(), places=FakePlaces(lambda r, c: [])))
    assert update["selected_brand_ids"] == ["b1", "b2", "b3"]
    result = update["messages"][-1]
    assert result.content == {"replacement_for": "b1", "brand_id": "b3", "reason": "replacement",
                              "in_reply_to": [request.id]}
    assert update["phase"] == "proposals"


async def test_each_open_replacement_request_gets_its_own_brand(make_deps):
    """Regression (A4): two declines at once used to get one replacement and both were marked answered."""
    first = new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": "b1"}, "declined")
    second = new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": "b2"}, "bounced")
    feedback = new_message("human", "discovery", "request", {"reason": "human_feedback", "feedback": "more gyms"}, "x")
    s = state(brands=[brand(i, 0.9 - i / 20) for i in range(1, 6)], selected_brand_ids=["b1", "b2"],
              messages=[first, second, feedback], phase="replies")
    update = await discovery.run(s, make_deps(llm(), places=FakePlaces(lambda r, c: [])))
    results = [m for m in update["messages"] if m.type == "result"]
    assert [(m.content["replacement_for"], m.content["in_reply_to"]) for m in results] == [
        ("b1", [first.id]), ("b2", [second.id])]
    assert results[0].content["brand_id"] != results[1].content["brand_id"]
    assert update["selected_brand_ids"] == ["b1", "b2", "b3", "b4"]
    answered = {i for m in update["messages"] for i in m.content.get("in_reply_to", [])}
    assert feedback.id not in answered  # unrelated requests stay open for the next turn


async def test_infeasible_brand_from_proposal_is_replaced(make_deps):
    """L7 (Discovery side): Proposal's infeasible request is answered with a replacement."""
    request = new_message("proposal", "discovery", "request",
                          {"reason": "infeasible", "brand_id": "b1", "why": "wholesaler"}, "no fit")
    s = state(brands=[brand(1, 0.9, category="wholesale"), brand(2, 0.8)], selected_brand_ids=["b1"],
              messages=[request], phase="replacement")
    update = await discovery.run(s, make_deps(llm(), places=FakePlaces(lambda r, c: [])))
    result = update["messages"][-1]
    assert (result.content["reason"], result.content["brand_id"], result.content["in_reply_to"]) == (
        "infeasible", "b2", [request.id])
    assert update["selected_brand_ids"] == ["b1", "b2"]


async def test_category_feedback_penalises_and_replaces_from_other_categories(make_deps):
    """L6: two gym declines -> gyms x0.6, logged -> replacements and top-up come from other categories."""
    gyms = [brand(i, 0.9, category="gym") for i in (1, 2, 3)]
    others = [brand(4, 0.8, category="cafe"), brand(5, 0.75, category="coaching"), brand(6, 0.7, category="salon")]
    declines = [new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": b}, "declined")
                for b in ("b1", "b2")]
    category = new_message("outreach", "discovery", "request", {
        "reason": "category_feedback", "category": "gym", "negative": 2,
        "evidence": [{"brand_id": "b1", "outcome": "declined"}, {"brand_id": "b2", "outcome": "declined"}]}, "2 gyms")
    s = state(brands=gyms + others, selected_brand_ids=["b1", "b2", "b4"], target_brand_count=3,
              messages=[*declines, category], phase="replies")
    update = await discovery.run(s, make_deps(llm(), places=FakePlaces(lambda r, c: [])))

    [penalty] = [m for m in update["messages"] if m.content.get("reason") == "category_penalty"]
    assert (penalty.content["category"], penalty.content["factor"], penalty.content["rescored"]) == ("gym", 0.6, 3)
    assert next(b for b in update["brands"] if b.id == "b3").fit_score == 0.54
    new = update["selected_brand_ids"][3:]
    assert new and all(b in ("b5", "b6") for b in new)  # never the third gym
    answered = {i for m in update["messages"] for i in m.content.get("in_reply_to", [])}
    assert {declines[0].id, declines[1].id, category.id} <= answered


async def test_replacement_falls_back_when_llm_is_down(make_deps):
    request = new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": "b1"}, "bounced")
    s = state(brands=[brand(1, 0.9), brand(2, 0.7)], selected_brand_ids=["b1"], messages=[request])
    deps = make_deps(FakeLLM(decide={"discovery.replace": LLMError("down")}), places=FakePlaces(lambda r, c: []))
    update = await discovery.run(s, deps)
    assert update["messages"][-1].content["brand_id"] == "b2"


async def test_no_replacement_left_escalates(make_deps):
    request = new_message("outreach", "discovery", "request", {"reason": "replacement", "brand_id": "b1"}, "declined")
    s = state(brands=[brand(1, 0.9), brand(2, 0.2)], selected_brand_ids=["b1"], messages=[request])
    deps = make_deps(FakeLLM(decide={"discovery.replace": {"name": "finish", "args": {"summary": "none good"}}}),
                     places=FakePlaces(lambda r, c: []))
    update = await discovery.run(s, deps)
    assert update["messages"][-1].type == "escalation"
    assert "selected_brand_ids" not in update

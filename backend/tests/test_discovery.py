"""Discovery & Ranking agent: normal pass, L1 self-replan, tool failure, and L4 replacement."""

from app.agents import discovery
from app.core.llm import FakeLLM, LLMError
from app.graph.messages import new_message
from tests.factories import brand, state
from tests.fakes import FakePlaces, discovery_plan, discovery_replace, failing, make_brand, score_by_name

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
    assert result.content == {"replacement_for": "b1", "brand_id": "b3", "in_reply_to": [request.id]}
    assert update["phase"] == "proposals"


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

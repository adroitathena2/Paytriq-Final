"""core/clef.py: the Clef REST client (mocked HTTP, no network) and the guarded ``judge`` helper."""

import httpx
import pytest

from app.core.clef import ClefClient, ClefError, FakeClef, UnconfiguredClef, build_clef, judge
from app.core.llm import FakeLLM

SUCCESS = {
    "result": {"model": "clef-flash",
               "answers": {"proposal.feasibility": {"type": "noul", "noul": 0.8123}},
               "usage": {"input_tokens": 120, "output_tokens": 0}},
    "success": True, "errors": [], "messages": [],
}


def client_with(*responses) -> tuple[ClefClient, list[httpx.Request]]:
    """A ClefClient whose HTTP calls get ``responses`` in order (an Exception is raised instead)."""
    queue, seen = list(responses), []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)

    return ClefClient("acct-123", "token-abc", retry_delay_s=0, transport=httpx.MockTransport(handler)), seen


async def test_client_parses_a_success_response():
    clef, seen = client_with((200, SUCCESS))
    p = await clef.ask_yes_no("proposal.feasibility", {"brand": {"name": "X"}}, "Does it fit?")
    assert p == pytest.approx(0.8123)
    req = seen[0]
    assert str(req.url).endswith("/accounts/acct-123/ai/run/@cf/cloudflare/clef-flash")
    assert req.headers["authorization"] == "Bearer token-abc"
    body = httpx.Response(200, content=req.content).json()
    assert body == {"model": "clef-flash", "state": {"brand": {"name": "X"}},
                    "questions": {"proposal.feasibility": {"type": "noul", "instructions": "Does it fit?"}}}


async def test_client_raises_on_success_false():
    clef, _ = client_with((200, {"success": False, "errors": [{"code": 5006, "message": "bad model"}],
                                 "messages": [], "result": None}))
    with pytest.raises(ClefError, match="5006"):
        await clef.ask_yes_no("proposal.feasibility", {}, "q")


async def test_client_retries_once_on_429_then_gives_up():
    ok, seen = client_with((429, {"success": False, "errors": []}), (200, SUCCESS))
    assert await ok.ask_yes_no("proposal.feasibility", {}, "q") == pytest.approx(0.8123)
    assert len(seen) == 2
    bad, seen = client_with((503, {"success": False}), (503, {"success": False, "errors": ["busy"]}))
    with pytest.raises(ClefError, match="503"):
        await bad.ask_yes_no("proposal.feasibility", {}, "q")
    assert len(seen) == 2


async def test_client_timeout_and_bad_shape_are_clef_errors():
    slow, _ = client_with(httpx.ReadTimeout("timed out"))
    with pytest.raises(ClefError, match="ReadTimeout"):
        await slow.ask_yes_no("p", {}, "q")
    odd, _ = client_with((200, {"success": True, "result": {"answers": {}}}))
    with pytest.raises(ClefError, match="shape"):
        await odd.ask_yes_no("p", {}, "q")


async def test_unconfigured_clef_and_build_clef(settings):
    with pytest.raises(ClefError):
        await UnconfiguredClef().ask_yes_no("p", {}, "q")
    assert isinstance(build_clef(settings), UnconfiguredClef)  # no Cloudflare credentials in tests
    configured = settings.model_copy(update={"cloudflare_account_id": "a", "cloudflare_api_token": "t"})
    assert isinstance(build_clef(configured), ClefClient)
    disabled = configured.model_copy(update={"clef_enabled": False})
    assert isinstance(build_clef(disabled), UnconfiguredClef)


async def judgements(db) -> list[dict]:
    return [t["payload"] for t in await db.list_trace("run-test")
            if t["event_type"] == "decision" and t["payload"]["kind"] == "clef_judgement"]


@pytest.mark.parametrize(("policy", "rule", "answer", "source", "escalated"), [
    (0.9, False, True, "clef", False),            # confident yes overrides the rule
    (0.1, True, False, "clef", False),            # confident no overrides the rule
    (0.5, True, True, "clef", True),              # unsure: keep the rule, ask the organizer
    (ClefError("down"), False, False, "rule_fallback", False),
    (RuntimeError("bug"), True, True, "rule_fallback", False),  # any failure, never a crash
])
async def test_judge_thresholds_and_fallback(make_deps, db, policy, rule, answer, source, escalated):
    deps = make_deps(FakeLLM(), clef=FakeClef({"t": policy}))
    j = await judge(deps, agent="proposal", purpose="t.q", state={"x": 1}, question="Q?", rule_answer=rule)
    assert (j.answer, j.source, j.escalate) == (answer, source, escalated)
    [entry] = await judgements(db)  # exactly one trace entry per judgement
    assert entry["source"] == source and entry["answer"] == answer and entry["rule_answer"] == rule
    assert entry["escalated"] == escalated and (entry["threshold_hi"], entry["threshold_lo"]) == (0.75, 0.25)
    assert entry["p_yes"] == (None if source == "rule_fallback" else policy)
    assert ("error" in entry) == (source == "rule_fallback")


async def test_judge_without_a_clef_client_uses_the_rule(make_deps, db):
    j = await judge(make_deps(FakeLLM(), clef=None), agent="outreach", purpose="p", state={}, question="Q?",
                    rule_answer=True)
    assert (j.answer, j.source, j.p_yes) == (True, "rule_fallback", None)
    assert len(await judgements(db)) == 1

"""Cloudflare Clef: a narrow judgement layer for two coordination loops (L6 and L7).

Clef is a decision model on Workers AI. It answers typed questions about a JSON ``state`` with calibrated
probabilities and writes no text. Paytriq asks it only yes/no (``noul``) questions:

- L7 (Proposal): can any student-event deliverable bring this business customers or brand value?
- L6 (Outreach): after these outcomes, should we stop prioritising this business category?

Code only guards the decision, in ``judge``: P(yes) >= ``threshold_hi`` means yes, <= ``threshold_lo`` means
no, anything in between keeps the old rule's answer and asks the organizer (escalation). If Clef is
unconfigured, down or slow, the rule decides (``source = "rule_fallback"``). Every judgement writes one
``decision`` trace entry with ``kind = "clef_judgement"``. Supervisor routing and reply classification stay
on Gemini because their written reasons are trace evidence; Clef returns only numbers.

REST API (Workers AI):
    POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/cloudflare/{model}
    body     {"model": "clef-flash", "state": {...}, "questions": {"<id>": {"type": "noul", "instructions": "..."}}}
    response {"success": true, "errors": [], "messages": [],
              "result": {"model": "...", "answers": {"<id>": {"type": "noul", "noul": 0.93}}, "usage": {...}}}
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.graph.messages import emit

log = logging.getLogger("paytriq.clef")

API_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/cloudflare/{model}"
MODELS = ("clef-flash", "clef")
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ClefError(Exception):
    """Clef could not answer (not configured, HTTP error, timeout or an unexpected response)."""


class ClefClient:
    """Async Workers AI client for Clef: 10 s timeout and one retry on 429/5xx."""

    def __init__(self, account_id: str, api_token: str, model: str = "clef-flash", timeout_s: float = 10.0,
                 retry_delay_s: float = 1.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if model not in MODELS:
            raise ValueError(f"CLEF_MODEL must be one of {MODELS}, got {model!r}")
        self.url = API_URL.format(account_id=account_id, model=model)
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_token}"}
        self.timeout_s = timeout_s
        self.retry_delay_s = retry_delay_s
        self.transport = transport  # tests pass httpx.MockTransport

    async def _post(self, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as client:
            for attempt in range(2):
                try:
                    resp = await client.post(self.url, headers=self.headers, json=body)
                except httpx.HTTPError as exc:  # timeouts and connection errors
                    raise ClefError(f"Clef request failed: {type(exc).__name__}: {exc}") from exc
                if resp.status_code in RETRY_STATUSES and attempt == 0:
                    await asyncio.sleep(self.retry_delay_s)
                    continue
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise ClefError(f"Clef returned HTTP {resp.status_code} with a non-JSON body") from exc
                if resp.status_code >= 400 or not data.get("success"):
                    raise ClefError(f"Clef returned HTTP {resp.status_code}: {data.get('errors') or 'success=false'}")
                return data
        raise AssertionError("unreachable")

    async def ask_yes_no(self, purpose: str, state: dict, question: str) -> float:
        """P(yes) for one ``noul`` question. ``purpose`` doubles as the question id."""
        data = await self._post({"model": self.model, "state": state,
                                 "questions": {purpose: {"type": "noul", "instructions": question}}})
        try:
            answer = data["result"]["answers"][purpose]
            p_yes = float(answer["noul"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ClefError(f"unexpected Clef response shape: {str(data)[:300]}") from exc
        if not 0.0 <= p_yes <= 1.0:
            raise ClefError(f"Clef probability out of range: {p_yes}")
        return p_yes


class UnconfiguredClef:
    """Used when CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN are missing or CLEF_ENABLED=false."""

    def __init__(self, why: str = "Clef is not configured") -> None:
        self.why = why

    async def ask_yes_no(self, purpose: str, state: dict, question: str) -> float:
        raise ClefError(self.why)


ClefPolicy = Callable[[dict], Any] | list | float | Exception


class FakeClef:
    """Scripted Clef for tests and rehearsals, in the same style as FakeLLM.

    Policies are looked up by ``purpose`` (exact match, then the part before the first dot). A policy is a
    probability, a callable ``f(state) -> probability``, a list (one item per call) or an Exception to raise.
    """

    def __init__(self, policies: dict[str, ClefPolicy] | None = None) -> None:
        self.policies = policies or {}
        self.calls: list[dict] = []

    async def ask_yes_no(self, purpose: str, state: dict, question: str) -> float:
        self.calls.append({"purpose": purpose, "state": state, "question": question})
        policy = self.policies.get(purpose, self.policies.get(purpose.split(".")[0]))
        if policy is None:
            raise ClefError(f"FakeClef has no policy for '{purpose}'")
        if isinstance(policy, list):
            if not policy:
                raise ClefError(f"FakeClef policy for '{purpose}' is exhausted")
            policy = policy.pop(0)
        value = policy(state) if callable(policy) else policy
        if isinstance(value, Exception):
            raise value
        return float(value)


def build_clef(settings) -> Any:
    if not settings.clef_enabled:
        return UnconfiguredClef("CLEF_ENABLED=false")
    if not (settings.cloudflare_account_id and settings.cloudflare_api_token):
        log.warning("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN not set: L6/L7 judgements use the rule fallback")
        return UnconfiguredClef("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN not set")
    return ClefClient(settings.cloudflare_account_id, settings.cloudflare_api_token, settings.clef_model)


@dataclass
class Judgement:
    answer: bool
    source: str  # "clef" | "rule_fallback"
    p_yes: float | None
    escalate: bool


async def judge(deps, *, agent: str, purpose: str, state: dict, question: str, rule_answer: bool,
                threshold_hi: float = 0.75, threshold_lo: float = 0.25) -> Judgement:
    """Ask Clef one yes/no question and guard the answer with thresholds and the rule fallback.

    Never raises: any Clef failure falls back to ``rule_answer``. Writes exactly one trace entry.
    """
    error = None
    try:
        if deps.clef is None:
            raise ClefError("no Clef client")
        p_yes = await deps.clef.ask_yes_no(purpose, state, question)
    except Exception as exc:  # a judgement layer failure must never crash a run
        p_yes, error = None, f"{type(exc).__name__}: {exc}"
    if p_yes is None:
        j = Judgement(answer=rule_answer, source="rule_fallback", p_yes=None, escalate=False)
    elif p_yes >= threshold_hi:
        j = Judgement(answer=True, source="clef", p_yes=p_yes, escalate=False)
    elif p_yes <= threshold_lo:
        j = Judgement(answer=False, source="clef", p_yes=p_yes, escalate=False)
    else:  # Clef is unsure: keep the rule's answer and ask the organizer
        j = Judgement(answer=rule_answer, source="clef", p_yes=p_yes, escalate=True)
    payload = {"kind": "clef_judgement", "purpose": purpose, "question": question, "p_yes": j.p_yes,
               "threshold_hi": threshold_hi, "threshold_lo": threshold_lo, "answer": j.answer, "source": j.source,
               "escalated": j.escalate, "rule_answer": rule_answer}
    if error:
        payload["error"] = error[:300]
    await deps.log(agent, "decision", payload)
    return j


async def unsure_escalation(deps, agent: str, purpose: str, j: Judgement, about: str, content: dict):
    """The ``escalation`` to the organizer when Clef's P(yes) fell between the thresholds."""
    return await emit(deps, agent, "human", "escalation",
                      {"reason": "clef_unsure", "purpose": purpose, "p_yes": j.p_yes, "applied": j.answer, **content},
                      f"Clef was unsure about {about} (P(yes) = {j.p_yes:.2f}); kept the rule's answer "
                      f"({'yes' if j.answer else 'no'}). Please check this one.")

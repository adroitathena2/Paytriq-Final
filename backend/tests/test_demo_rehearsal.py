"""The full demo scenario end to end through the API, in offline rehearsal mode (scripted LLM).

Rehearsal runs exist for practice only; this test checks that the fixture world reliably triggers every
coordination loop (L1-L8) and arbitration rule (A1-A3), and that every trace entry is marked rehearsal.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.api.common import Services
from app.core.config import get_settings
from app.core.llm import FakeLLM
from app.core.trace import REHEARSAL_MARK
from app.demo.world import SUGGESTED_SHORTLIST
from app.graph.build import build_graph
from app.graph.runner import RunManager
from app.main import create_app


@pytest.fixture
def client(make_deps, settings):
    deps = make_deps(FakeLLM())  # never used: rehearsal events swap in the scripted LLM
    app = create_app(Services(deps=deps, runner=RunManager(build_graph(deps), deps)))
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


def settle(client, run_id, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["run"]["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("run did not settle")


def approve_sends(client, run_id) -> dict:
    """Approve every send gate (initial emails, revisions, replacements, nudges) until the run rests."""
    body = settle(client, run_id)
    for _ in range(10):
        if body["run"]["pending_gate"] != "send":
            return body
        assert client.post(f"/runs/{run_id}/approve", json={"gate": "send", "decision": "approve"}).status_code == 202
        body = settle(client, run_id)
    raise AssertionError("send gate kept reopening")


def poll(client, run_id) -> dict:
    assert client.post(f"/runs/{run_id}/poll-replies").status_code == 202
    return approve_sends(client, run_id)


def test_rehearsal_demo_fires_every_loop_and_rule(client):
    event = client.post("/events/demo", json={"rehearsal": True}).json()
    assert event["demo_scenario"] == "rehearsal" and event["name"] == "Fergusson Fest 2026"
    run_id = client.post(f"/events/{event['id']}/runs").json()["id"]

    body = settle(client, run_id)
    assert body["run"]["pending_gate"] == "shortlist"
    client.post(f"/runs/{run_id}/approve", json={"gate": "shortlist", "decision": "approve",
                                                 "edits": {"selected_brand_ids": SUGGESTED_SHORTLIST}})
    body = approve_sends(client, run_id)
    assert body["run"]["phase"] == "awaiting_replies"

    body = poll(client, run_id)  # bounce, two gym declines, Rs 8,000 pushback, Apex accepts
    body = poll(client, run_id)  # Bean Theory accepts v2, replacements reply
    values = body["values"]
    accepted = {b for b, t in values["outreach"].items() if t["status"] == "accepted"}
    assert {"demo-apex-coaching", "demo-bean-theory"} <= accepted

    added = client.post(f"/runs/{run_id}/demo-evidence").json()["added"]
    assert added > 0
    assert client.post(f"/runs/{run_id}/audit", json={"actual_footfall": 1500}).status_code == 202
    body = settle(client, run_id)
    assert (body["run"]["phase"], body["run"]["pending_gate"]) == ("roi_blocked", None)  # A3 blocks publishing

    assert client.post(f"/runs/{run_id}/publish-with-gaps").status_code == 202
    body = settle(client, run_id)
    assert body["run"]["pending_gate"] == "roi_publish"  # the normal approval still follows
    client.post(f"/runs/{run_id}/approve", json={"gate": "roi_publish", "decision": "approve"})
    body = settle(client, run_id)
    assert body["run"]["status"] == "completed" and body["values"]["roi_report"]["published"]

    # Every coordination loop and arbitration rule is in the trace ---------------------------------
    trace = client.get(f"/runs/{run_id}/trace").json()
    msgs = [t["payload"] for t in trace if t["event_type"] == "message"]
    arbitrations = [t["payload"] for t in trace if t["event_type"] == "decision" and t["payload"]["kind"] == "arbitration"]

    def has(type_, **content):
        return any(m["type"] == type_ and all(m["content"].get(k) == v for k, v in content.items()) for m in msgs)

    assert any(m["type"] == "replan" and m["from_agent"] == "discovery" for m in msgs), "L1"
    assert has("revision_request", source="critique"), "L2"
    assert has("revision_request", source="brand_reply"), "L3"
    assert has("request", reason="replacement"), "L4"
    assert any(m["type"] == "escalation" and m["from_agent"] == "roi_audit" for m in msgs), "L5"
    assert has("request", reason="category_feedback", category="gym"), "L6"
    assert has("request", reason="infeasible", brand_id="demo-metro-wholesale"), "L7"
    assert has("result", reason="calibration"), "L8"
    rules = {(a["rule"], a["resolution"].split(":")[0].split(" ")[0]) for a in arbitrations}
    assert ("A1", "Delivery") in rules, "A1 bounce beats ranking"
    assert any(a["rule"] == "A2" and a["resolution"].startswith("Cap") for a in arbitrations), "A2 cap (coaching)"
    assert any(a["rule"] == "A2" and a["resolution"] == "insufficient data" for a in arbitrations), "A2 cafes"
    assert any(a["rule"] == "A3" and a["resolution"].startswith("block") for a in arbitrations), "A3"

    # ... and every entry is marked as rehearsal, including the export --------------------------------
    assert trace and all(t["payload"].get("rehearsal") == REHEARSAL_MARK for t in trace)
    export = client.get(f"/runs/{run_id}/trace/export")
    assert 'filename="REHEARSAL-NOT-FOR-SUBMISSION-' in export.headers["content-disposition"]
    assert export.json()["rehearsal"] is True


def test_demo_evidence_needs_a_demo_event(client):
    event = client.post("/events", json={
        "name": "TechFest 2026", "college": "SIT Pune", "event_date": "2026-11-20", "expected_footfall": 1500,
        "audience": "engineering students", "budget_goal_inr": 150000, "lat": 18.52, "lng": 73.85}).json()
    run_id = client.post(f"/events/{event['id']}/runs").json()["id"]
    settle(client, run_id)
    assert client.post(f"/runs/{run_id}/demo-evidence").status_code == 409
    export = client.get(f"/runs/{run_id}/trace/export")
    assert export.json()["rehearsal"] is False and "REHEARSAL" not in export.headers["content-disposition"]

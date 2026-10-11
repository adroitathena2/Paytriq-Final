"""API behaviour: validation, ownership, run lifecycle and trace export (auth disabled, fakes only)."""

import time

import pytest
from fastapi.testclient import TestClient

from app.api.common import Services
from app.core.config import get_settings
from app.core.llm import FakeLLM
from app.graph.build import build_graph
from app.graph.runner import RunManager
from app.main import create_app

EVENT = {
    "name": "TechFest 2026", "college": "SIT Pune", "event_date": "2026-11-20", "expected_footfall": 1500,
    "audience": "engineering students", "budget_goal_inr": 150000, "lat": 18.52, "lng": 73.85,
}


@pytest.fixture
def client(make_deps, settings):
    llm = FakeLLM(generate={"supervisor.route": {"next": "END", "reason": "test stops immediately"}})
    deps = make_deps(llm)
    app = create_app(Services(deps=deps, runner=RunManager(build_graph(deps), deps)))
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


def wait_until_settled(client, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["run"]["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("run did not settle")


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_invalid_event_gets_clear_422(client):
    resp = client.post("/events", json={**EVENT, "expected_footfall": -5, "lat": 300})
    assert resp.status_code == 422
    fields = {e["loc"][-1] for e in resp.json()["detail"]}
    assert {"expected_footfall", "lat"} <= fields


def test_run_lifecycle_and_trace_export(client):
    event = client.post("/events", json=EVENT).json()
    assert client.get("/events").json()[0]["id"] == event["id"]

    run = client.post(f"/events/{event['id']}/runs").json()
    body = wait_until_settled(client, run["id"])
    assert body["run"]["status"] == "idle"
    assert body["values"]["event"]["name"] == "TechFest 2026"

    trace = client.get(f"/runs/{run['id']}/trace").json()
    assert [t["event_type"] for t in trace][0] == "run_start"
    assert any(t["event_type"] == "message" and t["payload"]["type"] == "decision" for t in trace)

    export = client.get(f"/runs/{run['id']}/trace/export")
    assert "attachment" in export.headers["content-disposition"]
    assert export.json()["entries"] == trace


def test_approve_requires_a_pending_gate(client):
    event = client.post("/events", json=EVENT).json()
    run = client.post(f"/events/{event['id']}/runs").json()
    wait_until_settled(client, run["id"])
    resp = client.post(f"/runs/{run['id']}/approve", json={"gate": "send", "decision": "approve"})
    assert resp.status_code == 409


def test_unknown_ids_are_404(client):
    assert client.get("/runs/does-not-exist").status_code == 404
    assert client.post("/events/nope/runs").status_code == 404

"""API behaviour: validation, ownership, run lifecycle and trace export (auth disabled, fakes only)."""

import asyncio
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
    assert client.get("/runs").json()[0]["event_name"] == "TechFest 2026"
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
    # "Publish with gaps disclosed" is only possible while A3 blocks publishing
    assert client.post(f"/runs/{run['id']}/publish-with-gaps").status_code == 409


def test_evidence_upload_validation_and_audit_trigger(client):
    event = client.post("/events", json=EVENT).json()
    run = client.post(f"/events/{event['id']}/runs").json()
    wait_until_settled(client, run["id"])
    url = f"/runs/{run['id']}/evidence"

    assert client.post(url, data={"brand_id": "b1"}).status_code == 422  # neither photo nor link
    bad_type = client.post(url, data={"brand_id": "b1"}, files={"file": ("x.gif", b"GIF89a", "image/gif")})
    assert bad_type.status_code == 415
    assert client.post(url, data={"brand_id": "b1", "link": "ftp://x"}).status_code == 422

    photo = client.post(url, data={"brand_id": "b1", "note": "stall"},
                        files={"file": ("s.jpg", b"\xff\xd8x", "image/jpeg")})
    link = client.post(url, data={"brand_id": "b1", "link": "https://instagram.com/p/abc"})
    assert photo.status_code == link.status_code == 201
    assert [e["kind"] for e in client.get(url).json()] == ["photo", "link"]

    assert client.get(f"/runs/{run['id']}/report").status_code == 404
    audit = client.post(f"/runs/{run['id']}/audit")
    assert audit.status_code == 202 and audit.json()["evidence_items"] == 2


def test_oversized_and_empty_photos_get_413(client):
    event = client.post("/events", json=EVENT).json()
    run = client.post(f"/events/{event['id']}/runs").json()
    wait_until_settled(client, run["id"])
    url = f"/runs/{run['id']}/evidence"
    big = b"\xff\xd8" + b"0" * (8 * 1024 * 1024)
    resp = client.post(url, data={"brand_id": "b1"}, files={"file": ("big.jpg", big, "image/jpeg")})
    assert resp.status_code == 413 and "8 MB" in resp.json()["detail"]
    empty = client.post(url, data={"brand_id": "b1"}, files={"file": ("e.jpg", b"", "image/jpeg")})
    assert empty.status_code in (413, 422)


def test_continue_resumes_a_failed_run(client):
    event = client.post("/events", json=EVENT).json()
    run = client.post(f"/events/{event['id']}/runs").json()
    wait_until_settled(client, run["id"])
    services = client.app.state.services
    asyncio.run(services.db.update_run(run["id"], status="failed", last_error="RuntimeError: boom"))
    assert client.get(f"/runs/{run['id']}").json()["run"]["status"] == "failed"

    assert client.post(f"/runs/{run['id']}/continue").status_code == 202
    body = wait_until_settled(client, run["id"])
    assert body["run"]["status"] == "idle" and body["run"]["last_error"] is None
    trace = client.get(f"/runs/{run['id']}/trace").json()
    assert any(t["event_type"] == "run_start" and t["payload"]["trigger"] == "continue" for t in trace)


def test_continue_refuses_a_completed_run(client):
    event = client.post("/events", json=EVENT).json()
    run = client.post(f"/events/{event['id']}/runs").json()
    wait_until_settled(client, run["id"])
    asyncio.run(client.app.state.services.db.update_run(run["id"], status="completed"))
    resp = client.post(f"/runs/{run['id']}/continue")
    assert resp.status_code == 409 and "already complete" in resp.json()["detail"]


def test_unexpected_errors_get_a_friendly_500(client):
    async def broken(*_):
        raise RuntimeError("database exploded")

    client.app.state.services.db.list_events = broken
    with TestClient(client.app, raise_server_exceptions=False) as c:
        resp = c.get("/events")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Something went wrong on our side. Please retry."}


def test_unknown_ids_are_404(client):
    assert client.get("/runs/does-not-exist").status_code == 404
    assert client.post("/events/nope/runs").status_code == 404

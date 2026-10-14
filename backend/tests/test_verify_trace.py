"""backend/scripts/verify_trace.py: the checks a real exported trace must pass before it is committed.

These tests use small hand-built entry lists to test the checker itself; they are not traces and never
go in traces/.
"""

import json

from scripts import verify_trace


def msg(type_, from_agent="outreach", to_agent="proposal", **content):
    return {"id": 1, "ts": "t", "event_type": "message",
            "payload": {"id": f"m-{type_}", "type": type_, "from_agent": from_agent, "to_agent": to_agent,
                        "content": content}}


COMPLETE = [
    msg("replan", "discovery", "discovery"),
    msg("revision_request", source="critique"),
    msg("revision_request", source="brand_reply"),
    msg("request", to_agent="discovery", reason="replacement"),
    msg("escalation", "roi_audit", "human"),
    msg("request", to_agent="discovery", reason="category_feedback", category="gym"),
    msg("request", "proposal", "discovery", reason="infeasible"),
    msg("result", "roi_audit", "proposal", reason="calibration"),
    {"id": 9, "event_type": "decision", "payload": {"kind": "arbitration", "rule": "A2", "resolution": "Cap at 1"}},
]


def test_complete_trace_passes():
    assert verify_trace.problems({"entries": COMPLETE}) == []


def test_missing_loops_and_arbitration_are_reported():
    issues = verify_trace.problems({"entries": COMPLETE[:5]})
    assert {i.split(":")[0] for i in issues} == {"missing L6", "missing L7", "missing L8", "missing arbitration"}


def clef(source, escalated=False):
    return {"id": 20, "event_type": "decision", "payload": {"kind": "clef_judgement", "purpose": "proposal.feasibility",
                                                           "source": source, "escalated": escalated}}


def test_clef_judgements_are_counted_but_not_required():
    doc = {"entries": [*COMPLETE, clef("clef"), clef("clef", escalated=True), clef("rule_fallback")]}
    n = verify_trace.numbers(doc)
    assert (n["clef judgements"], n["clef judgements decided by clef"], n["clef judgements decided by rule_fallback"],
            n["clef escalations"]) == (3, 2, 1, 1)
    assert verify_trace.problems(doc) == []
    assert verify_trace.numbers({"entries": COMPLETE})["clef judgements"] == 0  # older traces still pass


def test_rehearsal_export_is_rejected():
    marked = [{**e, "payload": {**e["payload"], "rehearsal": "rehearsal — not real reasoning"}} for e in COMPLETE]
    assert any("rehearsal" in i for i in verify_trace.problems({"entries": marked}))
    assert any("rehearsal" in i for i in verify_trace.problems({"rehearsal": True, "entries": COMPLETE}))


def test_cli_exit_codes(tmp_path, capsys):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps({"run_id": "r", "entries": COMPLETE}), encoding="utf-8")
    bad.write_text(json.dumps({"run_id": "r", "entries": COMPLETE[:2]}), encoding="utf-8")
    assert verify_trace.main(["verify_trace.py", str(good)]) == 0
    assert verify_trace.main(["verify_trace.py", str(bad)]) == 1
    assert "FAILED" in capsys.readouterr().out

"""Check an exported Paytriq trace for every coordination loop (L1-L8) and arbitration rule (A1-A3).

Usage (from the repo root):
    python backend/scripts/verify_trace.py traces/2026-10-21-demo-mode-full-run.json

Read-only: the file is never changed. For each marker in traces/README.md it prints whether it fired, how
often, and the first trace entry id / message id. It also prints the message ids, timestamps and counts that
docs/design.md ("Response to the CA2 review") and the README demo script cite, so those numbers always come
from the real trace, never from memory or a rehearsal.

Exits 1 when the file is not usable as submission evidence (rehearsal markers, no entries). Loops that did
not fire in this run are listed, not treated as failures: the docs drop the sentences for them instead.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

REHEARSAL_WORD = "rehearsal"
DECISION_LOG_KINDS = {"route", "arbitration", "guardrail"}  # what the supervisor appends to decision_log


def _msg(entry: dict) -> dict | None:
    return entry["payload"] if entry.get("event_type") == "message" else None


def _is(type_: str, **content) -> Callable[[dict], bool]:
    def check(entry: dict) -> bool:
        m = _msg(entry)
        return bool(m) and m.get("type") == type_ and all(m.get("content", {}).get(k) == v for k, v in content.items())
    return check


def _from(type_: str, agent: str) -> Callable[[dict], bool]:
    return lambda e: bool(_msg(e)) and _msg(e).get("type") == type_ and _msg(e).get("from_agent") == agent


def _arbitration(rule: str) -> Callable[[dict], bool]:
    return lambda e: (e.get("event_type") == "decision" and e["payload"].get("kind") == "arbitration"
                      and e["payload"].get("rule") == rule)


CHECKS: list[tuple[str, str, Callable[[dict], bool]]] = [
    ("L1", "Discovery self-replan (discovery -> discovery replan)", _from("replan", "discovery")),
    ("L2", "critique revision_request (source=critique)", _is("revision_request", source="critique")),
    ("L3", "pushback revision_request (source=brand_reply)", _is("revision_request", source="brand_reply")),
    ("L4", "replacement request (reason=replacement)", _is("request", reason="replacement")),
    ("L5", "evidence escalation from roi_audit", _from("escalation", "roi_audit")),
    ("L6", "category feedback request (reason=category_feedback)", _is("request", reason="category_feedback")),
    ("L7", "infeasible request (reason=infeasible)", _is("request", reason="infeasible")),
    ("L8", "ROI calibration result (reason=calibration)", _is("result", reason="calibration")),
]
RULES = ["A1", "A2", "A3"]


def load(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"entries": data}


def matches(entries: list[dict]) -> dict[str, list[dict]]:
    found = {code: [e for e in entries if check(e)] for code, _, check in CHECKS}
    for rule in RULES:
        found[rule] = [e for e in entries if _arbitration(rule)(e)]
    return found


def _evidence_problems(doc: dict) -> list[str]:
    """Reasons this file cannot be submitted at all, whatever loops it shows."""
    entries = doc.get("entries", [])
    out = []
    if doc.get("rehearsal") or any(REHEARSAL_WORD in json.dumps(e.get("payload", {})).lower() for e in entries):
        out.append("contains rehearsal markers: this is a scripted rehearsal, not real reasoning")
    if not entries:
        out.append("no entries")
    return out


def problems(doc: dict) -> list[str]:
    """Everything that keeps this trace from showing the complete system (empty list = all good)."""
    found = matches(doc.get("entries", []))
    out = [f"missing {code}: {desc}" for code, desc, _ in CHECKS if not found[code]]
    if not any(found[r] for r in RULES):
        out.append("missing arbitration: no A1/A2/A3 decision entry")
    return out + _evidence_problems(doc)


def _brief(entry: dict) -> str:
    p = entry["payload"]
    message_id = f" message {p['id']}" if entry.get("event_type") == "message" and p.get("id") else ""
    return f"trace#{entry.get('id')}{message_id} @ {entry.get('ts') or p.get('ts')}"


def _reply_to(entries: list[dict], message_id: str) -> dict | None:
    """The first message that answers ``message_id`` (content.in_reply_to)."""
    for e in entries:
        m = _msg(e)
        if m and message_id in (m.get("content", {}).get("in_reply_to") or []):
            return e
    return None


def examples(doc: dict) -> dict[str, str]:
    """The three examples design.md cites: L3 pushback -> v2, A1 bounce vs ranking, A2 price cap."""
    entries, found = doc.get("entries", []), matches(doc.get("entries", []))
    out = {}
    if found["L3"]:
        req = found["L3"][0]
        answer = _reply_to(entries, req["payload"]["id"])
        c = req["payload"]["content"]
        out["L3"] = (f"Outreach revision_request {_brief(req)} for {c.get('brand_id')} "
                     f"(constraint: {c.get('constraint')!r}, max_price_inr: {c.get('max_price_inr')})")
        if answer:
            a = answer["payload"]["content"]
            out["L3"] += f" -> Proposal result {_brief(answer)}: v{a.get('version')} prices {a.get('prices')}"
    if found["A1"]:
        a1 = found["A1"][0]["payload"]
        out["A1"] = f"decision {_brief(found['A1'][0])}: {a1.get('conflict')} -> {a1.get('resolution')}"
    caps = [e for e in found["A2"] if str(e["payload"].get("resolution", "")).startswith("Cap")]
    for e in caps[:1] or found["A2"][:1]:
        out["A2"] = f"decision {_brief(e)}: {e['payload'].get('conflict')} -> {e['payload'].get('resolution')}"
    return out


def numbers(doc: dict) -> dict[str, int]:
    """Counts for the README demo script."""
    entries = doc.get("entries", [])
    found = matches(entries)
    decisions = [e["payload"] for e in entries if e.get("event_type") == "decision"]
    routes = [d for d in decisions if d.get("kind") == "route"]
    msgs = [m for m in map(_msg, entries) if m]
    clef = [d for d in decisions if d.get("kind") == "clef_judgement"]
    return {
        **{f"{code} occurrences": len(found[code]) for code, _, _ in CHECKS},
        **{f"{rule} arbitrations": len(found[rule]) for rule in RULES},
        "trace entries": len(entries),
        "agent messages": len(msgs),
        "supervisor route decisions": len(routes),
        **{f"routes decided by {k}": v for k, v in Counter(r.get("source") for r in routes).items()},
        "decision_log entries (route + arbitration + guardrail)": sum(
            1 for d in decisions if d.get("kind") in DECISION_LOG_KINDS),
        "human gate decisions": sum(1 for e in entries if e.get("event_type") == "resume"),
        # Clef judgements (L6/L7) are counted but not required, so traces from before Clef still pass.
        "clef judgements": len(clef),
        "clef judgements decided by clef": sum(1 for c in clef if c.get("source") == "clef"),
        "clef judgements decided by rule_fallback": sum(1 for c in clef if c.get("source") == "rule_fallback"),
        "clef escalations": sum(1 for c in clef if c.get("escalated")),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    doc = load(argv[1])
    found = matches(doc.get("entries", []))
    print(f"Trace {argv[1]}  run_id={doc.get('run_id')}  exported_at={doc.get('exported_at')}")
    print(f"note: {doc.get('note')}\n")
    for code, desc, _ in CHECKS:
        hits = found[code]
        print(f"  [{'x' if hits else ' '}] {code} {desc}: {len(hits)}" + (f"  first {_brief(hits[0])}" if hits else ""))
    for rule in RULES:
        hits = found[rule]
        print(f"  [{'x' if hits else ' '}] {rule} arbitration: {len(hits)}" + (f"  first {_brief(hits[0])}" if hits else ""))
    print("\nExamples for docs/design.md (Response to the CA2 review):")
    for key, text in examples(doc).items():
        print(f"  {key}: {text}")
    print("\nNumbers for the README demo script:")
    for key, value in numbers(doc).items():
        print(f"  {key}: {value}")
    not_fired = [code for code, _, _ in CHECKS if not found[code]] + [r for r in RULES if not found[r]]
    if not_fired:
        print("\nDid not fire in this run (drop these from the demo script): " + ", ".join(not_fired))
    issues = _evidence_problems(doc)
    print("\nRESULT: " + ("OK, usable as submission evidence" if not issues else "FAILED"))
    for issue in issues:
        print(f"  - {issue}")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

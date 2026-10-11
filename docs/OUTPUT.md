# Paytriq — output contracts

Four typed artefacts leave the system (`core/schemas.py`). Field lists below are
excerpts — the code is authoritative. After the schemas, one real captured sample is
quoted verbatim with its provenance.

## `MoU` (`core/schemas.py:343-357`)

A4's memorandum of understanding: `mou_id`, `event_id`, `brand`, `amount_inr` (≥ 0),
`terms` (non-empty clause text generated from the *accepted* offer's amount,
deliverables, tier, and version — never `proposals[0]`), `deliverables: list[str]`,
`status` (`draft` → `pending_approval` → `approved` / `signed` / `rejected`;
`approved` only via an MOU-gate `APPROVE`), `document_path | null`, `version` (≥ 1),
`created_at`.

## `ROIReport` (`core/schemas.py:369-395`)

A6's audit: `report_id`, `event_id`, `total_sponsored_inr`, `footfall`,
`estimated_leads`, `spend_inr`, `pipeline_value_inr`, `roi_multiple` (`0.0` with an
explicit "undefined" note when spend is zero — never a divide-by-zero multiple),
`assumptions: list[str]` (**validated non-empty**: an undocumented ROI is rejected by
the schema), `findings: list[AuditFinding]` (reused from A5, never recomputed),
`computed_at`.

## `Handoff` (`core/schemas.py:489-516`)

The core coordination record, written by every router: `handoff_id`, `event_id`,
`run_id`, `from_agent`, `to_agent` (must differ — self-handoffs are rejected),
`reason` (non-empty), `decision_source` (`clef` / `gemini` / `rules` / `replay`,
mandatory), `confidence` (0–1, mandatory), `summary`, `payload_refs` (board zone ids
the receiver should read), `at`.

## `TraceSummary` (`core/schemas.py:703-733`)

Derived statistics proving a trace is a genuine capture: `run_id`, `trace_file`,
`git_commit`, `code_sha256`, `event_count`, `span_count`, `root_span_count`,
`handoff_count`, `decision_counts`, `degraded_decision_count`, `wall_clock_ms`,
`consecutive_start_gaps_ms`, **`distinct_gap_values`** (many for a genuine capture,
exactly 1 for a hand-written trace with constant timing), `llm_call_count`,
`tool_call_count`, `tool_status_counts`, `human_gate_count`, `generated_at`.
Served live by `GET /api/runs/{run_id}/summary`.

## Real captured sample

The committed copy lives in `docs/samples/` (`golden_run.jsonl`,
`golden_summary.json`, provenance in `docs/samples/README.md`) — the one trace
artefact that ships with the repo, since `traces/` itself is gitignored.
Regenerate it with `python scripts/capture_canonical_trace.py` (real graph,
offline, simulated decision backend — never hand-written JSONL).

Provenance: `traces/canonical_20261008T174338Z_23288.summary.json` (+ `.board.json`
for the MoU/ROI below) plus event `seq 139` of
`traces/canonical_20261008T174338Z_23288.jsonl`, run
`canonical_20261008T174338Z_23288` — the single `TechFest 2026` demo event from
`docs/INPUT.md`. This run drives all 7 agents A1→A7 with
human SEND + MOU approvals granted (`decided_by="human:canonical-trace"`), a
contract-net award, a compliance dispute/veto, an MoU, and message/tool/llm
spans — the captured evidence is the summary, one routing handoff, the MoU, and
the ROI report:

```json
{
  "code_sha256": "uncommitted-working-tree",
  "decision_counts": {
    "clef": 88,
    "rules": 2
  },
  "degraded_decision_count": 0,
  "distinct_gap_values": 214,
  "event_count": 268,
  "git_commit": "uncommitted-working-tree",
  "handoff_count": 10,
  "human_gate_count": 5,
  "llm_call_count": 81,
  "message_count": 1,
  "root_span_count": 83,
  "run_id": "canonical_20261008T174338Z_23288",
  "sha": "uncommitted-working-tree",
  "span_count": 262,
  "tool_call_count": 15,
  "tool_status_counts": {
    "cached": 6,
    "ok": 1,
    "unavailable": 8
  },
  "trace_file": "canonical_20261008T174338Z_23288.jsonl",
  "wall_clock_ms": 479.803
}
```

```json
{
  "agent": "A1",
  "attributes": {
    "confidence": 0.82,
    "decision_source": "clef",
    "event_id": "evt_canonical",
    "from_agent": "A1",
    "handoff_id": "hnd_8f48072679",
    "payload_refs": [
      "brd_canonical_1",
      "brd_canonical_2",
      "brd_canonical_3",
      "brd_canonical_4"
    ],
    "reason": "4 viable lead(s) at 10.0 km meets the threshold of 3",
    "run_id": "canonical_20261008T174338Z_23288",
    "summary": "task ledger revision 0 priced",
    "to_agent": "A2"
  },
  "duration_ms": null,
  "event_id": "evt_canonical",
  "kind": "handoff",
  "name": "A1->A2",
  "parent_span_id": null,
  "run_id": "canonical_20261008T174338Z_23288",
  "seq": 139,
  "span_id": "08a53ea7c6e8cd54",
  "status": "ok",
  "trace_id": "88a04a049b69c76ed108cfff552a94e7",
  "ts": "2026-10-08T17:43:39.113475Z"
}
```

Notes on reading it: `distinct_gap_values: 214` over 267 gaps is the anti-fabrication
signal (irregular timing, not a constant cadence); `handoff_count: 10` with
`message_count: 1`, `tool_call_count: 15`, `llm_call_count: 81` and
`human_gate_count: 5` matches a full A1→A7 run with award, dispute, MoU and
human-granted SEND/MOU gates; `sha` is `uncommitted-working-tree` because the
repo has zero commits — CI/Docker/Render inject the real SHA at
build time. The handoff names its author (`A1 → A2`), its reason, its decision
source (`clef` from the simulated backend, confidence `0.82`), and the board
refs the receiver should read. Recompute with
`GET /api/runs/{run_id}/summary` and `GET /api/runs/{run_id}/trace` on any new run.

## MoU (captured, same run)

Board snapshot `traces/canonical_20261008T174338Z_23288.board.json` (`mous[0]`,
internal bookkeeping fields omitted; absolute `document_path` reduced to the
bare filename as with the trace sanitization). Release was approved at the MOU
gate (`decided_by="human:canonical-trace"`); the terms are generated from the
accepted offer's amount, deliverables, tier and version — never `proposals[0]`:

```json
{
  "mou_id": "mou_7ca6da9972",
  "event_id": "evt_canonical",
  "brand": "FIXTURE_Canonical Books Two",
  "amount_inr": 45000.0,
  "terms": "MEMORANDUM OF UNDERSTANDING\nReference: offer off_canonical_2 version 1 (silver tier)\nOrganiser: TechFest 2026\nEvent: TechFest 2026 at VIT Pune, Karad Road on 2026-02-14\nExpected audience: Second and third year engineering students, 62% male, hiring-season heavy (approx. 5,000 attendees)\nSponsor: FIXTURE_Canonical Books Two\n\n1. SPONSORSHIP FEE. The Sponsor shall pay the Organiser INR 45,000 (Indian Rupees 45000) for the silver tier of sponsorship described in clause 5.\n\n2. PAYMENT SCHEDULE. 50% of the sponsorship fee is payable within 7 days of signature of this MoU; the remaining 50% is payable within 7 days of the event date.\n\n3. LOGO USAGE RIGHTS. The Sponsor's logo may appear only on the collateral listed in clause 5 and only for the duration of the event. The Organiser will not alter the logo's proportions or colours, will not imply endorsement beyond the agreed tier, and will remove the logo from materials published after the event on request. Any use of the Organiser's name or marks by the Sponsor requires the Organiser's prior written consent.\n\n4. CANCELLATION AND NOTICE. Either party may terminate this MoU by 14 days' written notice. If the Organiser cancels, any amount already paid is refunded less costs already incurred and non-refundable third-party charges. If the Sponsor cancels, amounts paid are non-refundable. If the event is postponed beyond 90 days or cancelled, the parties will renegotiate in good faith and any unspent advance will be returned.\n\n5. DELIVERABLES. The Organiser shall provide:\n   1. stall slot\n   2. instagram reel\n   Photo or written evidence of each delivered item will be shared within 7 days of the event.\n\n6. EXCLUSIVITY. No exclusivity is granted under this MoU. The Organiser may enter into other sponsorships at the same tier unless a written exclusivity addendum is signed by both parties.\n\n7. STATUS. This MoU records the parties' agreement on the terms above. It becomes binding on signature by both parties.\n\nSIGNATURES\n   For the Organiser (TechFest 2026): ______________________  Name: ________________  Date: ____________\n   For the Sponsor (FIXTURE_Canonical Books Two): ______________________  Name: ________________  Date: ____________",
  "deliverables": [
    "stall slot",
    "instagram reel"
  ],
  "status": "pending_approval",
  "document_path": "mou_7ca6da9972.pdf",
  "version": 1,
  "created_at": "2026-10-08T17:43:39.484690Z"
}
```

## ROI report (captured, same run)

Same board snapshot (`roi_report`). The audit ran after the escalation with no
signed MoU in its scope, so A6 reported the honest empty path — every figure 0
with a non-empty assumptions list explaining why, `roi_multiple` 0.0 with an
explicit "undefined" note instead of a divide-by-zero multiple. That is the
documented schema behavior, quoted here as captured rather than as a success
it is not:

```json
{
  "report_id": "roi_348bb57f87",
  "event_id": "evt_canonical",
  "total_sponsored_inr": 0.0,
  "footfall": 0,
  "estimated_leads": 0,
  "spend_inr": 0.0,
  "pipeline_value_inr": 0.0,
  "roi_multiple": 0.0,
  "assumptions": [
    "no signed MoU was present in zone `contracts` when this audit ran, so every figure is 0 and no rate was applied",
    "roi_multiple is undefined: with zero spend and zero revenue any multiple would be fabrication, so it is reported as 0.0 with this note",
    "scope decision: 0 signed MoU(s) [], 2 A5 finding(s); decision chose stop_no_inputs",
    "decided bases: spend_basis=apply_15pct_spend_ratio, lead_basis=apply_configured_conversion, compliance_reading=report_recomputed_pct, lesson_focus=evidence_gap; provenance: a6 plan decisions still recorded on the empty path.",
    "computed_at=2026-10-08T17:43:39.531862+00:00 for run canonical_20261008T174338Z_23288"
  ],
  "findings": [],
  "computed_at": "2026-10-08T17:43:39.531899Z"
}
```

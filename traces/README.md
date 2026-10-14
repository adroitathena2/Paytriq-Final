# Execution traces

This folder holds traces exported from **live** Paytriq runs on the deployed system: Trace page, then
**Export JSON**, or `GET /runs/{run_id}/trace/export`. Commit the downloaded file here unchanged.

Rules (see the CA3 guidelines on academic integrity):

- Only commit files produced by the export endpoint of the deployed system.
- Never edit, trim, merge or hand-write a trace. If a run went wrong, do a new run and export that one.
- Name files `YYYY-MM-DD-<short-description>.json`. A demo-mode run is named
  `YYYY-MM-DD-demo-mode-full-run.json`, so the name says it ran in the fixture world (real Gemini reasoning).
- **Never commit a rehearsal trace.** Rehearsal runs use a scripted LLM; their exports are named
  `REHEARSAL-NOT-FOR-SUBMISSION-...` and every entry carries `"rehearsal": "rehearsal — not real reasoning"`.

Check a trace before committing it:

```bash
python backend/scripts/verify_trace.py traces/YYYY-MM-DD-demo-mode-full-run.json
```

A complete demo run shows every coordination loop and arbitration rule. The Trace page marks each one:

| Marker | What to look for in the trace |
|---|---|
| L1 self-replan | `discovery -> discovery` message of type `replan` |
| L2 critique | `outreach -> proposal` `revision_request` with `content.source = "critique"` |
| L3 pushback | `outreach -> proposal` `revision_request` with `content.source = "brand_reply"` |
| L4 replacement | `-> discovery` `request` with `content.reason = "replacement"` |
| L5 evidence | `roi_audit -> human` `escalation` |
| L6 category feedback | `outreach -> discovery` `request` with `content.reason = "category_feedback"` |
| L7 infeasible | `proposal -> discovery` `request` with `content.reason = "infeasible"` |
| L8 calibration | `roi_audit -> proposal` `result` with `content.reason = "calibration"` |
| A1 bounce beats ranking | event_type `decision`, `payload.kind = "arbitration"`, `payload.rule = "A1"` |
| A2 price vs past deals | event_type `decision`, `payload.kind = "arbitration"`, `payload.rule = "A2"` (cap, or "insufficient data") |
| A3 ROI gaps block publishing | event_type `decision`, `payload.kind = "arbitration"`, `payload.rule = "A3"`, plus a `supervisor -> human` `escalation` with `content.reason = "missing_proof"` |

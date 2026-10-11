# Execution traces

This folder holds traces exported from **live** Paytriq runs: Trace page, then **Export JSON**, or
`GET /runs/{run_id}/trace/export`. Commit the downloaded file here unchanged.

Rules (see the CA3 guidelines on academic integrity):

- Only commit files produced by the export endpoint of the deployed system.
- Never edit, trim, merge or hand-write a trace. If a run went wrong, do a new run and export that one.
- Name files `YYYY-MM-DD-<short-description>.json`, e.g. `2026-11-02-techfest-full-run.json`.

A complete demo run shows every coordination loop. The Trace page marks each one:

| Loop | What to look for in the trace |
|---|---|
| L1 self-replan | `discovery -> discovery` message of type `replan` |
| L2 critique | `outreach -> proposal` `revision_request` with `content.source = "critique"` |
| L3 pushback | `outreach -> proposal` `revision_request` with `content.source = "brand_reply"` |
| L4 replacement | `outreach -> discovery` `request` with `content.reason = "replacement"` |
| L5 evidence | `roi_audit -> human` `escalation`, followed by the supervisor's arbitration `decision` |

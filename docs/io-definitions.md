# Input and output definitions

All endpoints take and return JSON (evidence upload is `multipart/form-data`) and require
`Authorization: Bearer <Supabase access token>`. Samples: [sample_input.json](sample_input.json) and
[sample_output.json](sample_output.json). The authoritative schemas are the Pydantic models referenced below.

## Inputs

### 1. Event (`POST /events`, `EventCreate` in `backend/app/api/events.py`)

| Field | Type | Rules |
|---|---|---|
| `name` | string | 2 to 120 chars, not blank |
| `college` | string | 2 to 160 chars |
| `event_date` | date (`YYYY-MM-DD`) | required |
| `expected_footfall` | integer | 10 to 200,000 |
| `audience` | string | 3 to 500 chars; who attends (used for fit scoring and pitches) |
| `budget_goal_inr` | integer | 0 to 100,000,000 |
| `lat`, `lng` | number | -90..90, -180..180 (campus location; the search centre) |

Sample:

```json
{
  "name": "Fergusson Fest 2026", "college": "Fergusson College, Pune", "event_date": "2026-12-12",
  "expected_footfall": 1800,
  "audience": "18-22 year old arts, science and commerce students; many live in nearby PGs and hostels",
  "budget_goal_inr": 120000, "lat": 18.5236, "lng": 73.8394
}
```

This is also the demo event (`backend/app/demo/world.py`, `EVENT`), so the sample input and the sample output
describe the same event. `POST /events/{event_id}/runs` starts the agents (202, returns the run row).

### 2. Human-in-the-loop decision (`POST /runs/{run_id}/approve`, `ApprovalRequest`)

```json
{ "gate": "shortlist | send | roi_publish", "decision": "approve | reject", "edits": {} }
```

| Gate | `edits` on approve | `edits` on reject |
|---|---|---|
| `shortlist` | `selected_brand_ids: string[]` (defaults to the suggested ones) | `feedback: string` (becomes a request to Discovery) |
| `send` | `exclude: string[]`, `emails: {brand_id: {subject, body}}` | `feedback: string` (becomes revision requests to Proposal) |
| `roi_publish` | `note: string` (optional) | none (run waits for more evidence) |

Returns 409 if the run is not waiting for that gate.

### 3. External triggers

- `POST /runs/{run_id}/poll-replies`: posts `{"action": "poll_replies"}` from the organizer to Outreach.
- `POST /runs/{run_id}/evidence` (multipart): `brand_id` (required), `note`, and exactly one of `file`
  (JPEG/PNG/WebP, <= 8 MB) or `link` (http/https URL).
- `POST /runs/{run_id}/audit`: posts `{"action": "audit"}` to ROI Audit with all uploaded evidence. Optional JSON
  body `{"actual_footfall": int | null}` (10 to 200,000), used for L8 reach calibration.
- `POST /runs/{run_id}/publish-with-gaps`: the organizer's explicit choice to publish an ROI report whose proof is
  incomplete (arbitration A3). Only allowed in phase `roi_blocked` (else 409); the normal `roi_publish` approval
  gate still follows.
- `POST /runs/{run_id}/continue`: resumes routing after a failure or restart.

### 4. Demo mode

- `POST /events/demo` with `{"rehearsal": bool}` creates the fixture-world demo event (`demo_scenario` `demo`:
  real Gemini reasoning; `rehearsal`: scripted LLM, every trace entry marked, export named
  `REHEARSAL-NOT-FOR-SUBMISSION-...`). It also resets the organizer's seeded past deals.
- `POST /runs/{run_id}/demo-evidence` (demo events only, else 409) adds generated photos for every accepted
  sponsor's deliverables except one left out on purpose; returns `{"added": n}`.

## Outputs

### 1. Run state (`GET /runs/{run_id}`)

```json
{ "run": RunRow, "values": EventState, "interrupt": { "gate": "...", "payload": {...} } | null }
```

`RunRow`: `id, event_id, owner_id, status (running | waiting_approval | idle | completed | failed | stalled),
phase, pending_gate, last_error, created_at, updated_at`.

`EventState` (`backend/app/graph/state.py`):

| Field | Type |
|---|---|
| `event_id` | string |
| `event` | `{name, college, date, expected_footfall, audience, budget_goal_inr, lat, lng, demo_scenario}` |
| `brands` | `Brand[]`: `id, name, category, address, lat, lng, distance_km, contact_email?, website?, fit_score (0-1), fit_reason, unreachable` (A1) |
| `selected_brand_ids` | string[] |
| `proposals` | `{brand_id: Proposal}`: `brand_id, version, tiers: Tier[], pitch, pdf_url?, status (draft, needs_revision, on_hold, infeasible, ready, sent, accepted, declined), reach_note?` (L8, e.g. "estimate calibrated from 2 past events") |
| `Tier` | `name (Bronze, Silver, Gold), price_inr, deliverables: string[], expected_reach` |
| `outreach` | `{brand_id: OutreachThread}`: `brand_id, thread_id?, status (draft, sent, replied, nudged, accepted, declined, pushback, bounced), last_reply_class?, history: object[]` |
| `evidence` | evidence rows given to the last audit |
| `roi_report` | `RoiReport` or null (below) |
| `messages` | `AgentMessage[]`: `id, from_agent, to_agent, type (request, result, critique, revision_request, replan, escalation, decision), content, reasoning, ts` |
| `pending_approval` | `{gate, status, payload}` or null |
| `phase` | string (see `frontend/src/lib/format.ts` for all phases) |
| `step_count` | integer (supervisor steps in this segment) |
| `errors` | `{agent, tool, error}[]` |
| `decision_log` | supervisor record, one entry per routing decision, guardrail hit or arbitration: `{ts, kind (route, guardrail, arbitration), next, reason, source (llm, fallback, arbitration, code), ...}`. Route entries add `gate, guardrail, step`; guardrail entries add `rule` (`max_steps`, `empty_gate`, `pair_loop`, `revision_cap`) and `proposed`; arbitration entries add `rule (A1, A2, A3), key, conflict, agents: string[], resolution, justification` (A2 without enough history: `resolution: "insufficient data"`) |
| `feedback` | `{brand_id: outcome[]}` outreach outcomes per brand (`accepted`, `declined`, `bounced`, `silent`) |
| `revision_count` | `{brand_id: int}` agent-initiated revision requests let through to Proposal (max 2; a third puts the proposal `on_hold` and escalates to the organizer; organizer requests are not counted) |
| `publish_with_gaps` | bool, set by `POST /runs/{id}/publish-with-gaps` (A3) |
| `target_brand_count` | int, shortlist size the organizer approved (L6 tops up to it) |
| `actual_footfall` | int or null, from the audit request (L8) |

`interrupt.payload` by gate: `shortlist` gives `{suggested_brand_ids, brands}`; `send` gives `{emails: [{brand_id,
brand_name, kind (initial, revised, nudge), subject, body, version, pdf_url}]}`; `roi_publish` gives `{report}`.

### 2. ROI report (`GET /runs/{run_id}/report`)

```json
{
  "event": {"name": "...", "college": "...", "date": "...", "expected_footfall": 1800},
  "actual_footfall": 1500,
  "generated_at": "ISO-8601",
  "sponsors": [{
    "brand_id": "...", "name": "...", "category": "coaching", "tier": "Gold", "price_inr": 12500,
    "expected_reach": 2970, "deliverables": ["..."],
    "checks": [{"deliverable": "...", "status": "verified | not_verified | ambiguous | missing",
                "confidence": 0.95, "explanation": "...", "evidence_ids": ["..."]}],
    "delivered": 1, "total": 3, "delivery_rate": 0.33,
    "calibration": {"category": "coaching", "factor": 0.43, "verified_reach": 1270, "estimated_reach": 2970,
                    "footfall_used": 1500, "footfall_source": "actual | expected"}
  }],
  "overall_delivery_rate": 0.33,
  "disclosed_gaps": [{"brand_id": "...", "brand_name": "...", "deliverable": "...", "status": "...", "explanation": "..."}],
  "evidence_requests": ["Upload a photo or link showing 'Stall' for ..."],
  "summary": "...",
  "published": true,
  "published_with_gaps": true,
  "organizer_note": "..."
}
```

`calibration.factor` = reach of the verified deliverables at `footfall_used` (the actual footfall if given, else the
expected one) / the estimated reach. It is only saved to `reach_calibration` (running average per organizer and
category) when the organizer approves publishing; `published_with_gaps` records whether gaps were disclosed (A3).

### 3. Execution trace (`GET /runs/{run_id}/trace`, `/trace/export`)

Each entry: `{id, run_id, ts, agent, event_type, payload}` with `event_type` one of `run_start, run_end, message,
tool_call, tool_result, tool_error, llm_error, guardrail, decision, interrupt, resume, error`. For `message` the payload
is the `AgentMessage`; for `tool_call` it is `{tool, args, reasoning}`; for `decision` it is the `decision_log` entry
(agent `supervisor`) or a Clef judgement (agent `proposal` or `outreach`, below).
The export wraps the entries as `{run_id, event_id, exported_at, note, demo_scenario, rehearsal, entries}`. In
rehearsal runs every payload also carries `"rehearsal": "rehearsal — not real reasoning"` and the export file is
named `REHEARSAL-NOT-FOR-SUBMISSION-paytriq-trace-<run_id>.json`.

**Clef judgement entry** (L6, L7; `backend/app/core/clef.py`). One `decision` event per judgement, `agent` =
`proposal` (L7) or `outreach` (L6):

```json
{"kind": "clef_judgement", "purpose": "proposal.feasibility | outreach.category_feedback",
 "question": "...", "p_yes": 0.08, "threshold_hi": 0.75, "threshold_lo": 0.25,
 "answer": false, "source": "clef | rule_fallback", "escalated": false, "rule_answer": false,
 "error": "only when source is rule_fallback, e.g. ClefError: CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN not set"}
```

`p_yes` is null when Clef could not answer. `p_yes >= threshold_hi` gives yes and `<= threshold_lo` gives no. In
between, `answer = rule_answer`, `escalated = true`, and the agent posts an `escalation` to `human` with
`content = {reason: "clef_unsure", purpose, p_yes, applied, brand_id | category}`. The `category_feedback` request
carries `p_yes` and `decided_by` (`clef` or `rule_fallback`). So does an `infeasible` request when the brand-fit
judgement decided; the cost-per-reach half of L7 is plain code and adds neither. When Clef says to keep a
category, Outreach's step `result` to the supervisor carries `content.category_checks = {category: negative
outcomes at that time}`, so it asks again only after a new negative.

How each coordination loop and arbitration rule appears in `messages` and in the trace:

| Marker | Where |
|---|---|
| L1 | message `discovery -> discovery`, type `replan` |
| L2 / L3 | `outreach -> proposal` `revision_request`, `content.source` = `critique` / `brand_reply` |
| L4 | `-> discovery` `request`, `content.reason = "replacement"` (from Outreach, or from the supervisor with `content.arbitration = "A1"`) |
| L5 | `roi_audit -> human` `escalation` |
| L6 | `outreach -> discovery` `request`, `content.reason = "category_feedback"`, `content.category`, `content.evidence`, `content.p_yes`, `content.decided_by`; preceded by an `outreach` `decision` with `kind = "clef_judgement"` |
| L7 | `proposal -> discovery` `request`, `content.reason = "infeasible"`, `content.why` (plus `p_yes`, `decided_by` for a brand-fit rejection); preceded by a `proposal` `decision` with `kind = "clef_judgement"` |
| Clef unsure | `proposal` or `outreach -> human` `escalation`, `content.reason = "clef_unsure"` |
| L8 | `roi_audit -> proposal` `result`, `content.reason = "calibration"`, `{category, event_factor, factor, n_events}` |
| A1-A3 | trace event `decision` with `payload.kind = "arbitration"` and `payload.rule`; A2 also posts a `supervisor -> proposal` `revision_request` with `content.source = "arbitration"`; A3 posts a `supervisor -> human` `escalation` with `content.reason = "missing_proof"` |
| Revision cap | `supervisor -> human` `escalation`, `content.reason = "revision_cap"` |

### Sample output provenance

> **[[TO FILL after the deployed demo-mode run (B5); do not fill from memory]]**
> `sample_output.json` is the real `GET /runs/{run_id}` response captured from the deployed system at the end of the
> run whose trace is committed in [`/traces`](../traces).
>
> - run_id: `[[RUN_ID]]`
> - date: `[[YYYY-MM-DD]]`
> - deployed API URL: `[[https://<api>.onrender.com]]`
> - model: `gemini-flash-latest`
> - demo mode: fixture businesses and inbox, real Gemini reasoning
>
> Until it is replaced, `sample_output.json` still holds a response produced by the offline integration test with
> scripted fakes (its `_provenance` field says so); it shows the shape only.

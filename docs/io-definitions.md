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

`POST /events/{event_id}/runs` starts the agents (202, returns the run row).

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
- `POST /runs/{run_id}/audit`: posts `{"action": "audit"}` to ROI Audit with all uploaded evidence.
- `POST /runs/{run_id}/continue`: resumes routing after a failure or restart.

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
| `event` | `{name, college, date, expected_footfall, audience, budget_goal_inr, lat, lng}` |
| `brands` | `Brand[]`: `id, name, category, address, lat, lng, distance_km, contact_email?, website?, fit_score (0-1), fit_reason` |
| `selected_brand_ids` | string[] |
| `proposals` | `{brand_id: Proposal}`: `brand_id, version, tiers: Tier[], pitch, pdf_url?, status (draft, needs_revision, ready, sent, accepted, declined)` |
| `Tier` | `name (Bronze, Silver, Gold), price_inr, deliverables: string[], expected_reach` |
| `outreach` | `{brand_id: OutreachThread}`: `brand_id, thread_id?, status (draft, sent, replied, nudged, accepted, declined, pushback, bounced), last_reply_class?, history: object[]` |
| `evidence` | evidence rows given to the last audit |
| `roi_report` | `RoiReport` or null (below) |
| `messages` | `AgentMessage[]`: `id, from_agent, to_agent, type (request, result, critique, revision_request, replan, escalation, decision), content, reasoning, ts` |
| `pending_approval` | `{gate, status, payload}` or null |
| `phase` | string (see `frontend/src/lib/format.ts` for all phases) |
| `step_count` | integer (supervisor steps in this segment) |
| `errors` | `{agent, tool, error}[]` |

`interrupt.payload` by gate: `shortlist` gives `{suggested_brand_ids, brands}`; `send` gives `{emails: [{brand_id,
brand_name, kind (initial, revised, nudge), subject, body, version, pdf_url}]}`; `roi_publish` gives `{report}`.

### 2. ROI report (`GET /runs/{run_id}/report`)

```json
{
  "event": {"name": "...", "college": "...", "date": "...", "expected_footfall": 1800},
  "generated_at": "ISO-8601",
  "sponsors": [{
    "brand_id": "...", "name": "...", "tier": "Gold", "price_inr": 20000, "expected_reach": 2480,
    "deliverables": ["..."],
    "checks": [{"deliverable": "...", "status": "verified | not_verified | ambiguous | missing",
                "confidence": 0.95, "explanation": "...", "evidence_ids": ["..."]}],
    "delivered": 1, "total": 3, "delivery_rate": 0.33
  }],
  "overall_delivery_rate": 0.33,
  "disclosed_gaps": [{"brand_id": "...", "brand_name": "...", "deliverable": "...", "status": "...", "explanation": "..."}],
  "evidence_requests": ["Upload a photo or link showing 'Stall' for ..."],
  "summary": "...",
  "published": true
}
```

### 3. Execution trace (`GET /runs/{run_id}/trace`, `/trace/export`)

Each entry: `{id, run_id, ts, agent, event_type, payload}` with `event_type` one of `run_start, run_end, message,
tool_call, tool_result, tool_error, llm_error, guardrail, interrupt, resume, error`. For `message` the payload is the
`AgentMessage`; for `tool_call` it is `{tool, args, reasoning}`. The export wraps the entries as
`{run_id, event_id, exported_at, note, entries}`.

### Sample output provenance

`sample_output.json` was produced by the offline integration test (scripted fake LLM and tools). It shows the
exact shape of `GET /runs/{run_id}` at the end of a run that went through all five coordination loops. It will be
replaced with a response captured from the deployed system; real traces go in [`/traces`](../traces).

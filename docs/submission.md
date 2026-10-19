# Paytriq — Submission: Input definition, Output definition, Execution trace

Live demo-mode run on the deployed system (`https://paytriq-api.onrender.com`,
frontend `https://paytriq-aryan-7032.vercel.app`), model `gemini-flash-latest`,
7/7 Clef judgements decided by real Clef (zero rule fallbacks).

- run_id: `e160d171-213f-4076-b9c5-dab56e7c04d2`
- date: `2026-10-19`
- Full input/output schemas: `docs/io-definitions.md`
- Machine-readable samples: `docs/sample_input.json`, `docs/sample_output.json`
  (real `GET /runs/{run_id}` response from this run, 92 KB)
- Unedited trace export (264 entries): `traces/2026-10-19-demo-mode-full-run.json`

How this run went: demo event (Fergusson Fest 2026) → Discovery → shortlist gate
(approved demo-chai-junction, demo-bean-theory, demo-ironcore-gym,
demo-pulsefit-gym, demo-apex-coaching) → proposals with PDF for every feasible
brand → send gate → 2 reply polls (1 bounced, 1 pushback→v2→accepted, 2
declined, 1 accepted; bounced top cafe replaced by Metro Wholesale, refused as
infeasible and replaced by Crumbs Bakery, which also accepted) → 10 demo evidence photos → audit → publishing blocked → publish with
gaps disclosed → report approved and published. 4 human gates, 24 supervisor
routes (22 by LLM), 62 agent messages, 33 decision-log entries.

## 1. Input definition

All endpoints take and return JSON (evidence upload is `multipart/form-data`)
with `Authorization: Bearer <Supabase access token>`.

| Call | Rules |
|---|---|
| `POST /events` | `name` 2–120 chars; `college` 2–160; `event_date` `YYYY-MM-DD`; `expected_footfall` 10–200,000; `audience` 3–500 chars (drives fit scoring and pitches); `budget_goal_inr` 0–100,000,000; `lat` −90..90, `lng` −180..180 |
| `POST /events/demo` `{"rehearsal": bool}` | fixture-world demo (`demo`: real Gemini) or scripted rehearsal (never for submission) |
| `POST /events/{id}/runs` | starts the agents (202) |
| `POST /runs/{id}/approve` | `{gate: shortlist\|send\|roi_publish, decision: approve\|reject, edits}` — shortlist: `selected_brand_ids`; send: `exclude`, per-brand email edits; `roi_publish`: optional `note`. 409 unless waiting on that gate |
| `POST /runs/{id}/poll-replies` | check Gmail threads now |
| `POST /runs/{id}/evidence` (multipart) | `brand_id` + exactly one of `file` (JPEG/PNG/WebP ≤ 8 MB) or `link` (http/https) |
| `POST /runs/{id}/demo-evidence` | generated photos for accepted sponsors, one deliverable deliberately missing |
| `POST /runs/{id}/audit` `{"actual_footfall": int\|null}` | verify deliverables with Gemini Vision; footfall feeds L8 calibration |
| `POST /runs/{id}/publish-with-gaps` | only in phase `roi_blocked` (A3); normal `roi_publish` approval still follows |

Sample input (also `docs/sample_input.json`):

```json
{
  "create_event": {
    "endpoint": "POST /events",
    "body": {
      "name": "Fergusson Fest 2026",
      "college": "Fergusson College, Pune",
      "event_date": "2026-12-12",
      "expected_footfall": 1800,
      "audience": "18-22 year old arts, science and commerce students; many live in nearby PGs and hostels",
      "budget_goal_inr": 120000,
      "lat": 18.5236,
      "lng": 73.8394
    }
  },
  "create_demo_event": {
    "endpoint": "POST /events/demo",
    "body": { "rehearsal": false }
  },
  "start_run": { "endpoint": "POST /events/{event_id}/runs", "body": null },
  "approve_shortlist": {
    "endpoint": "POST /runs/{run_id}/approve",
    "body": {
      "gate": "shortlist",
      "decision": "approve",
      "edits": {
        "selected_brand_ids": ["demo-chai-junction", "demo-bean-theory", "demo-ironcore-gym",
                               "demo-metro-wholesale", "demo-pulsefit-gym", "demo-apex-coaching"]
      }
    }
  },
  "poll_replies": { "endpoint": "POST /runs/{run_id}/poll-replies", "body": null },
  "run_audit": {
    "endpoint": "POST /runs/{run_id}/audit",
    "body": { "actual_footfall": 1500 }
  },
  "publish_with_gaps": {
    "endpoint": "POST /runs/{run_id}/publish-with-gaps",
    "body": null
  },
  "publish_report": {
    "endpoint": "POST /runs/{run_id}/approve",
    "body": { "gate": "roi_publish", "decision": "approve", "edits": {} }
  }
}
```

## 2. Output definition

`GET /runs/{run_id}` returns `{run, values, interrupt}`. `run`: id, status
(`completed` here), phase (`done`), pending gate, timestamps. `values`
(`EventState`): `brands` with fit scores; `proposals` per brand (tiers, pitch,
`pdf_url`, `status`, `reach_note`); `outreach` threads
(draft/sent/replied/accepted/declined/pushback/bounced + reply class);
`evidence`; `roi_report`; `messages` (the inter-agent bus);
`decision_log` (every supervisor route, guardrail hit, arbitration);
`feedback`, `revision_count`, `publish_with_gaps`, footfall fields.
`GET /runs/{id}/report` returns the ROI report; `GET /runs/{id}/trace` and
`/trace/export` return `{id, run_id, ts, agent, event_type, payload}` entries.

Real excerpts from this run (full response: `docs/sample_output.json`):

```json
{
  "proposal_demo_apex_coaching": {
    "version": 2,
    "tiers": [
      { "name": "Bronze", "price_inr": 6000, "expected_reach": 1980 },
      { "name": "Silver", "price_inr": 9000, "expected_reach": 2430 },
      { "name": "Gold", "price_inr": 12000, "expected_reach": 1530 }
    ],
    "status": "accepted",
    "pdf_url": "https://essravfkemrewdubjtau.supabase.co/storage/v1/object/public/proposals/e160d171-213f-4076-b9c5-dab56e7c04d2/demo-apex-coaching-v2.pdf"
  },
  "proposal_demo_metro_wholesale": { "status": "infeasible", "pdf_url": null },
  "roi_report": {
    "actual_footfall": 1500,
    "published": true,
    "published_with_gaps": true,
    "overall_delivery_rate": 0.0,
    "sponsors": [
      { "name": "Bean Theory Cafe", "tier": "Silver", "price_inr": 5000 },
      { "name": "Apex Coaching Classes", "tier": "Gold", "price_inr": 12000 },
      { "name": "Crumbs & Co Bakery", "tier": "Bronze", "price_inr": 5000 }
    ],
    "organizer_note": null
  }
}
```

The generated demo photos read as stylized graphics, so Gemini Vision marked every check ambiguous
(`overall_delivery_rate` 0.0); every gap is listed in the report's `disclosed_gaps`.

Brand outcomes in this run: Bean Theory accepted Silver v2 (Rs 8,000 cap honoured),
Apex Coaching accepted Gold v2 (capped at Rs 12,500 by A2 (Rs 48,000 asked); v2 priced
Gold at Rs 12,000); Chai Junction bounced and was replaced by Metro Wholesale Traders (L4),
which Proposal refused as infeasible (L7) with no PDF rendered; Crumbs Bakery, the L7
replacement, accepted Bronze; both gyms declined.

## 3. Execution trace

File: `traces/2026-10-19-demo-mode-full-run.json` — downloaded via the Trace page
Export (equivalent: `GET /runs/{id}/trace/export`), committed unchanged.
Verified with `python backend/scripts/verify_trace.py` (same counts the README
demo script uses).

| Marker | This run | Evidence (first occurrence) |
|---|---|---|
| L1 Discovery self-replan | — did not fire (5 good fits within 3 km (searched 2 km then 3 km), no replan needed) | — |
| L2 Outreach critique → Proposal v2 | — did not fire (no critique this run) | — |
| L3 pushback → Proposal v2 | 1× | msg `1c395e902b4b`: Bean Theory "Rs 8,000 max" → v2 Gold 8000 / Bronze 3000 / Silver 5000 |
| L4 decline/bounce → replacement | 1× | msg `16aa8c5c9241`: bounced top cafe replaced by Metro Wholesale (then L7 → Crumbs Bakery) |
| L5 missing evidence → escalation | 1× | msg `eaedfc5e597d`: ROI Audit asked for the specific missing photo |
| L6 category feedback | — did not fire | — |
| L7 infeasible → Discovery | 1× | msg `92dc1b2a2083`: Metro Wholesale refused, no PDF |
| L8 reach calibration | 3× | saved per category on publish for future proposals (applies to later proposals, so none in this run carry `reach_note` yet) |
| A1 bounce beats ranking | 1× | trace#657: Chai Junction (fit 0.95) bounced → marked unreachable |
| A2 price vs past deals | 6× | trace#602: Apex capped Rs 48,000 → Rs 12,500 (1.25 × Rs 10,000 median) |
| A3 ROI gaps block publishing | 2× | publishing blocked, released only via explicit publish-with-gaps + approval |

Per the README rule, L1/L2/L6 get no narrative sentence since they did not fire
in this real trace — the run is presented as is, unedited.

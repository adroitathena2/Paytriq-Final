# Paytriq — Submission: Input definition, Output definition, Execution trace

Live demo-mode run on the deployed system (`https://paytriq-api.onrender.com`,
frontend `https://paytriq-aryan-7032.vercel.app`), model `gemini-flash-latest`,
9/9 Clef judgements decided by real Clef (zero rule fallbacks).

- run_id: `4961225e-639a-4fad-a50d-8097ab27fe1e`
- date: `2026-10-21`
- Full input/output schemas: `docs/io-definitions.md`
- Machine-readable samples: `docs/sample_input.json` (every organizer input of this run, in order, each with the
  trace entry it came from), `docs/sample_output.json` (real `GET /runs/{run_id}` response from this run, 171 KB)
- Unedited trace export (437 entries): `traces/2026-10-21-demo-mode-full-run.json`

How this run went: demo event (Fergusson Fest 2026) → Discovery replanned from 2 km to 4 km (L1) → shortlist gate
(approved demo-chai-junction, demo-apex-coaching, demo-bean-theory, demo-pulsefit-gym, demo-crumbs-bakery) →
proposals with PDF (A2 capped Apex Coaching; Outreach's critique sent PulseFit back for v2) → send gate → reply polls:
Chai Junction bounced (A1, L4), PulseFit declined (L4), Bean Theory pushed back on price (L3 → v2 → accepted), Apex
and Crumbs accepted → replacements Glow Studio Salon (accepted) and IronCore Gym (critiqued to v2, then declined) →
two gyms negative, Clef said stop prioritising gyms (L6) → replacement Page Turner Books (sent, nudged, no reply) →
14 demo evidence photos → audit (L5: 0 of 15 deliverables verified) → publishing blocked (A3) → publish with gaps
disclosed → report approved and published (L8 calibration for 4 categories). 6 human gate decisions, 43 supervisor
routes (41 by LLM), 110 agent messages, 54 decision-log entries.

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

Sample input, real excerpt from this run (all 17 inputs, including the 4 send approvals with the organizer's email
edits: `docs/sample_input.json`):

```json
[
  { "step": "create_demo_event", "endpoint": "POST /events/demo", "body": { "rehearsal": false },
    "event_created": {
      "name": "Fergusson Fest 2026", "college": "Fergusson College, Pune", "event_date": "2026-12-12",
      "expected_footfall": 1800,
      "audience": "18-22 year old arts, science and commerce students; many live in nearby PGs and hostels",
      "budget_goal_inr": 120000, "lat": 18.5236, "lng": 73.8394 } },
  { "step": "approve_shortlist", "endpoint": "POST /runs/4961225e-639a-4fad-a50d-8097ab27fe1e/approve",
    "body": { "gate": "shortlist", "decision": "approve",
              "edits": { "selected_brand_ids": ["demo-chai-junction", "demo-apex-coaching", "demo-bean-theory",
                                                "demo-pulsefit-gym", "demo-crumbs-bakery"] } },
    "trace_entry": 1211 },
  { "step": "poll_replies", "endpoint": "POST /runs/4961225e-639a-4fad-a50d-8097ab27fe1e/poll-replies",
    "body": null, "trace_entry": 1322 },
  { "step": "run_audit", "endpoint": "POST /runs/4961225e-639a-4fad-a50d-8097ab27fe1e/audit",
    "body": { "actual_footfall": 1500 }, "trace_entry": 1575 },
  { "step": "publish_with_gaps", "endpoint": "POST /runs/4961225e-639a-4fad-a50d-8097ab27fe1e/publish-with-gaps",
    "body": null, "trace_entry": 1599 },
  { "step": "approve_roi_publish", "endpoint": "POST /runs/4961225e-639a-4fad-a50d-8097ab27fe1e/approve",
    "body": { "gate": "roi_publish", "decision": "approve", "edits": {} }, "trace_entry": 1607 }
]
```

## 2. Output definition

`GET /runs/{run_id}` returns `{run, values, interrupt}`. `run`: id, status
(`completed` here), phase (`done`), pending gate, timestamps. `values`
(`EventState`): `brands` with fit scores; `proposals` per brand (tiers, pitch,
`pdf_url`, `status`, `reach_note`); `outreach` threads
(draft/sent/replied/nudged/accepted/declined/pushback/bounced + reply class);
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
      { "name": "Bronze", "price_inr": 5000, "expected_reach": 1980 },
      { "name": "Silver", "price_inr": 8500, "expected_reach": 2430 },
      { "name": "Gold", "price_inr": 12000, "expected_reach": 2970 }
    ],
    "status": "accepted",
    "pdf_url": "https://essravfkemrewdubjtau.supabase.co/storage/v1/object/public/proposals/4961225e-639a-4fad-a50d-8097ab27fe1e/demo-apex-coaching-v2.pdf"
  },
  "brand_demo_chai_junction": { "fit_score": 0.95, "unreachable": true },
  "feedback": { "demo-pulsefit-gym": ["declined"], "demo-ironcore-gym": ["declined"], "demo-page-turner": ["silent"] },
  "roi_report": {
    "actual_footfall": 1500,
    "published": true,
    "published_with_gaps": true,
    "overall_delivery_rate": 0.0,
    "sponsors": [
      { "name": "Apex Coaching Classes", "tier": "Gold", "price_inr": 12000 },
      { "name": "Bean Theory Cafe", "tier": "Silver", "price_inr": 5500 },
      { "name": "Crumbs & Co Bakery", "tier": "Bronze", "price_inr": 6000 },
      { "name": "Glow Studio Salon", "tier": "Bronze", "price_inr": 7000 }
    ],
    "summary": "0 of 15 promised deliverables verified.",
    "organizer_note": null
  }
}
```

The generated demo photos show sponsor graphic cards rather than the physical stage, stall or posts, so Gemini Vision
marked 10 checks ambiguous; 5 deliverables had no photo at all (missing). Every gap is listed in the report's
`disclosed_gaps`, and the report was published only after the organizer chose "publish with gaps disclosed".

Brand outcomes in this run: Apex Coaching accepted Gold v2 (capped at Rs 12,500 by A2, Rs 50,000 asked; v2 priced
Gold at Rs 12,000); Bean Theory accepted Silver v2 (Rs 8,000 cap honoured); Crumbs Bakery accepted Bronze; Glow
Studio Salon, a replacement, accepted Bronze; Chai Junction bounced and was marked unreachable (A1); PulseFit Gym and
IronCore Gym declined; Page Turner Books, picked after the L6 category feedback, was sent and nudged but did not reply.

## 3. Execution trace

File: `traces/2026-10-21-demo-mode-full-run.json` — downloaded via the Trace page
Export (equivalent: `GET /runs/{id}/trace/export`), committed unchanged.
Verified with `python backend/scripts/verify_trace.py` (same counts the README
demo script uses).

| Marker | This run | Evidence (first occurrence) |
|---|---|---|
| L1 Discovery self-replan | 1× | msg `947376c27eab`, trace#1193: only 3 of the target 5 good fits → radius widened to 4 km |
| L2 Outreach critique → Proposal v2 | 2× | msg `60ec1707cfbb`, trace#1282: PulseFit Gym prices too steep → v2 (also IronCore Gym, trace#1397) |
| L3 pushback → Proposal v2 | 1× | msg `805fbf6e4b80`, trace#1335: Bean Theory "Rs 8,000 max" → v2 Gold 8000 / Silver 5500 / Bronze 3000 |
| L4 decline/bounce → replacement | 3× | msg `6b206b8ef931`, trace#1338: PulseFit declined; Chai Junction bounced (trace#1339); IronCore declined (trace#1452) |
| L5 missing evidence → escalation | 1× | msg `1c2bb9d5d281`, trace#1592: ROI Audit asked for specific evidence for 15 deliverables |
| L6 category feedback | 1× | msg `a6b3d775378d`, trace#1456: two gyms declined, Clef p_yes 0.90 → gyms ×0.6, Page Turner Books picked |
| L7 infeasible → Discovery | — did not fire (Metro Wholesale was not shortlisted; every proposed brand passed the feasibility check, two after a Clef-unsure escalation where the rule kept them) | — |
| L8 reach calibration | 4× | msg `c5825ed1f38f`, trace#1608: factor saved per category (coaching, cafe, bakery, salon) on publish |
| A1 bounce beats ranking | 1× | trace#1341: Chai Junction (fit 0.95) bounced → marked unreachable |
| A2 price vs past deals | 8× | trace#1262: Apex capped Rs 50,000 → Rs 12,500 (1.25 × Rs 10,000 median) |
| A3 ROI gaps block publishing | 2× | trace#1594 block, trace#1601 released only via explicit publish-with-gaps + approval |

Per the README rule, L7 gets no narrative sentence since it did not fire in this
real trace — the run is presented as is, unedited.

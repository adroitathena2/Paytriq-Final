# Paytriq — input contract

The system's real input is one object: an `EventProfile` (`core/schemas.py`). It is
created with `POST /api/events`, posted to the `event` zone of the blackboard, and
read by A1 from the board — never from request state. Unknown fields are rejected
with HTTP 422 (`extra="forbid"`); they are never silently discarded.

## `EventProfile` schema (excerpt, `core/schemas.py:233-265`)

| Field | Type | Constraints |
| --- | --- | --- |
| `event_id` | `str` | server-owned on create; echoed on read |
| `name` | `str` | 1–200 chars, required |
| `location` | `str` | 1–200 chars, required |
| `footfall` | `int` | 0–10,000,000, required — A2's pricing moves with this |
| `date` | `str` | 4–40 chars, as the organiser writes it, required |
| `audience` | `str` | 1–500 chars (who attends — this is what sponsors buy), required |
| `budget_inr` | `float \| null` | ≥ 0, optional |
| `categories_wanted` | `list[str]` | optional; accepts an array **or** `"a, b, c"` CSV (split server-side) |
| `deliverables_offered` | `list[str]` | optional; array or CSV, same as above |
| `contact_email` | `str \| null` | optional; must contain a plausible `@` shape |
| `created_at` | `datetime` | server timestamp |

The full machine-readable schema, with required flags and hints, is served live by
`GET /api/schema/event` (the frontend builds its form from it, falling back to a
labelled built-in list when the backend is unreachable).

## Sample event (works offline)

Taken verbatim from the console's `DEMO_EVENT` (`frontend/app.js:86-96`) — the
one demo event used everywhere (console prefill, this sample, and the canonical
capture in `scripts/capture_canonical_trace.py`). The categories overlap the
offline fixture set on purpose: fixture filtering (`tools/fixtures.py`) is
exact-match on category, and the seeded categories are `cafe, gym, salon,
printing, coaching, electronics, bookstore` — so an offline run discovers leads
instead of nothing:

```json
{
  "name": "TechFest 2026",
  "location": "VIT Pune, Karad Road",
  "footfall": 5000,
  "date": "2026-02-14",
  "audience": "Second and third year engineering students, 62% male, hiring-season heavy",
  "categories_wanted": ["cafe", "coaching", "bookstore", "printing"],
  "deliverables_offered": ["main-stage logo backdrop", "4000 Instagram story mentions", "2 reels", "expo stall"],
  "budget_inr": 150000,
  "contact_email": "events@vit.edu.in"
}
```

`event_id` and `created_at` are server-owned and must not be sent; `categories_wanted`
/ `deliverables_offered` may also be sent as comma-separated strings.

## Calls

```bash
# What the backend accepts (schema + a validated sample + aliases):
curl http://127.0.0.1:18780/api/schema/event

# Create the event (returns the profile plus its run_id):
curl -X POST http://127.0.0.1:18780/api/events \
  -H "content-type: application/json" \
  -d '{"name":"TechFest 2026","location":"VIT Pune, Karad Road","footfall":5000,"date":"2026-02-14","audience":"Second and third year engineering students, 62% male, hiring-season heavy","categories_wanted":["cafe","coaching","bookstore","printing"],"deliverables_offered":["main-stage logo backdrop","4000 Instagram story mentions","2 reels","expo stall"],"budget_inr":150000,"contact_email":"events@vit.edu.in"}'

# Read it back (reconstructed from the board, not echoed from the request):
curl http://127.0.0.1:18780/api/events/{event_id}
```

# Paytriq

Paytriq is a multi-agent web app that helps Indian college event organizers find, pitch and close local sponsors
(cafes, gyms, coaching centres and salons near campus), then prove ROI to those sponsors after the event.

Four LLM agents (Discovery & Ranking, Proposal, Outreach, ROI Audit) coordinate through a shared blackboard under an
LLM supervisor built with LangGraph. They critique each other, request revisions and replan. The organizer approves
every shortlist, every email and the final report.

- Design document: [docs/design.md](docs/design.md)
- Input and output schemas with samples: [docs/io-definitions.md](docs/io-definitions.md)
- Execution traces from live runs: [traces/](traces/)

## How it works

```
Organizer creates event
  -> Supervisor (LLM) routes -> Discovery: search nearby, score fit, self-replan if < 5 good fits     [L1]
  -> Shortlist gate (organizer)
  -> Proposal: pitch + Bronze/Silver/Gold tiers + PDF
  -> Outreach critiques each proposal -> revision_request -> Proposal v2                          [L2]
  -> Send gate (organizer) -> Gmail (demo mode: team inboxes)
  -> Poll replies -> classify -> pushback -> Proposal v3 -> send gate                            [L3]
                             -> decline/bounce -> Discovery picks a replacement -> Proposal       [L4]
After the event: organizer uploads photos and links
  -> ROI Audit verifies each deliverable with Gemini Vision; missing evidence -> escalation       [L5]
  -> Supervisor arbitrates: publish with gaps disclosed, or wait -> Publish gate
```

Every agent runs its own tool-calling loop: Gemini picks the next tool, observes the result and adapts, for up to
6 tool calls per turn. Every decision, message, tool call and error is written to the `agent_trace` table and can be
exported from the Trace page.

## Repository layout

```
backend/
  app/
    main.py              FastAPI app, wiring, health check
    core/                config, auth (Supabase JWT), db, llm (Gemini + FakeLLM), trace, storage
    graph/               state (shared contracts), supervisor, approval gates, build, runner, messages
    agents/              loop (shared reasoning loop), discovery, proposal, outreach, roi_audit
    tools/               places, pdf, gmail, vision, base (timeouts and retries)
    api/                 events, runs (+ evidence, audit, poll), approvals, trace
  tests/                 pytest suite with FakeLLM and fake tools (no network)
frontend/                React + Vite + TypeScript + Tailwind (pages, components, lib)
supabase/migrations/     tables, RLS policies, storage buckets
docs/                    design.md, io-definitions.md, architecture.mmd, sample input/output
traces/                  exported traces from live runs
.github/workflows/ci.yml ruff + pytest + frontend build
render.yaml              Render blueprint for the API
```

## Run locally

Requirements: Python 3.11, Node 20+, a Gemini API key (free).

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then edit .env
```

For a quick local start, set only these in `backend/.env`:

```
GEMINI_API_KEY=<your key>
GEMINI_MIN_INTERVAL_S=4
AUTH_DISABLED=true
DEMO_MODE=true
DEMO_RECIPIENTS=you@example.com
```

With `DATABASE_URL` empty, the API uses an in-memory database and checkpointer, and proposal PDFs are stored in
`backend/.local_storage/`. Gmail stays unconfigured, so sending fails gracefully: drafts are kept and the organizer
is told.

```bash
uvicorn app.main:app --reload --port 8000
# http://localhost:8000/health and http://localhost:8000/docs
```

On native Windows, keep `DATABASE_URL` empty locally: the async Postgres driver needs a selector event loop that
uvicorn does not use on Windows. Use WSL or the deployed API to test with Postgres.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local           # leave the Supabase values empty when AUTH_DISABLED=true
npm run dev                          # http://localhost:5173
```

### Tests

```bash
cd backend
pytest -q          # unit tests per agent + full-graph integration test, all offline
ruff check .
cd ../frontend && npm run build
```

`tests/test_integration.py` runs the whole graph with fakes and asserts that the trace contains a replan, a critique
revision, a pushback-driven revision, a replacement request and an evidence escalation.

## Environment variables

Backend (`backend/.env`, or the Render dashboard):

| Variable | Required | How to get it |
|---|---|---|
| `GEMINI_API_KEY` | yes | [Google AI Studio](https://aistudio.google.com/apikey), then "Create API key" |
| `GEMINI_MODEL` | no | Defaults to `gemini-flash-latest`; any current Flash model works |
| `GEMINI_MIN_INTERVAL_S` | no | Seconds between LLM calls; `4` keeps the free tier under its rate limit |
| `SUPABASE_URL` | deploy | Supabase project, Settings, API, Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | deploy | Same page, `service_role` key (server only, never in the frontend) |
| `SUPABASE_JWT_SECRET` | legacy only | Only for projects still on the legacy HS256 JWT secret; otherwise leave empty (JWKS is used) |
| `DATABASE_URL` | deploy | Project, Connect, **Session pooler** connection string (IPv4; Render cannot reach the IPv6 direct host) |
| `AUTH_DISABLED` | local only | `true` skips login locally. Must be `false` in deployment |
| `GOOGLE_MAPS_API_KEY` | no | Google Cloud console, enable "Places API (New)". Without it, OpenStreetMap is used |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` | for email | Google Cloud console, OAuth consent screen (External, test user = sender), Credentials, OAuth client ID ("Desktop app") |
| `GMAIL_REFRESH_TOKEN` | for email | [OAuth Playground](https://developers.google.com/oauthplayground): gear icon, "Use your own OAuth credentials", scope `https://www.googleapis.com/auth/gmail.modify`, authorize with the sender account, "Exchange authorization code for tokens" |
| `GMAIL_SENDER` | for email | The dedicated demo Gmail address that authorized the token |
| `DEMO_MODE` | yes | `true` (default): all emails go to `DEMO_RECIPIENTS`, with the real brand name in the subject |
| `DEMO_RECIPIENTS` | demo | Comma-separated team inboxes; reply from these during the demo |
| `NUDGE_AFTER_DAYS` | no | Days of silence before Outreach drafts a follow-up (default 3; set `0` to demo nudges) |
| `CORS_ORIGINS` | deploy | The Vercel URL, e.g. `https://paytriq.vercel.app` |
| `PUBLIC_API_URL` | deploy | The Render URL of the API |

Frontend (`frontend/.env.local`, or Vercel project settings): `VITE_API_URL`, `VITE_SUPABASE_URL`,
`VITE_SUPABASE_ANON_KEY` (Supabase Settings, API, `anon` public key).

## Deploy

1. **Supabase**: create a project, then run `supabase/migrations/20261011000000_init.sql` in the SQL editor (or
   `supabase db push`). Under Authentication, enable email sign-in. LangGraph creates its checkpoint tables on first start.
2. **Render**: New, Blueprint, select this repo (`render.yaml`), then fill in the secret variables.
   Check `https://<api>.onrender.com/health`.
3. **Vercel**: import the repo with root directory `frontend`, framework Vite, and the three `VITE_*` variables.
   `vercel.json` handles SPA routing.
4. Set `CORS_ORIGINS` on Render to the Vercel URL and redeploy.
5. Before the demo, open `/health` once to wake the free Render instance.

## Demo script (about 5 minutes)

1. Create an event (use "Find college on map"), then click **Start finding sponsors**. Discovery may replan (L1).
2. Approve the shortlist. Proposal writes v1, Outreach critiques it and requests a revision (L2), and Proposal writes v2.
3. Edit an email if you like, then approve the send gate. Emails arrive in the team inboxes.
4. From the team inboxes, reply to the emails: one with "too expensive, max 15k", one with "not interested" and one
   with "we'll go with Silver". Then click **Check for replies**. You will see a pushback revision (L3) and a
   replacement brand (L4). Approve the new emails.
5. Upload one event photo for the accepted sponsor and run the ROI audit. It escalates the missing items (L5), and
   the supervisor recommends publishing with the gaps disclosed. Publish.
6. Open **Trace** and show the highlighted loops, then export the JSON into `traces/`.

## Team

| Member | GitHub | Owns |
|---|---|---|
| Aryan Srivastava | aryan20s | `main.py`, `graph/*`, `core/trace.py`, ROI Audit agent, `RoiReport.tsx` |
| Archisha Yadav | adroitathena2 | frontend scaffold, Discovery agent, `Events*.tsx`, `Brands.tsx` |
| Aarohi Kondpalle | mlllemmmm | Proposal agent, `tools/pdf.py`, `Proposals.tsx`, `docs/*` |
| Aditi Padole | BeTechSavvy | `supabase/migrations/*`, Outreach agent, `tools/gmail.py`, `Trace.tsx`, CI, deployment |

Built for the F0003 Agentic AI course (CA3). No secrets are committed; see `.env.example` files.

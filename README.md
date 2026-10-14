# Paytriq

Paytriq is a multi-agent web app that helps Indian college event organizers find, pitch and close local sponsors
(cafes, gyms, coaching centres and salons near campus), then prove ROI to those sponsors after the event.

Paytriq is 4 worker agents (Discovery & Ranking, Proposal, Outreach, ROI Audit) + an LLM supervisor that routes,
arbitrates conflicts and justifies every decision. It is built with LangGraph as an orchestrator-worker (supervisor)
pattern over a shared blackboard. The agents critique each other, request revisions, hand work back and replan
(eight coordination loops, L1-L8); the supervisor settles conflicts between them with three arbitration rules
(A1-A3). The organizer approves every shortlist, every email and the final report (gates `shortlist`, `send`,
`roi_publish`). The model is Gemini Flash (`gemini-flash-latest`). Two yes/no judgements (L6, L7) go to
**Cloudflare Clef**, a decision model that returns calibrated probabilities instead of text; code applies thresholds,
asks the organizer when Clef is unsure and falls back to the old rules if Clef is unavailable.

- Synopsis: [docs/synopsis.md](docs/synopsis.md)
- Design document: [docs/design.md](docs/design.md)
- Input and output schemas with samples: [docs/io-definitions.md](docs/io-definitions.md)
- Execution traces from live runs: [traces/](traces/)

## How it works

```
Organizer creates event
  -> LLM supervisor routes -> Discovery: search nearby (Google Places or OpenStreetMap), score fit,
                              self-replan if < 5 good fits                                         [L1]
  -> Shortlist gate (organizer)
  -> Proposal: feasibility (Clef: does any deliverable fit? + cost per reach); infeasible -> Discovery [L7]
             pitch + Bronze/Silver/Gold tiers + PDF; reach scaled by past calibration              [L8]
  -> Supervisor: price far above past accepted deals -> capped, revision request to Proposal       [A2]
  -> Outreach critiques each proposal -> revision_request -> Proposal v2                          [L2]
  -> Send gate (organizer) -> Gmail (DEMO_MODE: redirected to team inboxes)
  -> Poll replies -> classify -> pushback -> Proposal v2/v3 -> send gate                         [L3]
                             -> decline/bounce -> Discovery picks a replacement -> Proposal       [L4]
                             -> top-ranked brand bounced -> marked unreachable, replaced          [A1]
                             -> 2 negatives in a category -> Clef: stop it? -> Discovery re-ranks [L6]
After the event: organizer uploads photos and links (and optionally the actual footfall)
  -> ROI Audit verifies each deliverable with Gemini Vision; missing evidence -> escalation       [L5]
  -> Supervisor: missing proof blocks publishing until new evidence or "publish with gaps"        [A3]
  -> Publish gate -> approved report calibrates future reach estimates                           [L8]
```

Every agent runs its own tool-calling loop: Gemini picks the next tool, observes the result and adapts, for up to
6 tool calls per turn. The supervisor writes every routing decision, guardrail hit and arbitration to its decision
log. Every decision, message, tool call and error is written to the `agent_trace` table and can be exported from the
Trace page.

## How agents adapt to each other

One worked example per loop and rule, from the demo fixture world (`backend/app/demo/world.py`, Fergusson Fest 2026):

- **L1 self-replan (Discovery -> Discovery).** Only 4 businesses lie within 2 km, so fewer than 5 score >= 0.6.
  Discovery posts a `replan`, widens to 4 km with adjacent categories and finds the coaching centre and bakery.
- **L2 critique (Outreach -> Proposal).** Outreach reads a cafe's v1 like a sceptical shop owner, finds Gold too
  expensive for a cafe and sends a `revision_request`; Proposal writes v2 under the cap.
- **L3 pushback (Outreach -> Proposal).** Bean Theory Cafe replies "Rs 8,000 is the max". Outreach classifies it as
  `pushback_price` and routes the constraint to Proposal, which writes a version with every tier at or under Rs 8,000.
  Bean Theory then accepts Silver.
- **L4 replacement (Outreach -> Discovery).** IronCore Gym and PulseFit Gym decline; Chai Junction's email bounces.
  Each one gets its own replacement brand from Discovery.
- **L5 evidence (ROI Audit -> organizer).** One promised deliverable has no photo. ROI Audit asks the organizer for
  that specific evidence instead of guessing.
- **L6 category feedback (Outreach -> Discovery).** Two gyms said no. Outreach asks Clef whether to stop prioritising
  gyms; on yes it tells Discovery, which multiplies gym scores by 0.6, re-ranks and picks replacements from other
  categories.
- **L7 infeasible (Proposal -> Discovery).** Metro Wholesale Traders sells to shops, not students. Clef judges that
  no deliverable fits, so Proposal renders no PDF and hands the brand back to Discovery for a replacement.
- **L8 calibration (ROI Audit -> Proposal).** When the organizer approves the ROI report, the reach the verified
  deliverables actually achieved (at the actual footfall) is saved per category; the next proposal in that category
  scales its expected reach and says "estimate calibrated from N past events".
- **A1 bounce beats ranking.** Chai Junction is the closest, top-ranked cafe, but its email bounced. The supervisor
  marks it unreachable: delivery evidence beats ranking.
- **A2 price vs past deals.** This organizer's past coaching sponsors paid a median of Rs 10,000. Proposal priced Apex
  Coaching far above that, so the supervisor caps every tier at 1.25 x the median and asks for a revision. For cafes
  there are only 2 past deals, so A2 logs "insufficient data" and leaves the price alone.
- **A3 ROI gaps block publishing.** Because proof is missing, the report is not published. The organizer either
  uploads the evidence and re-runs the audit, or explicitly clicks "Publish with gaps disclosed"; the normal publish
  approval still follows.

## Repository layout

```
backend/
  app/
    main.py              FastAPI app, wiring, health check
    core/                config, auth (Supabase JWT), db, llm (Gemini + FakeLLM), clef (Clef + FakeClef), trace, storage
    graph/               state (shared contracts), supervisor, arbitration (A1-A3), approval gates, build, runner, messages
    agents/              loop (shared reasoning loop), discovery, proposal, outreach, roi_audit
    tools/               places, pdf, gmail, vision, base (timeouts and retries)
    demo/                fixture world (businesses, inbox, evidence) and the offline rehearsal LLM
    api/                 events (+ demo), runs (+ evidence, audit, poll, publish-with-gaps), approvals, trace
  scripts/               verify_trace.py (checks an exported trace for every loop and rule)
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

`tests/test_integration.py` runs the whole graph with fakes and checks the trace for the coordination loops and
arbitration; `tests/test_demo_rehearsal.py` runs the whole demo scenario through the API in rehearsal mode and
asserts that L1-L8 and A1-A3 all fire.

Check an exported trace before committing it to `traces/`:

```bash
python backend/scripts/verify_trace.py traces/<file>.json
```

## Environment variables

Backend (`backend/.env`, or the Render dashboard):

| Variable | Required | How to get it |
|---|---|---|
| `GEMINI_API_KEY` | yes | [Google AI Studio](https://aistudio.google.com/apikey), then "Create API key" |
| `GEMINI_MODEL` | no | Defaults to `gemini-flash-latest`; any current Flash model works |
| `GEMINI_MIN_INTERVAL_S` | no | Seconds between LLM calls; `4` keeps the free tier under its rate limit |
| `CLOUDFLARE_ACCOUNT_ID` | no | Cloudflare dashboard, Workers & Pages, Account ID (right sidebar). Without it (or the token), L6/L7 use the old rules (`rule_fallback` in the trace) |
| `CLOUDFLARE_API_TOKEN` | no | Cloudflare, My Profile, API Tokens, Create token from the "Workers AI" template |
| `CLEF_MODEL` | no | `clef-flash` (default, fast and cheap) or `clef` |
| `CLEF_ENABLED` | no | `true` (default); `false` forces the rule fallback |
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
| `NUDGE_AFTER_DAYS` | no | Days of silence before Outreach drafts a follow-up (default 3; `render.yaml` sets `0` as a demo setting) |
| `CORS_ORIGINS` | deploy | The Vercel URL, e.g. `https://paytriq.vercel.app` |
| `PUBLIC_API_URL` | deploy | The Render URL of the API |

Frontend (`frontend/.env.local`, or Vercel project settings): `VITE_API_URL`, `VITE_SUPABASE_URL`,
`VITE_SUPABASE_ANON_KEY` (Supabase Settings, API, `anon` public key).

## Deploy

1. **Supabase**: create a project, then run every file in `supabase/migrations/` in order in the SQL editor (or
   `supabase db push`). Under Authentication, enable email sign-in. LangGraph creates its checkpoint tables on first start.
2. **Render**: New, Blueprint, select this repo (`render.yaml`), then fill in the secret variables.
   Check `https://<api>.onrender.com/health`.
3. **Vercel**: import the repo with root directory `frontend`, framework Vite, and the three `VITE_*` variables.
   `vercel.json` handles SPA routing.
4. Set `CORS_ORIGINS` on Render to the Vercel URL and redeploy.
5. Before the demo, open `/health` once to wake the free Render instance.

## Demo mode and rehearsal

On the Events page, **Create demo event** offers two choices:

- **Live demo (real Gemini).** A demo event (Fergusson Fest 2026) in a fixture world: fictional businesses near
  campus, a scripted inbox and generated evidence photos. Every agent and supervisor decision is made by Gemini.
  No real email is sent and no map service is called. Creating it also resets the organizer's seeded past deals
  (3 coaching, 2 cafe), so A2 behaves the same every time.
- **Offline rehearsal (scripted, not for submission).** The same world with a scripted LLM and Clef, for practising without
  network or quota. Every trace entry is marked "rehearsal — not real reasoning" and the export is named
  `REHEARSAL-NOT-FOR-SUBMISSION-...`. Never put a rehearsal export in `traces/`.

Running the demo:

1. Click **Start finding sponsors**. Discovery replans (L1).
2. At the shortlist gate, tick the six fixture brands, **including Metro Wholesale Traders** (needed for L7):
   Chai Junction Cafe, Bean Theory Cafe, IronCore Gym, Metro Wholesale Traders, PulseFit Gym, Apex Coaching Classes.
3. Approve every send gate. Click **Check for replies** twice (the second time Bean Theory accepts its v2).
4. In the ROI step, click **Add demo evidence** (one deliverable is left out on purpose), enter an actual footfall
   and run the audit. Publishing is blocked (A3); click **Publish with gaps disclosed**, then approve the report.
5. Open **Trace**, check the labels, click **Export JSON**, run `python backend/scripts/verify_trace.py` on the file
   and commit it unchanged as `traces/YYYY-MM-DD-demo-mode-full-run.json`.

## Demo script (60 seconds)

Numbers in `[[...]]` are read from the committed real trace with `python backend/scripts/verify_trace.py
traces/<file>.json`; never use numbers from a rehearsal run. If a loop or rule did not fire in the real trace
(A1 and L2 depend on Gemini's own ranking and critique; L6 and L7 on Clef's judgement), drop that sentence rather
than claim it.

> "The reviewer asked for an example of one agent's output changing what another does. Here are eight loops and
> three conflicts the supervisor arbitrates.
> On this real demo-mode run with Gemini, Discovery found only [[N good fits at 2 km]] good fits and replanned
> itself (L1). Proposal refused to pitch a wholesaler and sent it back to Discovery (L7). The supervisor capped
> Apex Coaching from Rs [[A2 original price]] to Rs [[A2 cap]] because past coaching deals had a median of
> Rs [[A2 median]] (A2). Outreach critiqued [[N L2]] proposals and Proposal rewrote them (L2).
> After sending: Bean Theory said 'Rs 8,000 max' and Proposal wrote a cheaper version (L3); [[N L4]] brands
> declined or bounced and Discovery replaced each one (L4); the bounce on our top-ranked cafe overrode Discovery's
> ranking (A1); two gyms said no, so Discovery down-weighted gyms and picked other categories (L6).
> After the event, one photo was missing: ROI Audit asked for it (L5) and the supervisor blocked publishing until
> we chose to publish with the gap disclosed (A3). That approval saved a reach factor of [[L8 factor]] that the next
> proposal uses (L8). All [[N decision_log entries]] supervisor decisions are in this exported trace, unedited."

## Team

| Member | GitHub | Owns |
|---|---|---|
| Aryan Srivastava | aryan20s | `main.py`, `graph/*`, `core/trace.py`, `core/clef.py`, ROI Audit agent, `RoiReport.tsx` |
| Archisha Yadav | adroitathena2 | frontend scaffold, Discovery agent, `Events*.tsx`, `Brands.tsx` |
| Aarohi Kondpalle | mlllemmmm | Proposal agent, `tools/pdf.py`, `Proposals.tsx`, `docs/*` |
| Aditi Padole | BeTechSavvy | `supabase/migrations/*`, Outreach agent, `tools/gmail.py`, `Trace.tsx`, CI, deployment |

Built for the F0003 Agentic AI course (CA3). No secrets are committed; see `.env.example` files.

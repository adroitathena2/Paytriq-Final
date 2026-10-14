# Paytriq — project context for Claude Code

## What we are building
Paytriq is a multi-agent web app that helps Indian college event organizers (fest cores, clubs, hackathon teams)
find and close local sponsors (cafes, gyms, coaching centres, salons within ~2 km of campus), and then prove ROI
to those sponsors after the event. This is a graded CA3 project for an Agentic AI course. The two attached PDFs
(Guidelines + Approved Proposal) are the source of truth. Re-read the rubric before big decisions.

## What the evaluators care about most (design every feature around this)
1. ≥4 genuinely AGENTIC agents (plan, reason, use tools, adapt) — NOT a fixed pipeline.
2. Real INTER-AGENT COORDINATION: agents critique each other, request revisions, replan. The reviewer flagged our
   proposal as "a largely linear pipeline" — the coordination loops in section "Coordination loops" are mandatory.
3. A REAL execution trace of agent messages, captured from the live system (never hand-written or edited).
4. Consistency: design doc, I/O definitions, README and code must describe the same system.
5. Robustness: bad input and a failing tool call must degrade gracefully, never crash.
6. Repo hygiene: clear folders, real README, no secrets, working .gitignore, real tests, small meaningful commits.

## Team and module owners (owners maintain, test and present these modules)
| Owner | GitHub | Owns |
|---|---|---|
| Aryan Srivastava | aryan20s | backend/app/main.py, backend/app/graph/*, backend/app/core/trace.py, ROI Audit Agent, frontend/src/pages/RoiReport.tsx |
| Archisha Yadav | adroitathena2 | frontend scaffold (layout, auth, api client, ui components), Discovery Agent, frontend/src/pages/Events*.tsx, Brands.tsx |
| Aarohi Kondpalle | mlllemmmm | Proposal Agent, backend/app/tools/pdf.py, frontend/src/pages/Proposals.tsx, docs/* |
| Aditi Padole (Ady) | BeTechSavvy | supabase/migrations/*, Outreach Agent, backend/app/tools/gmail.py, frontend/src/pages/Trace.tsx, CI, deployment |

## Stack (keep it; zero infra cost)
- Backend: Python 3.11, FastAPI, LangGraph (StateGraph + supervisor routing + `interrupt()` for human-in-the-loop),
  `langgraph-checkpoint-postgres` so paused runs survive Render restarts. Pydantic v2 everywhere.
- LLM: Google Gemini via `google-genai`. Model name from env `GEMINI_MODEL` (default to a current Flash model;
  do NOT hardcode the retired "gemini-1.5-flash"). Use structured JSON output with Pydantic schemas.
- Data/auth/storage: Supabase (Postgres, Auth, Storage). Frontend uses supabase-js for auth; backend verifies the
  Supabase JWT.
- Frontend: React + Vite + TypeScript + Tailwind. Deployed on Vercel. Backend on Render (free tier).
- Tools: Google Places API (if `GOOGLE_MAPS_API_KEY` set) with automatic fallback to OpenStreetMap Overpass API
  (free); Gmail API (OAuth, dedicated demo sender account); ReportLab for PDFs; Gemini Vision for ROI evidence;
  Cloudflare Clef (`clef-flash`, Workers AI) for the two yes/no judgements in L6 and L7 (rule fallback without it).
- Tests: pytest with a FakeLLM (no network in tests). Frontend: no heavy test suite needed.

## Repository layout
```
backend/
  app/
    main.py                # FastAPI app, routers, CORS, health check
    core/  config.py  auth.py  db.py  llm.py  clef.py  trace.py
    graph/ state.py  supervisor.py  arbitration.py  build.py  messages.py
    agents/ discovery.py  proposal.py  outreach.py  roi_audit.py
    tools/  places.py  pdf.py  gmail.py  vision.py
    api/    events.py  runs.py  approvals.py  trace.py
    demo/   world.py (fixture world)  rehearsal.py (scripted LLM, practice only)
  scripts/ verify_trace.py
  tests/
  requirements.txt  .env.example
frontend/  (Vite app)  src/{pages,components,lib}
supabase/migrations/
docs/  synopsis.md  design.md  io-definitions.md  architecture.png(.mmd, .svg)  sample_input.json  sample_output.json
traces/  (exported real run traces, JSON)
.github/workflows/ci.yml
README.md  .gitignore  CLAUDE.md
```

## Shared contracts (everyone codes against these from Day 1 — do not change without telling the team)

### EventState (backend/app/graph/state.py)
```python
class Brand(BaseModel): id: str; name: str; category: str; address: str; lat: float; lng: float
    distance_km: float; contact_email: str | None; website: str | None; fit_score: float; fit_reason: str
    unreachable: bool                       # A1
class Tier(BaseModel): name: Literal["Bronze","Silver","Gold"]; price_inr: int; deliverables: list[str]; expected_reach: int
class Proposal(BaseModel): brand_id: str; version: int; tiers: list[Tier]; pitch: str; pdf_url: str | None; status: str
    reach_note: str | None                  # L8; status also on_hold (revision cap) / infeasible (L7)
class OutreachThread(BaseModel): brand_id: str; thread_id: str | None; status: Literal["draft","sent","replied","nudged","accepted","declined","pushback","bounced"]
    last_reply_class: str | None; history: list[dict]
class AgentMessage(BaseModel): id: str; from_agent: str; to_agent: str; type: Literal["request","result","critique","revision_request","replan","escalation","decision"]
    content: dict; reasoning: str; ts: datetime
class EventState(TypedDict):
    event_id: str; event: dict            # name, college, date, expected_footfall, audience, budget_goal_inr, lat, lng, demo_scenario
    brands: list[Brand]; selected_brand_ids: list[str]
    proposals: dict[str, Proposal]; outreach: dict[str, OutreachThread]
    evidence: list[dict]; roi_report: dict | None
    messages: Annotated[list[AgentMessage], operator.add]   # the inter-agent message bus (blackboard)
    pending_approval: dict | None; phase: str; step_count: int; errors: list[dict]
    decision_log: list[dict]              # supervisor: every route, guardrail hit and arbitration
    feedback: dict[str, list[str]]; revision_count: dict[str, int]
    publish_with_gaps: bool; target_brand_count: int; actual_footfall: int | None
```
New signal kinds go in `content.reason` / `content.source`, never in new message types.

### Agent interface
Every agent is a LangGraph node: `async def run(state: EventState, deps: Deps) -> dict` (returns a partial state
update). Inside, the agent runs its OWN small reasoning loop: LLM decides which tool to call next (function calling),
observes the result, and decides whether it is done, needs to retry differently, or must message another agent.
Every decision is appended to `messages` as an `AgentMessage` with a short `reasoning` string, and logged via
`trace.log(...)`. Max 6 tool calls per agent turn. `deps` carries llm, db, tools (so tests can inject fakes).

### Supervisor
`graph/supervisor.py`: an LLM-driven router. Given phase, latest messages, errors and step_count it returns
`{"next": <agent|"human_approval"|"END">, "reason": str}` (structured output). Hard guardrails in code:
max 25 steps per run, never send email without an approved `pending_approval`, never loop the same pair more than
3 times (then escalate to the human), at most 2 agent-initiated revision requests per brand (then hold the brand and
escalate; organizer requests are never capped). The supervisor's decision is also an AgentMessage (type "decision").
Before routing, `graph/arbitration.py` settles conflicts with rules A1-A3 (section "Coordination loops"); Gemini
writes the justification, with a template fallback. Every route, guardrail hit and arbitration is appended to
`decision_log` and traced as `event_type="decision"`. The system is "4 worker agents + an LLM supervisor that routes,
arbitrates conflicts and justifies every decision" (orchestrator-worker over a shared blackboard).

### Trace (core/trace.py)
`trace.log(run_id, agent, event_type, payload)` writes to Supabase table `agent_trace` and stdout (JSON lines).
`GET /runs/{run_id}/trace` returns it; `GET /runs/{run_id}/trace/export` returns a downloadable JSON file.
Traces are captured from real runs only. Never fabricate or edit a trace.

## Agents (4 + supervisor)
1. **Discovery & Ranking Agent** — finds businesses near the campus, enriches them, scores fit for the event's
   audience with the LLM. ADAPTS: if fewer than 5 brands score ≥0.6, it replans itself (widen radius 2→4 km,
   add adjacent categories) up to 2 times, explaining why in the trace. Can be called back by the supervisor to
   find a replacement when a brand declines/bounces or is infeasible (one replacement per open request), and
   re-ranks a category after category feedback (L6).
2. **Proposal Agent** — writes a brand-specific pitch and Bronze/Silver/Gold tiers with expected-reach estimates
   (derived from footfall and audience), renders a clean PDF to Supabase Storage. Accepts `revision_request`
   messages and produces a new version (v2, v3) honouring the constraint (e.g. lower price, different deliverable).
   Checks feasibility before rendering (L7) and applies the organizer's reach calibration (L8).
3. **Outreach Agent** — before sending, CRITIQUES the proposal (tone, length, price vs. brand size) and may send a
   `revision_request` to the Proposal Agent (max 1 per brand). After human approval, sends via Gmail, polls the thread,
   classifies replies into {interested, accepted, pushback_price, pushback_deliverables, declined, out_of_office,
   unrelated}, and decides next action: nudge after N days, route pushback to Proposal Agent, or ask Discovery for a
   replacement on decline/bounce. Records per-brand outcomes (`feedback`), sends category feedback (L6) and stores
   accepted deals (`sponsor_deals`) for arbitration A2.
4. **ROI Audit Agent** — after the event, takes organizer-uploaded photos/links, uses Gemini Vision to verify each
   promised deliverable from the accepted proposal (logo visible? stall present? post live?), and produces an ROI
   report with evidence per deliverable. If evidence is missing or ambiguous, it sends an `escalation` asking the
   organizer for specific extra evidence instead of guessing, and marks under-delivery honestly. Computes a reach
   calibration factor per sponsor, saved only when the organizer approves publishing (L8).

## Coordination loops (mandatory — these are what lift us out of "linear pipeline")
- L1 Discovery self-replan (too few good brands → widen search).
- L2 Outreach critiques Proposal before send → revision_request → Proposal v2.
- L3 Reply = pushback_price/deliverables → supervisor routes to Proposal with constraint → v2 → human approval → Outreach sends.
- L4 Decline/bounce → supervisor asks Discovery for the next-best replacement brand → Proposal → approval.
- L5 ROI finds missing evidence → escalation to human asking for specific evidence. Supervisor rule A3 then
  blocks publishing by default (phase `roi_blocked`, "missing proof" escalation naming brand + deliverable). It
  reopens only when (a) new evidence passes a re-audit, or (b) the organizer explicitly clicks "Publish with gaps
  disclosed" (`POST /runs/{id}/publish-with-gaps`); the normal `roi_publish` approval still follows. Never publish
  silently.
- L6 Outreach records outcomes per brand; 2 negative outcomes (declined, bounced, silent after nudge) in one
  category trigger a Clef judgement ("stop prioritising this category?"); on yes → `request`
  `reason=category_feedback` (with `p_yes`, `decided_by`) to Discovery → that category's scores x0.6, re-rank,
  replacements from other categories until the organizer's shortlist size is met → Proposal → send gate. A "no" is
  re-asked only after a new negative outcome in that category.
- L7 Proposal feasibility check before any PDF: Clef judges whether any deliverable fits the brand (rule fallback:
  wholesale/industrial/warehouse), and code checks Bronze costs at most Rs 50 per person reached → `request`
  `reason=infeasible` to Discovery → replacement.
- Clef judgement layer (`core/clef.py`, `judge()`): P(yes) >= 0.75 yes, <= 0.25 no, in between keep the rule's
  answer and escalate to the human (`reason=clef_unsure`); Clef unavailable → rule (`source=rule_fallback`). One
  `decision` trace entry `kind=clef_judgement` per call. Supervisor routing and reply classification stay on Gemini.
- L8 ROI Audit computes reach factor = verified-deliverable reach at actual footfall ÷ estimate; saved per owner +
  category (running average) only when the organizer approves publishing → `result` `reason=calibration` to
  Proposal, which scales expected reach and adds `reach_note`.

Supervisor arbitration rules (in code, Gemini justifies, template fallback; each a `decision_log` entry):
- A1 bounce vs ranking: a top-3 brand bounced → mark `unreachable`, replacement `request` to Discovery.
- A2 price vs past deals: price > 1.25 × median accepted price (same owner + category) → cap, `revision_request`
  `source=arbitration`; only with ≥ 3 deals, otherwise log "insufficient data".
- A3 ROI gaps: see L5 above.
Each loop and rule must be visible in the trace and in the Trace page UI.

## Human-in-the-loop gates
Use LangGraph `interrupt()`; the frontend shows an approval card. Gates: (a) brand shortlist, (b) proposals before
send, (c) ROI report before publish. API: `POST /runs/{run_id}/approve` with `{gate, decision, edits}` resumes the graph.

## Demo/safety mode
`DEMO_MODE=true` (default in deployment): every outgoing email goes to addresses in `DEMO_RECIPIENTS` (team inboxes),
with the real brand name in the subject, so we never spam real businesses. Replies are real (we reply from team
inboxes during the demo). `POST /runs/{run_id}/poll-replies` triggers a poll on demand for the live demo.
Demo events (`POST /events/demo`, `events.demo_scenario`) run in a fixture world (`backend/app/demo/`): fixture
businesses, scripted inbox and generated evidence, with real Gemini reasoning. Rehearsal events use a scripted LLM,
every trace entry is marked "rehearsal — not real reasoning" and exports are named
`REHEARSAL-NOT-FOR-SUBMISSION-...`; never commit them to `traces/`.

## Robustness rules
- Every tool call wrapped: timeout, 1 retry with backoff, then a structured error added to `state.errors` and a
  trace entry; the agent then reasons about an alternative (e.g. Places fails → Overpass; Gmail fails → keep as
  draft and tell the human). Never let one tool failure crash the run.
- Validate all API input with Pydantic; return clear 4xx messages. Frontend shows friendly errors, never a blank screen.

## UI guidelines (keep it calm, not cluttered)
- One neutral palette (slate/zinc greys + ONE accent, e.g. indigo-600), white background, generous spacing, max
  content width ~1100px, system font or Inter. No gradients, no glassmorphism, no emoji, no animations beyond subtle
  transitions. Light mode is enough.
- Layout: left sidebar (Events, Trace, Settings) + main content. Each page has one clear primary action.
- Event page = a horizontal stepper: Discover → Proposals → Outreach → ROI. Approval cards appear inline at the step
  that is waiting. Tables for brands/threads, small status badges, empty states with one line of guidance.
- Trace page = vertical timeline of AgentMessages (agent chip, type badge, one-line reasoning, expandable JSON).
  Coordination loops (revision_request, replan, escalation) get a subtle highlighted left border.

## Engineering rules
- Never commit secrets. `.env.example` lists every variable with a placeholder. `.gitignore` covers .env, venv,
  node_modules, dist, __pycache__.
- Small commits with conventional messages: `feat(discovery): ...`, `fix(outreach): ...`, `test(proposal): ...`,
  `docs: ...`, `ci: ...`. Commit after each working sub-step, not one dump at the end of the day.
- Every agent has tests in backend/tests using FakeLLM + fake tools, covering the happy path AND its adaptation loop.
- Write code a student can explain: clear names, short functions, docstrings on each agent explaining its
  reasoning loop. No unnecessary abstraction.

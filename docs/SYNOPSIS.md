# Paytriq — synopsis

Campus sponsorship dealmaking is still done by hand: a student organiser cold-mails
nearby businesses, quotes a flat fee copied from last year's fest, and negotiates over
email with no record of what was promised or why a number was chosen. The previous
prototype automated the shape of that workflow while keeping its failure modes — a
hardcoded list of invented businesses with sequential fake phone numbers, one flat
price for every event regardless of footfall, a reply router that tested for "yes"
before it tested for price objections, and a human approval gate that defaulted to
`approve=True`.

Paytriq replaces that pipeline with seven reasoning agents (A1 Discovery … A7 Arbiter)
coordinating through a typed append-only blackboard under a LangGraph `StateGraph`.
Every routing decision goes through a named decision backend (`clef → gemini → rules`)
and names its source; every outbound email and MoU release parks at a human gate
backed by a durable approval row; every figure in the final ROI report carries a
stated assumption. The execution trace — including `distinct_gap_values`, a timing
statistic that distinguishes a genuine capture from a hand-written one — is the
system's primary output, not an afterthought.

Relative to the proposal, three things changed, each because the alternative would
have produced a demo that looks right without being right. First, the live demo pins
the decision layer to `gemini` instead of `auto`: no reviewer machine or Render host
has the local Clef GPU endpoint, so `auto` would probe it and fall back on every
single decision. Second, the committed golden run (`docs/samples/`) is a
simulated-backend capture and is labelled as exactly that — evidence of control flow
through the real agents, not of model quality; a live-model golden that shows
`gemini` ahead of `rules` still needs a funded key and one full Create-event → Audit
run. Third, the primary model is the lite variant (`GEMINI_MODEL=gemini-3.5-flash-lite`,
fallback `gemini-3.8-flash`), because the heavier default made every decision wait
out the 45-second timeout and arrive tagged `DEGRADED`.

To try it, start the API (`uvicorn api.main:app`), open the dependency-free console in
`frontend/`, press *Prefill demo event* (the single `TechFest 2026` event used
everywhere — console, `docs/INPUT.md`, and the canonical capture), and drive the
seven stages in order — approving the SEND and MOU gates when they park. Architecture
and contracts are documented in `docs/DESIGN.md` (`docs/architecture.png` is rendered
from the compiled graph, not drawn by hand), `docs/INPUT.md`, and `docs/OUTPUT.md`;
the evaluation manual and its headline negative finding are in `docs/EVALUATION.md`.

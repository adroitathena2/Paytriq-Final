# Paytriq: project synopsis

**Problem and target user.** Event organizers at Indian colleges (fest cores, student clubs, hackathon teams) raise
sponsorship mostly by cold-emailing local businesses with copy-pasted PDFs, and few of those emails get a reply.
The cafes, gyms, coaching centres and salons within about 2 km of campus do want to reach students, but they rarely
get proof afterwards that the promised logo, stall or social post actually happened, so trust stays low. Paytriq is
for the student organizer who has to find, pitch and close these local sponsors with a small team and no budget, and
then show each sponsor what it got for its money.

**Solution.** Paytriq is a web app built as 4 worker agents + an LLM supervisor. The Discovery agent finds and ranks
nearby businesses for the event's audience; the Proposal agent writes a brand-specific pitch with Bronze, Silver and
Gold tiers and a PDF; the Outreach agent critiques each proposal, sends the emails the organizer approved, reads and
classifies the replies, and negotiates; after the event, the ROI Audit agent checks the organizer's photos and links
against every promised deliverable and writes an honest ROI report. The LLM supervisor decides at every step which
agent works next, settles conflicts between agents with fixed rules, and records the reason for every decision. The
agents react to each other: a brand's "too expensive" reply makes the Proposal agent write a cheaper version, a
decline or a failed email makes Discovery find a replacement, and missing photos stop the report from being
published until the organizer adds evidence or chooses to publish with the gaps listed. The organizer approves the
shortlist, every email and the final report.

**Changes since the approved CA2 description, and why.** (1) The CA2 review said the design read as "a largely
linear pipeline" with no example of one agent's output changing what another does. We added eight coordination
loops in which agents critique each other, hand work back and replan, and three arbitration rules with which the
supervisor resolves conflicts (a bounced email beats a high ranking, a price far above past deals is capped, and
missing proof blocks publishing). (2) Gemini 1.5 Flash, named in the proposal, has been retired; Paytriq uses the
`gemini-flash-latest` model, configurable in one setting. (3) Discovery falls back to OpenStreetMap when Google
Places is not available, which keeps the project at zero infrastructure cost. (4) A demo-mode email redirect sends
every outgoing email to team inboxes, so no real business is contacted during evaluation. (5) A demo mode with a
fixture world (fictional businesses, a scripted inbox and generated evidence photos, with real Gemini reasoning)
lets us show every loop and rule live in a few minutes.

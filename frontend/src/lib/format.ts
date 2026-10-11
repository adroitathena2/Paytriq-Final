export const inr = (n: number) => `Rs. ${n.toLocaleString("en-IN")}`;

export const pct = (n: number) => `${Math.round(n * 100)}%`;

export function when(iso: string | undefined | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export const AGENT_LABELS: Record<string, string> = {
  supervisor: "Supervisor",
  discovery: "Discovery",
  proposal: "Proposal",
  outreach: "Outreach",
  roi_audit: "ROI Audit",
  human: "Organizer",
  human_approval: "Approval",
  system: "System",
  END: "End",
};

export const agentLabel = (a: string) => AGENT_LABELS[a] ?? a;

/** Which stepper step a backend phase belongs to. */
export type Step = "discover" | "proposals" | "outreach" | "roi";

export function stepForPhase(phase: string | undefined): Step {
  switch (phase) {
    case "proposals":
    case "review":
    case "send_review":
    case "outreach_paused":
      return "proposals";
    case "sending":
    case "awaiting_replies":
    case "replies":
    case "replacement":
    case "outreach_idle":
      return "outreach";
    case "roi":
    case "roi_review":
    case "awaiting_evidence":
    case "done":
      return "roi";
    default:
      return "discover";
  }
}

export const PHASE_LABELS: Record<string, string> = {
  discovery: "Finding nearby brands",
  discovery_failed: "Could not find brands",
  shortlist_review: "Shortlist ready for review",
  proposals: "Writing proposals",
  review: "Reviewing proposals before sending",
  send_review: "Emails ready for your approval",
  outreach_paused: "Outreach paused",
  sending: "Sending emails",
  awaiting_replies: "Waiting for replies",
  replies: "Reading replies",
  replacement: "Finding a replacement brand",
  outreach_idle: "Outreach idle",
  roi: "Auditing deliverables",
  roi_review: "ROI report ready for review",
  awaiting_evidence: "Waiting for more evidence",
  done: "Report published",
  blocked: "Blocked",
};

export const phaseLabel = (p: string | undefined) => (p ? PHASE_LABELS[p] ?? p : "");

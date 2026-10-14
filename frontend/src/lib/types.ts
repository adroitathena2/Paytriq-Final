// Mirrors the backend contracts in backend/app/graph/state.py and the API responses.

export interface EventRow {
  id: string;
  name: string;
  college: string;
  event_date: string;
  expected_footfall: number;
  audience: string;
  budget_goal_inr: number;
  lat: number;
  lng: number;
  created_at: string;
  runs?: RunRow[];
  /** "demo" = fixture businesses + real Gemini; "rehearsal" = scripted LLM, not for submission. */
  demo_scenario?: DemoScenario | null;
}

export type DemoScenario = "demo" | "rehearsal";

export type RunStatus = "running" | "waiting_approval" | "idle" | "completed" | "failed" | "stalled";
export type Gate = "shortlist" | "send" | "roi_publish";

export interface RunRow {
  id: string;
  event_id: string;
  event_name?: string;
  status: RunStatus;
  phase: string;
  pending_gate: Gate | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface Brand {
  id: string;
  name: string;
  category: string;
  address: string;
  lat: number;
  lng: number;
  distance_km: number;
  contact_email: string | null;
  website: string | null;
  fit_score: number;
  fit_reason: string;
  unreachable?: boolean;
}

export interface Tier {
  name: "Bronze" | "Silver" | "Gold";
  price_inr: number;
  deliverables: string[];
  expected_reach: number;
}

export interface Proposal {
  brand_id: string;
  version: number;
  tiers: Tier[];
  pitch: string;
  pdf_url: string | null;
  status: string; // e.g. draft, ready, sent, on_hold
  reach_note?: string | null;
}

export interface HistoryEntry {
  type: string;
  ts?: string;
  [key: string]: unknown;
}

export interface OutreachThread {
  brand_id: string;
  thread_id: string | null;
  status: string;
  last_reply_class: string | null;
  history: HistoryEntry[];
}

export type MessageType = "request" | "result" | "critique" | "revision_request" | "replan" | "escalation" | "decision";

export interface AgentMessage {
  id: string;
  from_agent: string;
  to_agent: string;
  type: MessageType;
  content: Record<string, unknown>;
  reasoning: string;
  ts: string;
}

export interface DeliverableCheck {
  deliverable: string;
  status: "verified" | "not_verified" | "ambiguous" | "missing";
  confidence: number;
  explanation: string;
  evidence_ids: string[];
}

export interface SponsorReport {
  brand_id: string;
  name: string;
  tier: string;
  price_inr: number;
  expected_reach: number;
  checks: DeliverableCheck[];
  delivered: number;
  total: number;
  delivery_rate: number;
}

export interface RoiReport {
  event: { name: string; college: string; date: string; expected_footfall: number };
  generated_at: string;
  sponsors: SponsorReport[];
  overall_delivery_rate: number;
  disclosed_gaps: { brand_id: string; brand_name: string; deliverable: string; status: string; explanation: string }[];
  evidence_requests: string[];
  summary: string;
  published: boolean;
  organizer_note?: string | null;
}

export interface RunState {
  event: { name: string; college: string; date: string; expected_footfall: number; audience: string; budget_goal_inr: number };
  brands: Brand[];
  selected_brand_ids: string[];
  proposals: Record<string, Proposal>;
  outreach: Record<string, OutreachThread>;
  evidence: Evidence[];
  roi_report: RoiReport | null;
  messages: AgentMessage[];
  pending_approval: { gate: Gate; status: string; payload?: unknown } | null;
  phase: string;
  step_count: number;
  errors: { agent: string; tool: string; error: string }[];
  decision_log: DecisionEntry[];
  feedback: Record<string, string[]>;
  revision_count: Record<string, number>;
  publish_with_gaps: boolean;
}

/** One supervisor decision; also written to the trace as event_type "decision". */
export interface DecisionEntry {
  ts: string;
  kind: "route" | "guardrail" | "arbitration" | "clef_judgement";
  next: string;
  reason: string;
  source: "llm" | "fallback" | "code" | "clef" | "rule_fallback";
  // clef_judgement entries (written by proposal / outreach for L7 / L6)
  purpose?: string;
  question?: string;
  p_yes?: number | null;
  answer?: boolean;
  escalated?: boolean;
  rule_answer?: boolean;
  rule?: string; // A1|A2|A3 for arbitration; max_steps|empty_gate|pair_loop|revision_cap for guardrail
  gate?: string;
  step?: number;
  proposed?: string | Record<string, unknown>;
  brand_id?: string;
  conflict?: string;
  agents?: string[];
  resolution?: string;
  justification?: string;
}

export interface TraceExport {
  run_id: string;
  event_id: string;
  exported_at: string;
  note: string;
  rehearsal: boolean;
  entries: TraceEntry[];
}

export interface EmailDraft {
  brand_id: string;
  brand_name: string;
  kind: "initial" | "revised" | "nudge";
  subject: string;
  body: string;
  version: number;
  pdf_url: string | null;
}

export interface Interrupt {
  gate: Gate;
  payload: {
    suggested_brand_ids?: string[];
    brands?: Brand[];
    emails?: EmailDraft[];
    report?: RoiReport;
  };
}

export interface RunDetail {
  run: RunRow;
  values: Partial<RunState>;
  interrupt: Interrupt | null;
}

export interface Evidence {
  id: string;
  run_id: string;
  brand_id: string;
  kind: "photo" | "link";
  url: string | null;
  storage_path: string | null;
  note: string | null;
  created_at: string;
}

export interface TraceEntry {
  id: number;
  run_id: string;
  ts: string;
  agent: string;
  event_type: string;
  payload: Record<string, unknown>;
}

export interface Health {
  status: string;
  demo_mode: boolean;
  llm_configured: boolean;
  database: string;
}

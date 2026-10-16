// Shared labels for coordination loops (L1-L8), supervisor arbitration rules (A1-A3) and rehearsal markers.
// Used by the Trace page, the Agent Activity timeline and the run graph so all three name things the same way.
import { agentLabel } from "./format";
import type { DecisionEntry, TraceEntry } from "./types";

/** How a timeline item is coloured: forward step, feedback loop, arbitration/conflict, human gate. */
export type ActivityKind = "forward" | "loop" | "arbitration" | "gate";

export const ARBITRATION_LABELS: Record<string, string> = {
  A1: "A1 bounce beats ranking",
  A2: "A2 price vs past deals",
  A3: "A3 ROI gaps block publish",
};

/** Workers in pipeline order; routing to an earlier one than the last is a backward (feedback) route. */
export const AGENT_ORDER = ["discovery", "proposal", "outreach", "roi_audit"];

interface MessageLike {
  type?: string;
  from_agent?: string;
  to_agent?: string;
  content?: Record<string, unknown>;
}

/** Label for an AgentMessage payload, or null when it is an ordinary forward step. */
export function messageLabel(m: MessageLike): string | null {
  const c = m.content ?? {};
  if (m.type === "replan") return "L1 self-replan";
  if (m.type === "revision_request") {
    if (c.source === "critique") return "L2 critique → revision";
    if (c.source === "brand_reply") return "L3 pushback → revision";
    if (c.source === "arbitration") return "A2 price cap → revision";
    return "Organizer revision";
  }
  if (m.type === "request" && c.reason === "replacement") return c.arbitration === "A1" ? "L4 replacement · A1" : "L4 replacement";
  if (m.type === "request" && c.reason === "category_feedback") return c.category ? `L6 category feedback: ${String(c.category)}` : "L6 category feedback";
  if (m.type === "request" && c.reason === "infeasible") return "L7 infeasible → rediscover";
  if (m.type === "result" && c.reason === "calibration") return "L8 reach calibration";
  if (m.type === "escalation") {
    if (m.from_agent === "roi_audit") return "L5 evidence escalation";
    if (c.reason === "revision_cap") return "Revision cap";
    if (c.reason === "clef_unsure") return "Clef unsure → organizer";
    if (c.reason === "missing_proof" || c.arbitration === "A3") return ARBITRATION_LABELS.A3;
    return "Escalation";
  }
  return null;
}

/** Label for a supervisor decision_log entry (arbitration and guardrails only; plain routes have none). */
export function decisionLabel(d: Partial<DecisionEntry>): string | null {
  if (d.kind === "arbitration") return ARBITRATION_LABELS[d.rule ?? ""] ?? `Arbitration ${d.rule ?? ""}`.trim();
  if (d.kind === "guardrail") return d.rule === "revision_cap" ? "Revision cap" : `Guardrail: ${(d.rule ?? "").replace(/_/g, " ")}`;
  if (d.kind === "clef_judgement") return `${CLEF_LOOPS[d.purpose ?? ""] ?? ""}Clef judgement`.trim();
  return null;
}

/** Which loop each Clef question belongs to. */
const CLEF_LOOPS: Record<string, string> = { "outreach.category_feedback": "L6 ", "proposal.feasibility": "L7 " };

/** Label for any trace entry, or null. */
export function entryLabel(e: TraceEntry): string | null {
  if (e.event_type === "message") return messageLabel(e.payload as MessageLike);
  if (e.event_type === "decision") return decisionLabel(e.payload as Partial<DecisionEntry>);
  return null;
}

/** Short code such as "L3" or "A2" at the start of a label, used on graph arrows. */
export function labelCode(label: string | null): string | null {
  const m = label?.match(/^([LA]\d)\b/);
  return m ? m[1] : null;
}

/** True for messages/decisions produced by a supervisor arbitration rule (amber). */
export function isArbitration(e: TraceEntry): boolean {
  const p = e.payload as Record<string, unknown>;
  if (e.event_type === "decision") return p.kind === "arbitration" || p.kind === "guardrail";
  if (e.event_type === "guardrail") return true;
  if (e.event_type !== "message") return false;
  const c = (p.content ?? {}) as Record<string, unknown>;
  return Boolean(c.arbitration) || c.source === "arbitration" || c.reason === "revision_cap";
}

/** True for L1-L8 coordination loop messages (indigo). */
export function isLoop(e: TraceEntry): boolean {
  return !isArbitration(e) && /^L\d/.test(entryLabel(e) ?? "");
}

/** Rehearsal runs stamp every payload with a `rehearsal` field; it must never be mistaken for real reasoning. */
export function isRehearsal(e: TraceEntry): boolean {
  return Boolean((e.payload as Record<string, unknown>).rehearsal);
}

/** One-line summary of a supervisor decision event. */
export function decisionSummary(d: Partial<DecisionEntry>): { headline: string; detail: string } {
  if (d.kind === "arbitration") {
    return { headline: `Arbitration ${d.rule ?? ""}: ${d.resolution ?? d.next ?? ""}`, detail: d.justification ?? d.reason ?? "" };
  }
  if (d.kind === "guardrail") return { headline: `guardrail: ${d.rule ?? ""}`, detail: d.reason ?? "" };
  if (d.kind === "clef_judgement") {
    const p = typeof d.p_yes === "number" ? `P(yes) ${d.p_yes.toFixed(2)}` : "Clef unavailable";
    const by = d.source === "rule_fallback" ? "rule fallback" : d.escalated ? "Clef unsure, rule kept, organizer asked" : "Clef";
    return { headline: `${d.answer ? "yes" : "no"} (${p}; decided by ${by})`, detail: d.question ?? "" };
  }
  return { headline: `routes to ${agentLabel(d.next ?? "")}`, detail: d.reason ?? "" };
}

export interface Route {
  entry: TraceEntry;
  next: string;
  reason: string;
  /** Worker that was routed to before this one (null at the start). */
  previous: string | null;
  backward: boolean;
}

/**
 * Supervisor route decisions in order. Prefers "decision" events (kind "route"); older runs without
 * them fall back to supervisor messages of type "decision" (content.next).
 */
export function routesFromTrace(entries: TraceEntry[]): Route[] {
  const hasDecisionEvents = entries.some((e) => e.event_type === "decision");
  const routes: Route[] = [];
  let lastWorker: string | null = null;
  for (const e of entries) {
    const r = routeOf(e, hasDecisionEvents);
    if (!r) continue;
    const isWorker = AGENT_ORDER.includes(r.next);
    const backward = isWorker && lastWorker !== null && AGENT_ORDER.indexOf(r.next) < AGENT_ORDER.indexOf(lastWorker);
    routes.push({ entry: e, next: r.next, reason: r.reason, previous: lastWorker, backward });
    if (isWorker) lastWorker = r.next;
  }
  return routes;
}

function routeOf(e: TraceEntry, hasDecisionEvents: boolean): { next: string; reason: string } | null {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const p = e.payload as Record<string, any>;
  if (hasDecisionEvents) {
    return e.event_type === "decision" && p.kind === "route" && p.next ? { next: String(p.next), reason: p.reason ?? "" } : null;
  }
  if (e.event_type === "message" && p.type === "decision" && p.from_agent === "supervisor" && p.content?.next) {
    return { next: String(p.content.next), reason: p.reasoning ?? "" };
  }
  return null;
}

/** Normalises human / human_approval into one "gate" node name. */
export const nodeFor = (agent: string) => (agent === "human" || agent === "human_approval" ? "human" : agent);

import { Link } from "react-router-dom";
import { agentLabel, when } from "../lib/format";
import { decisionSummary, entryLabel, isArbitration, isLoop, isRehearsal, routesFromTrace, type ActivityKind } from "../lib/loops";
import type { DecisionEntry, TraceEntry } from "../lib/types";
import { RehearsalMarker } from "./Banners";
import { Card, cx } from "./ui";

interface Item {
  id: number;
  ts: string;
  kind: ActivityKind;
  who: string;
  label: string | null;
  reason: string;
  rehearsal: boolean;
}

const KIND_STYLES: Record<ActivityKind, { border: string; dot: string; text: string; name: string }> = {
  forward: { border: "border-l-slate-300", dot: "bg-slate-400", text: "text-slate-600", name: "Forward step" },
  loop: { border: "border-l-indigo-500", dot: "bg-indigo-500", text: "text-indigo-700", name: "Feedback loop" },
  arbitration: { border: "border-l-amber-500", dot: "bg-amber-500", text: "text-amber-800", name: "Arbitration / conflict" },
  gate: { border: "border-l-emerald-500", dot: "bg-emerald-500", text: "text-emerald-700", name: "Human gate" },
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Payload = Record<string, any>;

/** Turns raw trace entries into timeline items: routes, messages, arbitrations and gates (no raw tool calls). */
function buildActivity(entries: TraceEntry[]): Item[] {
  const routes = new Map(routesFromTrace(entries).map((r) => [r.entry.id, r]));
  const items: Item[] = [];
  for (const e of entries) {
    const p = e.payload as Payload;
    const base = { id: e.id, ts: e.ts, rehearsal: isRehearsal(e) };
    const route = routes.get(e.id);
    if (route) {
      const to = agentLabel(route.next);
      items.push({
        ...base,
        kind: route.backward ? "loop" : route.next === "human_approval" ? "gate" : "forward",
        who: `Supervisor → ${to}`,
        label: route.backward ? `Back to ${to} (feedback loop)` : null,
        reason: route.reason,
      });
      continue;
    }
    const item = otherItem(e, p);
    if (item) items.push({ ...base, ...item });
  }
  return items;
}

function otherItem(e: TraceEntry, p: Payload): Omit<Item, "id" | "ts" | "rehearsal"> | null {
  const label = entryLabel(e);
  const kind: ActivityKind = isArbitration(e) ? "arbitration" : isLoop(e) ? "loop" : "forward";
  switch (e.event_type) {
    case "message":
      if (p.type === "decision") return null; // supervisor route, already shown via routesFromTrace
      return { kind, who: `${agentLabel(p.from_agent)} → ${agentLabel(p.to_agent)}`, label: label ?? p.type?.replace(/_/g, " "), reason: p.reasoning ?? "" };
    case "decision": {
      if (p.kind === "route") return null;
      const { headline, detail } = decisionSummary(p as Partial<DecisionEntry>);
      return { kind: "arbitration", who: "Supervisor", label: label ?? headline, reason: p.kind === "arbitration" ? `${headline}. ${detail}` : detail };
    }
    case "guardrail":
      return { kind: "arbitration", who: "Supervisor", label: `Guardrail: ${String(p.rule ?? "").replace(/_/g, " ")}`, reason: p.reason ?? "" };
    case "interrupt":
      return { kind: "gate", who: "Organizer gate", label: `Paused for approval: ${String(p.gate ?? "").replace(/_/g, " ")}`, reason: "" };
    case "resume":
      return { kind: "gate", who: "Organizer", label: `${p.decision === "approve" ? "Approved" : "Rejected"} ${String(p.gate ?? "").replace(/_/g, " ")}`, reason: "" };
    case "tool_error":
    case "llm_error":
      return { kind: "forward", who: agentLabel(e.agent), label: e.event_type === "llm_error" ? "LLM unavailable" : `${p.tool} failed`, reason: p.error ?? "" };
    default:
      return null;
  }
}

export function KindLegend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
      {(Object.keys(KIND_STYLES) as ActivityKind[]).map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <span className={cx("h-2 w-2 rounded-full", KIND_STYLES[k].dot)} />
          {KIND_STYLES[k].name}
        </span>
      ))}
    </div>
  );
}

/** Agent Activity: every supervisor step, message, backward route, arbitration and gate, newest first. */
export default function ActivityTimeline({ entries, runId }: { entries: TraceEntry[]; runId: string }) {
  const items = buildActivity(entries).reverse();
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-3">
        <h3 className="text-sm font-semibold">Agent activity</h3>
        <KindLegend />
        <Link to={`/trace/${runId}`} className="text-sm text-indigo-700 hover:underline">
          Full trace
        </Link>
      </div>
      {items.length === 0 ? (
        <p className="px-5 py-4 text-sm text-slate-500">Agent steps appear here as soon as the run starts.</p>
      ) : (
        <ol className="max-h-[28rem] space-y-1.5 overflow-y-auto px-5 py-3">
          {items.map((it) => (
            <li key={it.id} className={cx("border-l-4 bg-white py-1.5 pl-3 text-sm", KIND_STYLES[it.kind].border)}>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span className="w-24 shrink-0 text-xs tabular-nums text-slate-400">{when(it.ts)}</span>
                <span className="font-medium text-slate-800">{it.who}</span>
                {it.label && <span className={cx("text-xs font-medium", KIND_STYLES[it.kind].text)}>{it.label}</span>}
                {it.rehearsal && <RehearsalMarker />}
              </div>
              {it.reason && <p className="mt-0.5 pl-0 text-slate-600 sm:pl-[6.5rem]">{it.reason}</p>}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

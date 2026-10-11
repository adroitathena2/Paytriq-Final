import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, Card, EmptyState, ErrorBanner, Loading, PageHeader, StatusBadge, Table, cx } from "../components/ui";
import { api } from "../lib/api";
import { agentLabel, when } from "../lib/format";
import type { RunRow, TraceEntry } from "../lib/types";

/** Trace index (pick a run) or the timeline of one run. */
export default function Trace() {
  const { runId } = useParams();
  return runId ? <Timeline runId={runId} /> : <RunPicker />;
}

function RunPicker() {
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.listRuns().then(setRuns).catch((e: Error) => setError(e.message));
  }, []);
  return (
    <>
      <PageHeader title="Agent trace" subtitle="Every message, decision and tool call from real runs, exactly as captured." />
      {error && <ErrorBanner message={error} />}
      {!runs && !error && <Loading />}
      {runs && runs.length === 0 && <EmptyState title="No runs yet" hint="Start a run from an event page; its trace appears here." />}
      {runs && runs.length > 0 && (
        <Card>
          <Table head={["Event", "Status", "Phase", "Started"]}>
            {runs.map((r) => (
              <tr key={r.id} className="hover:bg-slate-50">
                <td className="px-4 py-2.5">
                  <Link to={`/trace/${r.id}`} className="font-medium text-indigo-700 hover:underline">
                    {r.event_name ?? r.event_id}
                  </Link>
                </td>
                <td className="px-4 py-2.5">
                  <StatusBadge status={r.status} />
                </td>
                <td className="px-4 py-2.5 text-slate-600">{r.phase}</td>
                <td className="px-4 py-2.5 text-slate-500">{when(r.created_at)}</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </>
  );
}

/** Labels for the coordination loops, so they stand out in the timeline. */
function loopLabel(e: TraceEntry): string | null {
  if (e.event_type !== "message") return null;
  const p = e.payload as { type: string; content: Record<string, unknown>; from_agent: string };
  if (p.type === "replan") return "L1 self-replan";
  if (p.type === "revision_request") {
    if (p.content.source === "critique") return "L2 critique → revision";
    if (p.content.source === "brand_reply") return "L3 pushback → revision";
    return "Organizer revision";
  }
  if (p.type === "request" && p.content.reason === "replacement") return "L4 replacement";
  if (p.type === "escalation") return p.from_agent === "roi_audit" ? "L5 evidence escalation" : "Escalation";
  return null;
}

const AGENT_COLORS: Record<string, string> = {
  supervisor: "bg-slate-800 text-white",
  discovery: "bg-sky-100 text-sky-800",
  proposal: "bg-violet-100 text-violet-800",
  outreach: "bg-teal-100 text-teal-800",
  roi_audit: "bg-orange-100 text-orange-800",
  human: "bg-amber-100 text-amber-900",
};

function AgentChip({ agent }: { agent: string }) {
  return <span className={cx("rounded px-1.5 py-0.5 text-xs font-medium", AGENT_COLORS[agent] ?? "bg-slate-100 text-slate-700")}>{agentLabel(agent)}</span>;
}

function summary(e: TraceEntry): { headline: string; detail: string } {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const p = e.payload as Record<string, any>;
  switch (e.event_type) {
    case "message":
      return { headline: `${agentLabel(p.from_agent)} → ${agentLabel(p.to_agent)}`, detail: p.reasoning };
    case "tool_call":
      return { headline: `calls ${p.tool}`, detail: p.reasoning ?? "" };
    case "tool_result":
      return { headline: `${p.tool} returned`, detail: "" };
    case "tool_error":
      return { headline: `${p.tool} failed`, detail: p.error };
    case "llm_error":
      return { headline: "LLM unavailable", detail: p.error };
    case "guardrail":
      return { headline: `guardrail: ${p.rule}`, detail: `proposed ${p.proposed?.next}, applied ${p.applied?.next}` };
    case "interrupt":
      return { headline: `paused for approval: ${p.gate}`, detail: "" };
    case "resume":
      return { headline: `organizer: ${p.decision} ${p.gate}`, detail: "" };
    case "run_start":
      return { headline: `segment started (${p.trigger})`, detail: "" };
    case "run_end":
      return { headline: `segment ended in phase ${p.phase}`, detail: "" };
    default:
      return { headline: e.event_type, detail: p.error ?? "" };
  }
}

function Timeline({ runId }: { runId: string }) {
  const [entries, setEntries] = useState<TraceEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showTools, setShowTools] = useState(false);
  const [exporting, setExporting] = useState(false);

  const load = useCallback(() => {
    setError(null);
    api.trace(runId).then(setEntries).catch((e: Error) => setError(e.message));
  }, [runId]);
  useEffect(load, [load]);

  const visible = useMemo(
    () => (entries ?? []).filter((e) => showTools || !["tool_call", "tool_result"].includes(e.event_type)),
    [entries, showTools],
  );
  const loops = (entries ?? []).filter((e) => loopLabel(e)).length;

  async function exportTrace() {
    setExporting(true);
    try {
      await api.downloadTrace(runId);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setExporting(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Agent trace"
        subtitle={entries ? `${entries.length} entries · ${loops} coordination events · captured live, unedited` : undefined}
        action={
          <div className="flex gap-2">
            <Button variant="secondary" onClick={load}>
              Refresh
            </Button>
            <Button onClick={exportTrace} busy={exporting}>
              Export JSON
            </Button>
          </div>
        }
      />
      <label className="mb-4 flex items-center gap-2 text-sm text-slate-600">
        <input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={showTools} onChange={(e) => setShowTools(e.target.checked)} />
        Show every tool call and result
      </label>
      {error && <ErrorBanner message={error} onRetry={load} />}
      {!entries && !error && <Loading label="Loading trace" />}
      {entries && entries.length === 0 && <EmptyState title="No trace yet" hint="Entries appear as soon as the agents start working." />}
      <ol className="space-y-2">
        {visible.map((e) => (
          <TraceItem key={e.id} entry={e} />
        ))}
      </ol>
    </>
  );
}

function TraceItem({ entry }: { entry: TraceEntry }) {
  const [open, setOpen] = useState(false);
  const loop = loopLabel(entry);
  const { headline, detail } = summary(entry);
  const p = entry.payload as { type?: string };
  const isError = ["tool_error", "llm_error", "error"].includes(entry.event_type);
  return (
    <li
      className={cx(
        "rounded-md border bg-white px-4 py-2.5",
        loop ? "border-slate-200 border-l-4 border-l-indigo-500" : "border-slate-200",
        isError && "border-l-4 border-l-red-400",
      )}
    >
      <button className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="w-28 shrink-0 text-xs tabular-nums text-slate-400">{when(entry.ts)}</span>
        <AgentChip agent={entry.agent} />
        {entry.event_type === "message" ? <Badge tone={loop ? "accent" : "neutral"}>{p.type?.replace(/_/g, " ")}</Badge> : <Badge>{entry.event_type.replace(/_/g, " ")}</Badge>}
        {loop && <span className="text-xs font-medium text-indigo-700">{loop}</span>}
        <span className="text-sm font-medium text-slate-800">{headline}</span>
        {detail && <span className="min-w-0 flex-1 truncate text-sm text-slate-500">{detail}</span>}
      </button>
      {open && <pre className="mt-2 max-h-96 overflow-auto rounded bg-slate-50 p-3 text-xs text-slate-700">{JSON.stringify(entry.payload, null, 2)}</pre>}
    </li>
  );
}

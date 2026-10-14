import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import ActivityTimeline from "../components/ActivityTimeline";
import { DemoBanner } from "../components/Banners";
import RunGraph from "../components/RunGraph";
import Stepper from "../components/Stepper";
import { Button, Card, EmptyState, ErrorBanner, Loading, PageHeader, Spinner, StatusBadge } from "../components/ui";
import { api } from "../lib/api";
import { agentLabel, phaseLabel, stepForPhase, type Step } from "../lib/format";
import { isRehearsal } from "../lib/loops";
import type { DemoScenario, EventRow, Gate, RunDetail, TraceEntry } from "../lib/types";
import Brands from "./Brands";
import Outreach from "./Outreach";
import Proposals from "./Proposals";
import { RoiStep } from "./RoiReport";

export interface StepProps {
  runId: string;
  detail: RunDetail;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<void>;
  demoScenario?: DemoScenario | null;
}

const GATE_STEP: Record<Gate, Step> = { shortlist: "discover", send: "proposals", roi_publish: "roi" };

export default function EventsDetail() {
  const { eventId = "" } = useParams();
  const [event, setEvent] = useState<EventRow | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [trace, setTrace] = useState<TraceEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<Step | null>(null);

  const loadEvent = useCallback(() => {
    setError(null);
    api
      .getEvent(eventId)
      .then((e) => {
        setEvent(e);
        setRunId(e.runs?.[0]?.id ?? null);
      })
      .catch((e: Error) => setError(e.message));
  }, [eventId]);
  useEffect(loadEvent, [loadEvent]);

  const refresh = useCallback(async () => {
    if (!runId) return;
    try {
      setDetail(await api.getRun(runId));
    } catch (e) {
      setActionError((e as Error).message);
    }
    // The activity timeline and graph read the stored trace, so they survive a page refresh.
    api.trace(runId).then(setTrace).catch(() => undefined);
  }, [runId]);

  const status = detail?.run.status;
  useEffect(() => {
    if (!runId) return;
    refresh();
    // Poll quickly while agents work, slowly otherwise (replies can arrive from other tabs).
    const timer = setInterval(refresh, status === "running" ? 2500 : 10000);
    return () => clearInterval(timer);
  }, [runId, status, refresh]);

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setActionError(null);
      try {
        await fn();
        await refresh();
      } catch (e) {
        setActionError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  if (error) return <ErrorBanner message={error} onRetry={loadEvent} />;
  if (!event) return <Loading label="Loading event" />;

  const subtitle = `${event.college} · ${event.event_date} · ${event.expected_footfall.toLocaleString("en-IN")} expected`;
  const current = stepForPhase(detail?.values.phase);
  const waiting = detail?.interrupt ? GATE_STEP[detail.interrupt.gate] : null;
  const shown = selected ?? waiting ?? current;
  const stepProps = runId && detail ? { runId, detail, busy, act, demoScenario: event.demo_scenario } : null;

  return (
    <>
      <PageHeader
        title={event.name}
        subtitle={subtitle}
        action={runId && <Link to={`/trace/${runId}`} className="text-sm font-medium text-indigo-700 hover:underline">View agent trace</Link>}
      />

      {(event.demo_scenario || trace.some(isRehearsal)) && (
        <div className="mb-6">
          {/* the trace flag also catches rehearsal runs whose event row lacks demo_scenario */}
          <DemoBanner scenario={trace.some(isRehearsal) ? "rehearsal" : event.demo_scenario} />
        </div>
      )}

      {!runId && (
        <EmptyState
          title="Your agents are ready"
          hint="Discovery will look for cafes, gyms, coaching centres and more within 2 km of campus and rank them for your audience."
          action={
            <Button busy={busy} onClick={() => act(async () => setRunId((await api.startRun(eventId)).id))}>
              Start finding sponsors
            </Button>
          }
        />
      )}

      {runId && !detail && <Loading label="Loading run" />}

      {stepProps && (
        <div className="space-y-6">
          <StatusBar {...stepProps} />
          {actionError && <ErrorBanner message={actionError} />}
          <Stepper current={current} selected={shown} waiting={waiting} onSelect={setSelected} />
          {shown === "discover" && <Brands {...stepProps} />}
          {shown === "proposals" && <Proposals {...stepProps} />}
          {shown === "outreach" && <Outreach {...stepProps} />}
          {shown === "roi" && <RoiStep {...stepProps} />}
          <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
            <ActivityTimeline entries={trace} runId={stepProps.runId} />
            <RunGraph entries={trace} />
          </div>
        </div>
      )}
    </>
  );
}

function StatusBar({ runId, detail, busy, act }: StepProps) {
  const { run, values } = detail;
  const last = values.messages?.[values.messages.length - 1];
  return (
    <Card className="flex flex-wrap items-center gap-3 px-5 py-3.5">
      {run.status === "running" && <Spinner className="h-4 w-4 text-indigo-600" />}
      <StatusBadge status={run.status} />
      <span className="text-sm font-medium text-slate-800">{phaseLabel(values.phase)}</span>
      {run.status === "running" && last && (
        <span className="truncate text-sm text-slate-500">
          {agentLabel(last.from_agent)}: {last.reasoning}
        </span>
      )}
      {(run.status === "failed" || run.status === "stalled") && (
        <>
          <span className="text-sm text-slate-500">{run.last_error ?? "The agents stopped unexpectedly."}</span>
          <Button className="ml-auto" variant="secondary" busy={busy} onClick={() => act(() => api.continueRun(runId))}>
            Continue run
          </Button>
        </>
      )}
    </Card>
  );
}

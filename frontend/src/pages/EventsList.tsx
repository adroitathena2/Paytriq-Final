import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Badge, Button, Card, CardHeader, EmptyState, ErrorBanner, Loading, PageHeader, Table, cx } from "../components/ui";
import { api } from "../lib/api";
import type { EventRow } from "../lib/types";

export default function EventsList() {
  const navigate = useNavigate();
  const [events, setEvents] = useState<EventRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    api.listEvents().then(setEvents).catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  const [demoOpen, setDemoOpen] = useState(false);
  const newEvent = <Button onClick={() => navigate("/events/new")}>New event</Button>;
  const actions = (
    <div className="flex gap-2">
      <Button variant="secondary" onClick={() => setDemoOpen(!demoOpen)} aria-expanded={demoOpen}>
        Create demo event
      </Button>
      {newEvent}
    </div>
  );

  return (
    <>
      <PageHeader title="Events" subtitle="Each event gets its own team of agents to find and close sponsors." action={actions} />
      {demoOpen && <DemoChooser onCreated={(id) => navigate(`/events/${id}`)} onCancel={() => setDemoOpen(false)} />}
      {error && <ErrorBanner message={error} onRetry={load} />}
      {!events && !error && <Loading />}
      {events && events.length === 0 && (
        <EmptyState title="No events yet" hint="Create your first event to start finding sponsors near campus." action={newEvent} />
      )}
      {events && events.length > 0 && (
        <Card>
          <Table head={["Event", "College", "Date", "Footfall", "Sponsorship goal"]}>
            {events.map((e) => (
              <tr key={e.id} className="hover:bg-slate-50">
                <td className="px-4 py-3 font-medium">
                  <Link to={`/events/${e.id}`} className="text-indigo-700 hover:underline">
                    {e.name}
                  </Link>
                  {e.demo_scenario && (
                    <span className="ml-2">
                      <Badge tone={e.demo_scenario === "rehearsal" ? "warn" : "neutral"}>{e.demo_scenario}</Badge>
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-slate-600">{e.college}</td>
                <td className="px-4 py-3 text-slate-600">{e.event_date}</td>
                <td className="px-4 py-3 text-slate-600">{e.expected_footfall.toLocaleString("en-IN")}</td>
                <td className="px-4 py-3 text-slate-600">Rs. {e.budget_goal_inr.toLocaleString("en-IN")}</td>
              </tr>
            ))}
          </Table>
        </Card>
      )}
    </>
  );
}

const DEMO_OPTIONS = [
  { rehearsal: false, title: "Live demo (real Gemini)", hint: "Fixture businesses and inbox, real agent reasoning. No real emails are sent." },
  { rehearsal: true, title: "Offline rehearsal (scripted, not for submission)", hint: "Scripted LLM answers for practising the flow offline. Its trace is marked as rehearsal." },
];

/** Lets the organizer pick a live demo or an offline rehearsal, then creates the event and opens it. */
function DemoChooser({ onCreated, onCancel }: { onCreated: (id: string) => void; onCancel: () => void }) {
  const [rehearsal, setRehearsal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      onCreated((await api.createDemoEvent(rehearsal)).id);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <Card className="mb-6">
      <CardHeader title="Create a demo event" subtitle="A ready-made event near a sample campus, for showing the agents end to end." />
      <div className="space-y-2 px-5 py-4">
        {DEMO_OPTIONS.map((o) => (
          <label
            key={o.title}
            className={cx(
              "flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5 transition-colors",
              rehearsal === o.rehearsal ? "border-indigo-600 bg-indigo-50/50" : "border-slate-200 hover:border-slate-300",
            )}
          >
            <input type="radio" name="demo" className="mt-1 accent-indigo-600" checked={rehearsal === o.rehearsal} onChange={() => setRehearsal(o.rehearsal)} />
            <span>
              <span className="block text-sm font-medium text-slate-800">{o.title}</span>
              <span className="block text-sm text-slate-500">{o.hint}</span>
            </span>
          </label>
        ))}
        {error && <ErrorBanner message={error} />}
        <div className="flex gap-2 pt-1">
          <Button busy={busy} onClick={create}>
            Create demo event
          </Button>
          <Button variant="ghost" disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </div>
    </Card>
  );
}

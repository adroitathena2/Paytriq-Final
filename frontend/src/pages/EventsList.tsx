import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Button, Card, EmptyState, ErrorBanner, Loading, PageHeader, Table } from "../components/ui";
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

  const newEvent = <Button onClick={() => navigate("/events/new")}>New event</Button>;

  return (
    <>
      <PageHeader title="Events" subtitle="Each event gets its own team of agents to find and close sponsors." action={newEvent} />
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

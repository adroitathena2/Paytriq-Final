import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Card, ErrorBanner, Field, Input, PageHeader, Textarea } from "../components/ui";
import { api } from "../lib/api";

interface Form {
  name: string;
  college: string;
  event_date: string;
  expected_footfall: string;
  audience: string;
  budget_goal_inr: string;
  lat: string;
  lng: string;
}

const EMPTY: Form = { name: "", college: "", event_date: "", expected_footfall: "", audience: "", budget_goal_inr: "", lat: "", lng: "" };

/** Client-side checks mirror the backend's validation so most mistakes are caught before submitting. */
function validate(f: Form): string | null {
  if (f.name.trim().length < 2) return "Give the event a name.";
  if (f.college.trim().length < 2) return "Enter the college name.";
  if (!f.event_date) return "Pick the event date.";
  const footfall = Number(f.expected_footfall);
  if (!Number.isInteger(footfall) || footfall < 10) return "Expected footfall must be a whole number of at least 10.";
  if (f.audience.trim().length < 3) return "Describe the audience, e.g. '18-22 year old engineering students'.";
  if (!(Number(f.budget_goal_inr) >= 0)) return "The sponsorship goal must be a positive amount.";
  const lat = Number(f.lat);
  const lng = Number(f.lng);
  if (!f.lat || !f.lng || Math.abs(lat) > 90 || Math.abs(lng) > 180) return "Set the campus location (find it or use your location).";
  return null;
}

export default function EventsCreate() {
  const navigate = useNavigate();
  const [form, setForm] = useState<Form>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [locating, setLocating] = useState(false);
  const [locationNote, setLocationNote] = useState<string | null>(null);

  const set = (key: keyof Form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  async function findCollege() {
    if (!form.college.trim()) return setError("Type the college name first.");
    setLocating(true);
    setError(null);
    try {
      const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=in&q=${encodeURIComponent(form.college)}`;
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      const hits = (await res.json()) as { lat: string; lon: string; display_name: string }[];
      if (!hits.length) throw new Error("not found");
      setForm((f) => ({ ...f, lat: Number(hits[0].lat).toFixed(5), lng: Number(hits[0].lon).toFixed(5) }));
      setLocationNote(hits[0].display_name);
    } catch {
      setError("Could not find that college on the map. Use your current location or enter coordinates.");
    } finally {
      setLocating(false);
    }
  }

  function useMyLocation() {
    if (!navigator.geolocation) return setError("Your browser cannot share its location.");
    setLocating(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setForm((f) => ({ ...f, lat: pos.coords.latitude.toFixed(5), lng: pos.coords.longitude.toFixed(5) }));
        setLocationNote("Your current location");
        setLocating(false);
      },
      () => {
        setError("Location permission was denied. Find the college instead or enter coordinates.");
        setLocating(false);
      },
    );
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = validate(form);
    if (problem) return setError(problem);
    setBusy(true);
    setError(null);
    try {
      const event = await api.createEvent({
        name: form.name.trim(),
        college: form.college.trim(),
        event_date: form.event_date,
        expected_footfall: Number(form.expected_footfall),
        audience: form.audience.trim(),
        budget_goal_inr: Number(form.budget_goal_inr || 0),
        lat: Number(form.lat),
        lng: Number(form.lng),
      });
      navigate(`/events/${event.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="New event" subtitle="Tell the agents about your event. They use this to find and pitch the right brands." />
      <Card className="max-w-2xl">
        <form onSubmit={submit} className="space-y-5 p-6" noValidate>
          <div className="grid gap-5 sm:grid-cols-2">
            <Field label="Event name">
              <Input value={form.name} onChange={set("name")} placeholder="TechFest 2026" />
            </Field>
            <Field label="Date">
              <Input type="date" value={form.event_date} onChange={set("event_date")} />
            </Field>
          </div>
          <Field label="College">
            <Input value={form.college} onChange={set("college")} placeholder="Symbiosis Institute of Technology, Pune" />
          </Field>
          <Field label="Audience" hint="Who attends? The agents match brands to this.">
            <Textarea rows={2} value={form.audience} onChange={set("audience")} placeholder="18-22 year old engineering students, mostly hostel residents" />
          </Field>
          <div className="grid gap-5 sm:grid-cols-2">
            <Field label="Expected footfall">
              <Input type="number" min={10} value={form.expected_footfall} onChange={set("expected_footfall")} placeholder="1500" />
            </Field>
            <Field label="Sponsorship goal (Rs.)">
              <Input type="number" min={0} value={form.budget_goal_inr} onChange={set("budget_goal_inr")} placeholder="150000" />
            </Field>
          </div>

          <fieldset className="rounded-md border border-slate-200 p-4">
            <legend className="px-1 text-sm font-medium text-slate-700">Campus location</legend>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="secondary" onClick={findCollege} busy={locating}>
                Find college on map
              </Button>
              <Button type="button" variant="ghost" onClick={useMyLocation} disabled={locating}>
                Use my location
              </Button>
            </div>
            {locationNote && <p className="mt-2 text-xs text-slate-500">{locationNote}</p>}
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <Field label="Latitude">
                <Input value={form.lat} onChange={set("lat")} placeholder="18.5204" />
              </Field>
              <Field label="Longitude">
                <Input value={form.lng} onChange={set("lng")} placeholder="73.8567" />
              </Field>
            </div>
          </fieldset>

          {error && <ErrorBanner message={error} />}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => navigate("/events")}>
              Cancel
            </Button>
            <Button type="submit" busy={busy}>
              Create event
            </Button>
          </div>
        </form>
      </Card>
    </>
  );
}

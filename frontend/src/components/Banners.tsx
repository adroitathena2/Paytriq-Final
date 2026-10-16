import type { DemoScenario } from "../lib/types";

export const REHEARSAL_TEXT = "Rehearsal run — scripted LLM, not real reasoning. Not for submission.";

/** Amber page-level banner for rehearsal runs (scripted LLM). */
export function RehearsalBanner() {
  return (
    <div role="status" className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-900">
      {REHEARSAL_TEXT}
    </div>
  );
}

/** Banner on demo events: tells the viewer what is real and what is fixture data. */
export function DemoBanner({ scenario }: { scenario: DemoScenario | null | undefined }) {
  if (scenario === "rehearsal") return <RehearsalBanner />;
  if (scenario !== "demo") return null;
  return (
    <div role="status" className="rounded-md border border-indigo-200 bg-indigo-50/60 px-4 py-3 text-sm text-indigo-900">
      Demo mode: fixture businesses and inbox, real Gemini reasoning. No real emails are sent.
    </div>
  );
}

/** Small inline marker on each rehearsal trace entry. */
export function RehearsalMarker() {
  return (
    <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-xs font-medium text-amber-800" title="Scripted LLM output, not real reasoning">
      rehearsal
    </span>
  );
}

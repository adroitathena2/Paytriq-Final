import type { ReactNode } from "react";

/** Inline card shown at the step that is waiting for the organizer. */
export default function ApprovalCard({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-amber-300 bg-amber-50/40" aria-label={title}>
      <div className="border-b border-amber-200 px-5 py-4">
        <p className="text-xs font-semibold uppercase tracking-wide text-amber-700">Your approval is needed</p>
        <h3 className="mt-1 text-sm font-semibold text-slate-900">{title}</h3>
        <p className="mt-0.5 text-sm text-slate-600">{description}</p>
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

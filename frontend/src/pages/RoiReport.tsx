import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import ApprovalCard from "../components/ApprovalCard";
import { Badge, Button, Card, CardHeader, EmptyState, ErrorBanner, Field, Input, Loading, StatusBadge, Table } from "../components/ui";
import { api } from "../lib/api";
import { inr, pct, when } from "../lib/format";
import type { Evidence, RoiReport as Report } from "../lib/types";
import type { StepProps } from "./EventsDetail";

/** ROI step inside the event page: upload evidence, run the audit, review and publish the report. */
export function RoiStep({ runId, detail, busy, act }: StepProps) {
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const report = detail.values.roi_report;
  const gate = detail.interrupt?.gate === "roi_publish" ? detail.interrupt : null;
  const escalation = [...(detail.values.messages ?? [])].reverse().find((m) => m.from_agent === "roi_audit" && m.type === "escalation");

  const loadEvidence = useCallback(() => {
    api.listEvidence(runId).then(setEvidence).catch(() => setEvidence([]));
  }, [runId]);
  useEffect(loadEvidence, [loadEvidence]);

  const threads = Object.values(detail.values.outreach ?? {});
  const brands = Object.fromEntries((detail.values.brands ?? []).map((b) => [b.id, b.name]));
  const sponsors = threads.filter((t) => t.status === "accepted").map((t) => t.brand_id);
  const idle = detail.run.status !== "running" && detail.run.status !== "waiting_approval";

  if (!sponsors.length && !report)
    return <EmptyState title="No sponsors have accepted yet" hint="Once a brand accepts, upload event photos here after the event to prove delivery." />;

  return (
    <div className="space-y-6">
      {gate && report && (
        <ApprovalCard
          title="Publish the ROI report?"
          description="The supervisor recommends publishing with any gaps disclosed. You can also wait and upload more evidence."
        >
          <div className="flex flex-wrap gap-2">
            <Button busy={busy} onClick={() => act(() => api.approve(runId, "roi_publish", "approve"))}>
              Publish report
            </Button>
            <Button variant="secondary" disabled={busy} onClick={() => act(() => api.approve(runId, "roi_publish", "reject"))}>
              Wait for more evidence
            </Button>
          </div>
        </ApprovalCard>
      )}

      {escalation && !report?.published && (
        <div className="rounded-md border-l-4 border-indigo-500 bg-indigo-50/50 px-4 py-3 text-sm text-slate-700">
          <p className="font-medium">The ROI Audit agent needs more evidence instead of guessing:</p>
          <ul className="mt-1 list-disc pl-5">
            {((escalation.content.requests as string[] | undefined) ?? [escalation.reasoning]).map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      <Card>
        <CardHeader
          title="Evidence"
          subtitle="Photos or links proving each deliverable. Mention the deliverable in the note."
          action={
            <Button busy={busy} disabled={!idle || evidence.length === 0} onClick={() => act(() => api.startAudit(runId))}>
              Run ROI audit
            </Button>
          }
        />
        <EvidenceForm runId={runId} sponsors={sponsors.length ? sponsors : (detail.values.selected_brand_ids ?? [])} names={brands} onAdded={loadEvidence} />
        {evidence.length > 0 && (
          <Table head={["Sponsor", "Type", "Note", "Added"]}>
            {evidence.map((e) => (
              <tr key={e.id}>
                <td className="px-4 py-2.5">{brands[e.brand_id] ?? e.brand_id}</td>
                <td className="px-4 py-2.5">
                  {e.kind === "link" && e.url ? (
                    <a href={e.url} target="_blank" rel="noreferrer" className="text-indigo-700 hover:underline">
                      link
                    </a>
                  ) : (
                    "photo"
                  )}
                </td>
                <td className="px-4 py-2.5 text-slate-600">{e.note || "-"}</td>
                <td className="px-4 py-2.5 text-slate-500">{when(e.created_at)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      {report && <ReportView report={report} runId={runId} />}
    </div>
  );
}

function EvidenceForm({ runId, sponsors, names, onAdded }: { runId: string; sponsors: string[]; names: Record<string, string>; onAdded: () => void }) {
  const [brandId, setBrandId] = useState(sponsors[0] ?? "");
  const [link, setLink] = useState("");
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!file && !link.trim()) return setError("Choose a photo or paste a link.");
    if (file && file.size > 8 * 1024 * 1024) return setError("Photos must be under 8 MB.");
    const form = new FormData();
    form.append("brand_id", brandId);
    form.append("note", note);
    if (file) form.append("file", file);
    else form.append("link", link.trim());
    setBusy(true);
    setError(null);
    try {
      await api.addEvidence(runId, form);
      setLink("");
      setNote("");
      setFile(null);
      (e.target as HTMLFormElement).reset();
      onAdded();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-3 border-b border-slate-100 px-5 py-4 md:grid-cols-[1fr_1fr_1fr_auto] md:items-end">
      <Field label="Sponsor">
        <select
          className="block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          value={brandId}
          onChange={(e) => setBrandId(e.target.value)}
        >
          {sponsors.map((id) => (
            <option key={id} value={id}>
              {names[id] ?? id}
            </option>
          ))}
        </select>
      </Field>
      <Field label="Photo or link">
        {file ? (
          <div className="flex items-center gap-2 text-sm">
            <span className="truncate">{file.name}</span>
            <button type="button" className="text-slate-500 underline" onClick={() => setFile(null)}>
              clear
            </button>
          </div>
        ) : (
          <div className="flex gap-2">
            <Input placeholder="https://instagram.com/p/..." value={link} onChange={(e) => setLink(e.target.value)} />
            <label className="cursor-pointer whitespace-nowrap rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
              Photo
              <input type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </label>
          </div>
        )}
      </Field>
      <Field label="Note">
        <Input placeholder="Logo on main stage banner" value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Button type="submit" variant="secondary" busy={busy} disabled={!brandId}>
        Add
      </Button>
      {error && (
        <div className="md:col-span-4">
          <ErrorBanner message={error} />
        </div>
      )}
    </form>
  );
}

function ReportView({ report, runId }: { report: Report; runId: string }) {
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            ROI report {report.published ? <Badge tone="good">published</Badge> : <Badge tone="warn">draft</Badge>}
          </span>
        }
        subtitle={report.summary}
        action={
          <Link to={`/runs/${runId}/report`} className="text-sm text-indigo-700 hover:underline">
            Shareable view
          </Link>
        }
      />
      <ReportBody report={report} />
    </Card>
  );
}

function ReportBody({ report }: { report: Report }) {
  return (
    <div className="divide-y divide-slate-100">
      <div className="grid grid-cols-3 gap-4 px-5 py-4 text-sm">
        <Stat label="Deliverables verified" value={pct(report.overall_delivery_rate)} />
        <Stat label="Sponsors audited" value={String(report.sponsors.length)} />
        <Stat label="Disclosed gaps" value={String(report.disclosed_gaps.length)} />
      </div>
      {report.sponsors.map((s) => (
        <div key={s.brand_id} className="px-5 py-4">
          <div className="flex flex-wrap items-baseline gap-2">
            <h4 className="text-sm font-semibold">{s.name}</h4>
            <Badge>{s.tier}</Badge>
            <span className="text-sm text-slate-500">
              {inr(s.price_inr)} &middot; ~{s.expected_reach.toLocaleString("en-IN")} expected reach &middot; {s.delivered}/{s.total} verified
            </span>
          </div>
          <ul className="mt-2 space-y-1.5">
            {s.checks.map((c) => (
              <li key={c.deliverable} className="flex flex-wrap items-start gap-2 text-sm">
                <StatusBadge status={c.status} />
                <span className="font-medium text-slate-800">{c.deliverable}</span>
                <span className="text-slate-500">{c.explanation}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

/** Standalone, printable report page for sharing with sponsors. */
export default function RoiReportPage() {
  const { runId = "" } = useParams();
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.report(runId).then(setReport).catch((e: Error) => setError(e.message));
  }, [runId]);

  if (error) return <ErrorBanner message={error} />;
  if (!report) return <Loading label="Loading report" />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-wide text-slate-500">Sponsorship ROI report</p>
          <h1 className="text-xl font-semibold">{report.event.name}</h1>
          <p className="text-sm text-slate-500">
            {report.event.college} &middot; {report.event.date} &middot; generated {when(report.generated_at)}
          </p>
        </div>
        <Button variant="secondary" onClick={() => window.print()}>
          Print
        </Button>
      </div>
      {!report.published && <ErrorBanner message="This report is a draft and has not been approved by the organizer yet." />}
      <Card>
        <p className="border-b border-slate-100 px-5 py-4 text-sm text-slate-700">{report.summary}</p>
        <ReportBody report={report} />
      </Card>
      {report.disclosed_gaps.length > 0 && (
        <p className="text-sm text-slate-500">
          Items marked missing, ambiguous or not verified could not be confirmed from the evidence and are disclosed rather than assumed delivered.
        </p>
      )}
    </div>
  );
}

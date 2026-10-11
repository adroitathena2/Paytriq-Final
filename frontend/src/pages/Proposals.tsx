import { useState } from "react";
import ApprovalCard from "../components/ApprovalCard";
import { Badge, Button, Card, EmptyState, Input, StatusBadge, Textarea } from "../components/ui";
import { api } from "../lib/api";
import { agentLabel, inr } from "../lib/format";
import type { AgentMessage, EmailDraft, Proposal } from "../lib/types";
import type { StepProps } from "./EventsDetail";

/** Proposals step: every proposal with its revision history, and the send approval gate. */
export default function Proposals({ runId, detail, busy, act }: StepProps) {
  const proposals = Object.values(detail.values.proposals ?? {});
  const brands = Object.fromEntries((detail.values.brands ?? []).map((b) => [b.id, b]));
  const messages = detail.values.messages ?? [];
  const gate = detail.interrupt?.gate === "send" ? detail.interrupt : null;

  return (
    <div className="space-y-6">
      {gate && (
        <SendApproval
          key={(gate.payload.emails ?? []).map((e) => `${e.brand_id}-${e.version}-${e.kind}`).join(",")}
          emails={gate.payload.emails ?? []}
          busy={busy}
          onApprove={(edits) => act(() => api.approve(runId, "send", "approve", edits))}
          onReject={(feedback) => act(() => api.approve(runId, "send", "reject", { feedback }))}
        />
      )}
      {proposals.length === 0 ? (
        <EmptyState title="No proposals yet" hint="Proposals are written after you approve the shortlist." />
      ) : (
        proposals.map((p) => (
          <ProposalCard key={p.brand_id} proposal={p} brandName={brands[p.brand_id]?.name ?? p.brand_id} revisions={revisionsFor(messages, p.brand_id)} />
        ))
      )}
    </div>
  );
}

function revisionsFor(messages: AgentMessage[], brandId: string) {
  return messages.filter((m) => m.type === "revision_request" && m.content.brand_id === brandId);
}

const SOURCE_LABEL: Record<string, string> = {
  critique: "Outreach critique",
  brand_reply: "Brand pushback",
  organizer: "Your feedback",
};

function ProposalCard({ proposal, brandName, revisions }: { proposal: Proposal; brandName: string; revisions: AgentMessage[] }) {
  const [open, setOpen] = useState(false);
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3 border-b border-slate-100 px-5 py-3.5">
        <h3 className="text-sm font-semibold">{brandName}</h3>
        <Badge>v{proposal.version}</Badge>
        <StatusBadge status={proposal.status} />
        {proposal.pdf_url && (
          <a href={proposal.pdf_url} target="_blank" rel="noreferrer" className="ml-auto text-sm text-indigo-700 hover:underline">
            Open PDF
          </a>
        )}
      </div>
      <div className="grid gap-px bg-slate-100 sm:grid-cols-3">
        {proposal.tiers.map((t) => (
          <div key={t.name} className="bg-white px-5 py-4">
            <div className="flex items-baseline justify-between">
              <span className="text-sm font-semibold">{t.name}</span>
              <span className="text-sm tabular-nums text-slate-700">{inr(t.price_inr)}</span>
            </div>
            <ul className="mt-2 space-y-1 text-sm text-slate-600">
              {t.deliverables.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
            <p className="mt-2 text-xs text-slate-500">~{t.expected_reach.toLocaleString("en-IN")} people reached</p>
          </div>
        ))}
      </div>
      <div className="px-5 py-3.5">
        <p className={open ? "text-sm text-slate-700" : "line-clamp-2 text-sm text-slate-700"}>{proposal.pitch}</p>
        <button className="mt-1 text-xs text-slate-500 hover:text-slate-800" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show full pitch"}
        </button>
        {revisions.length > 0 && (
          <ul className="mt-3 space-y-1.5">
            {revisions.map((r) => (
              <li key={r.id} className="border-l-4 border-indigo-500 bg-slate-50 px-3 py-1.5 text-sm text-slate-700">
                <span className="font-medium">{SOURCE_LABEL[String(r.content.source)] ?? agentLabel(r.from_agent)}:</span>{" "}
                {String(r.content.constraint ?? r.reasoning)}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function SendApproval({
  emails,
  busy,
  onApprove,
  onReject,
}: {
  emails: EmailDraft[];
  busy: boolean;
  onApprove: (edits: Record<string, unknown>) => void;
  onReject: (feedback: string) => void;
}) {
  const [drafts, setDrafts] = useState(() => Object.fromEntries(emails.map((e) => [e.brand_id, { subject: e.subject, body: e.body, include: true }])));
  const [feedback, setFeedback] = useState("");
  const included = Object.values(drafts).filter((d) => d.include).length;

  const update = (id: string, patch: Partial<(typeof drafts)[string]>) => setDrafts({ ...drafts, [id]: { ...drafts[id], ...patch } });

  function approve() {
    const exclude = Object.entries(drafts).filter(([, d]) => !d.include).map(([id]) => id);
    const edited = Object.fromEntries(Object.entries(drafts).map(([id, d]) => [id, { subject: d.subject, body: d.body }]));
    onApprove({ exclude, emails: edited });
  }

  return (
    <ApprovalCard
      title="Approve emails before they are sent"
      description="Outreach critiqued every proposal and wrote these emails. Edit anything, untick what should not go out."
    >
      <div className="space-y-4">
        {emails.map((e) => (
          <div key={e.brand_id} className="rounded-md border border-slate-200 bg-white p-4">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <input
                type="checkbox"
                className="h-4 w-4 accent-indigo-600"
                checked={drafts[e.brand_id].include}
                onChange={(ev) => update(e.brand_id, { include: ev.target.checked })}
                aria-label={`Send to ${e.brand_name}`}
              />
              <span className="text-sm font-semibold">{e.brand_name}</span>
              <Badge tone={e.kind === "initial" ? "neutral" : "accent"}>{e.kind}</Badge>
              <Badge>proposal v{e.version}</Badge>
              {e.pdf_url && (
                <a href={e.pdf_url} target="_blank" rel="noreferrer" className="ml-auto text-xs text-indigo-700 hover:underline">
                  PDF
                </a>
              )}
            </div>
            <Input value={drafts[e.brand_id].subject} onChange={(ev) => update(e.brand_id, { subject: ev.target.value })} aria-label="Subject" />
            <Textarea className="mt-2" rows={5} value={drafts[e.brand_id].body} onChange={(ev) => update(e.brand_id, { body: ev.target.value })} aria-label="Body" />
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap items-end gap-3">
        <Button busy={busy} disabled={included === 0} onClick={approve}>
          Send {included} email{included === 1 ? "" : "s"}
        </Button>
        <div className="flex min-w-[260px] flex-1 items-end gap-2">
          <Textarea rows={1} placeholder="Or ask for changes to the proposals first" value={feedback} onChange={(e) => setFeedback(e.target.value)} />
          <Button variant="secondary" disabled={busy || !feedback.trim()} onClick={() => onReject(feedback.trim())}>
            Request changes
          </Button>
        </div>
      </div>
    </ApprovalCard>
  );
}

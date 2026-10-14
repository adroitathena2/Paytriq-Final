import { useState } from "react";
import ApprovalCard from "../components/ApprovalCard";
import { Badge, Button, Card, CardHeader, EmptyState, Table, Textarea } from "../components/ui";
import { api } from "../lib/api";
import type { Brand } from "../lib/types";
import type { StepProps } from "./EventsDetail";

/** Discover step: ranked brands, and the shortlist approval gate. */
export default function Brands({ runId, detail, busy, act }: StepProps) {
  const brands = detail.values.brands ?? [];
  const gate = detail.interrupt?.gate === "shortlist" ? detail.interrupt : null;
  const selectedIds = new Set(detail.values.selected_brand_ids ?? []);
  const replans = (detail.values.messages ?? []).filter((m) => m.type === "replan");

  if (!brands.length) {
    if (detail.values.phase === "discovery_failed")
      return (
        <EmptyState
          title="No businesses found"
          hint="The map services did not return results. Try again in a minute."
          action={<Button busy={busy} onClick={() => act(() => api.continueRun(runId))}>Retry discovery</Button>}
        />
      );
    return <EmptyState title="Searching near campus" hint="The Discovery agent is finding and ranking businesses. This takes a minute." />;
  }

  return (
    <div className="space-y-6">
      {replans.length > 0 && (
        <div className="rounded-md border-l-4 border-indigo-500 bg-indigo-50/50 px-4 py-3 text-sm text-slate-700">
          <span className="font-medium">Discovery replanned {replans.length}x: </span>
          {replans.map((r) => r.reasoning).join(" ")}
        </div>
      )}
      {gate ? (
        <ShortlistApproval
          key={gate.payload.suggested_brand_ids?.join(",")}
          brands={brands}
          suggested={gate.payload.suggested_brand_ids ?? []}
          busy={busy}
          onApprove={(ids) => act(() => api.approve(runId, "shortlist", "approve", { selected_brand_ids: ids }))}
          onReject={(feedback) => act(() => api.approve(runId, "shortlist", "reject", { feedback }))}
        />
      ) : (
        <Card>
          <CardHeader title="Ranked brands" subtitle={`${brands.length} found, ${selectedIds.size} on the shortlist`} />
          <BrandTable brands={brands} isChecked={(b) => selectedIds.has(b.id)} />
        </Card>
      )}
    </div>
  );
}

function ShortlistApproval({
  brands,
  suggested,
  busy,
  onApprove,
  onReject,
}: {
  brands: Brand[];
  suggested: string[];
  busy: boolean;
  onApprove: (ids: string[]) => void;
  onReject: (feedback: string) => void;
}) {
  const [chosen, setChosen] = useState<Set<string>>(new Set(suggested));
  const [feedback, setFeedback] = useState("");

  const toggle = (id: string) => {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setChosen(next);
  };

  return (
    <ApprovalCard
      title="Approve the sponsor shortlist"
      description="Proposals are written only for the brands you keep. The agents suggested the best fits."
    >
      <div className="-mx-5">
        <BrandTable brands={brands} isChecked={(b) => chosen.has(b.id)} onToggle={toggle} />
      </div>
      <div className="mt-4 flex flex-wrap items-end gap-3">
        <Button busy={busy} disabled={chosen.size === 0} onClick={() => onApprove([...chosen])}>
          Approve {chosen.size} brand{chosen.size === 1 ? "" : "s"}
        </Button>
        <div className="flex min-w-[260px] flex-1 items-end gap-2">
          <Textarea
            rows={1}
            placeholder="Or tell Discovery what to change, e.g. 'no gyms, more coaching centres'"
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
          />
          <Button variant="secondary" disabled={busy || !feedback.trim()} onClick={() => onReject(feedback.trim())}>
            Search again
          </Button>
        </div>
      </div>
    </ApprovalCard>
  );
}

function BrandTable({ brands, isChecked, onToggle }: { brands: Brand[]; isChecked: (b: Brand) => boolean; onToggle?: (id: string) => void }) {
  return (
    <Table head={[onToggle ? "Keep" : "Shortlist", "Brand", "Category", "Distance", "Fit", "Contact", "Why"]}>
      {brands.map((b) => (
        <tr key={b.id} className={isChecked(b) ? "bg-indigo-50/30" : undefined}>
          <td className="px-4 py-2.5">
            {onToggle ? (
              <input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={isChecked(b)} onChange={() => onToggle(b.id)} aria-label={`Keep ${b.name}`} />
            ) : (
              isChecked(b) && <Badge tone="accent">selected</Badge>
            )}
          </td>
          <td className="px-4 py-2.5">
            <div className="font-medium text-slate-900">{b.name}</div>
            <div className="text-xs text-slate-500">{b.address}</div>
          </td>
          <td className="px-4 py-2.5 capitalize text-slate-600">{b.category}</td>
          <td className="px-4 py-2.5 text-slate-600">{b.distance_km} km</td>
          <td className="px-4 py-2.5">
            <FitScore score={b.fit_score} />
          </td>
          <td className="px-4 py-2.5 text-slate-600">{b.contact_email ? <span title={b.contact_email}>email</span> : <span className="text-slate-400">none</span>}</td>
          <td className="max-w-xs px-4 py-2.5 text-slate-600">{b.fit_reason}</td>
        </tr>
      ))}
    </Table>
  );
}

function FitScore({ score }: { score: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-14 rounded-full bg-slate-200">
        <div className={`h-1.5 rounded-full ${score >= 0.6 ? "bg-indigo-600" : "bg-slate-400"}`} style={{ width: `${Math.round(score * 100)}%` }} />
      </div>
      <span className="text-xs tabular-nums text-slate-600">{score.toFixed(2)}</span>
    </div>
  );
}

import { Fragment, useState } from "react";
import { Badge, Button, Card, CardHeader, EmptyState, StatusBadge, Table } from "../components/ui";
import { api } from "../lib/api";
import { when } from "../lib/format";
import type { HistoryEntry, OutreachThread } from "../lib/types";
import type { StepProps } from "./EventsDetail";

/** Outreach step: email threads with their status, replies and the agent's classification. */
export default function Outreach({ runId, detail, busy, act }: StepProps) {
  const threads = Object.values(detail.values.outreach ?? {});
  const brands = Object.fromEntries((detail.values.brands ?? []).map((b) => [b.id, b]));
  const live = threads.some((t) => t.thread_id);
  const [open, setOpen] = useState<string | null>(null);
  const canPoll = detail.run.status !== "running" && detail.run.status !== "waiting_approval";

  const poll = (
    <Button busy={busy} disabled={!live || !canPoll} onClick={() => act(() => api.pollReplies(runId))}>
      Check for replies
    </Button>
  );

  if (!threads.length) return <EmptyState title="Nothing sent yet" hint="Emails go out after you approve them in the Proposals step." />;

  return (
    <Card>
      <CardHeader
        title="Email threads"
        subtitle="Outreach reads every reply, then routes pushback to Proposal or asks Discovery for a replacement."
        action={poll}
      />
      <Table head={["Brand", "Status", "Reply type", "Latest reply", "Last activity"]}>
        {threads.map((t) => {
          const reply = lastOf(t, "reply");
          const lastTs = t.history[t.history.length - 1]?.ts;
          return (
            <Fragment key={t.brand_id}>
              <tr className="cursor-pointer hover:bg-slate-50" onClick={() => setOpen(open === t.brand_id ? null : t.brand_id)}>
                <td className="px-4 py-2.5 font-medium">{brands[t.brand_id]?.name ?? t.brand_id}</td>
                <td className="px-4 py-2.5">
                  <StatusBadge status={t.status} />
                </td>
                <td className="px-4 py-2.5 text-slate-600">{t.last_reply_class?.replace(/_/g, " ") ?? "-"}</td>
                <td className="max-w-sm truncate px-4 py-2.5 text-slate-600">{reply ? String(reply.text) : <span className="text-slate-400">no reply yet</span>}</td>
                <td className="px-4 py-2.5 text-slate-500">{when(lastTs)}</td>
              </tr>
              {open === t.brand_id && (
                <tr>
                  <td colSpan={5} className="bg-slate-50 px-4 py-3">
                    <ThreadHistory thread={t} />
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </Table>
    </Card>
  );
}

function lastOf(thread: OutreachThread, type: string): HistoryEntry | undefined {
  return [...thread.history].reverse().find((e) => e.type === type);
}

function describe(e: HistoryEntry): string {
  switch (e.type) {
    case "email_draft":
      return `${String(e.kind)} email (${String(e.state)}): ${String(e.subject)}`;
    case "sent":
      return `Sent to ${String(e.to)}${e.redirected_to_demo ? " (demo inbox)" : ""}: ${String(e.subject)}`;
    case "reply":
      return `Reply from ${String(e.from)}: ${String(e.text)}`;
    case "classified":
      return `Classified as ${String(e.label).replace(/_/g, " ")}: ${String(e.summary)}`;
    case "accepted":
      return `Accepted the ${String(e.tier)} tier`;
    default:
      return e.type;
  }
}

function ThreadHistory({ thread }: { thread: OutreachThread }) {
  return (
    <ol className="space-y-1.5 text-sm">
      {thread.history.map((e, i) => (
        <li key={i} className="flex gap-3">
          <span className="w-28 shrink-0 text-xs text-slate-400">{when(e.ts)}</span>
          <Badge>{e.type.replace(/_/g, " ")}</Badge>
          <span className="whitespace-pre-line text-slate-700">{describe(e)}</span>
        </li>
      ))}
    </ol>
  );
}

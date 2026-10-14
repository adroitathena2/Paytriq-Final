import { accessToken } from "./auth";
import type { Evidence, EventRow, Gate, Health, RoiReport, RunDetail, RunRow, TraceEntry, TraceExport } from "./types";

export const API_URL = ((import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

/** Turns FastAPI error bodies (string or validation list) into one readable sentence. */
function describe(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: (string | number)[]; msg?: string }) => `${String(d.loc?.[d.loc.length - 1] ?? "field")}: ${d.msg}`)
      .join("; ");
  }
  return status >= 500 ? "Something went wrong on the server. Please retry." : `Request failed (${status}).`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await accessToken();
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (init.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("Could not reach the Paytriq server. Check your connection and try again.", 0);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(describe(body.detail, res.status), res.status);
  }
  return (await res.json()) as T;
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  health: () => request<Health>("/health"),
  listEvents: () => request<EventRow[]>("/events"),
  getEvent: (id: string) => request<EventRow>(`/events/${id}`),
  createEvent: (body: Omit<EventRow, "id" | "created_at" | "runs">) => request<EventRow>("/events", json(body)),
  startRun: (eventId: string) => request<RunRow>(`/events/${eventId}/runs`, { method: "POST" }),
  listRuns: () => request<RunRow[]>("/runs"),
  getRun: (runId: string) => request<RunDetail>(`/runs/${runId}`),
  continueRun: (runId: string) => request<{ status: string }>(`/runs/${runId}/continue`, { method: "POST" }),
  approve: (runId: string, gate: Gate, decision: "approve" | "reject", edits: Record<string, unknown> = {}) =>
    request<{ status: string }>(`/runs/${runId}/approve`, json({ gate, decision, edits })),
  pollReplies: (runId: string) => request<{ status: string }>(`/runs/${runId}/poll-replies`, { method: "POST" }),
  listEvidence: (runId: string) => request<Evidence[]>(`/runs/${runId}/evidence`),
  addEvidence: (runId: string, form: FormData) => request<Evidence>(`/runs/${runId}/evidence`, { method: "POST", body: form }),
  startAudit: (runId: string, actualFootfall: number | null = null) =>
    request<{ status: string }>(`/runs/${runId}/audit`, json({ actual_footfall: actualFootfall })),
  publishWithGaps: (runId: string) => request<{ status: string }>(`/runs/${runId}/publish-with-gaps`, { method: "POST" }),
  createDemoEvent: (rehearsal: boolean) => request<EventRow>("/events/demo", json({ rehearsal })),
  addDemoEvidence: (runId: string) => request<{ added: number }>(`/runs/${runId}/demo-evidence`, { method: "POST" }),
  report: (runId: string) => request<RoiReport>(`/runs/${runId}/report`),
  trace: (runId: string) => request<TraceEntry[]>(`/runs/${runId}/trace`),

  /** Downloads the unedited trace JSON exactly as the server exports it. */
  async downloadTrace(runId: string) {
    const token = await accessToken();
    const res = await fetch(`${API_URL}/runs/${runId}/trace/export`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new ApiError("Could not export the trace.", res.status);
    // Download the server's bytes unchanged; only peek at the "rehearsal" flag to name the file.
    const text = await res.text();
    let rehearsal = false;
    try {
      rehearsal = (JSON.parse(text) as Partial<TraceExport>).rehearsal === true;
    } catch {
      rehearsal = false;
    }
    const blob = new Blob([text], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${rehearsal ? "REHEARSAL-NOT-FOR-SUBMISSION-" : ""}paytriq-trace-${runId}.json`;
    link.click();
    // revoking synchronously can cancel the download in Firefox/Safari
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  },
};

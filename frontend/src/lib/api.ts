import { accessToken } from "./auth";
import type { Evidence, EventRow, Gate, Health, RoiReport, RunDetail, RunRow, TraceEntry } from "./types";

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
  startAudit: (runId: string) => request<{ status: string }>(`/runs/${runId}/audit`, { method: "POST" }),
  report: (runId: string) => request<RoiReport>(`/runs/${runId}/report`),
  trace: (runId: string) => request<TraceEntry[]>(`/runs/${runId}/trace`),

  /** Downloads the unedited trace JSON exactly as the server exports it. */
  async downloadTrace(runId: string) {
    const token = await accessToken();
    const res = await fetch(`${API_URL}/runs/${runId}/trace/export`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!res.ok) throw new ApiError("Could not export the trace.", res.status);
    const blob = await res.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `paytriq-trace-${runId}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  },
};

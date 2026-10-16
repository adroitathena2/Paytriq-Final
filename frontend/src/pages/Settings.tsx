import { useEffect, useState, type ReactNode } from "react";
import { Badge, Button, Card, CardHeader, ErrorBanner, PageHeader } from "../components/ui";
import { API_URL, api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { authEnabled } from "../lib/supabase";
import type { Health } from "../lib/types";

export default function Settings() {
  const { email, signOut } = useAuth();
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.health().then(setHealth).catch((e: Error) => setError(e.message));
  }, []);

  const rows: [string, ReactNode][] = [
    ["Signed in as", email ?? "-"],
    ["API", API_URL],
    ["Server", health ? <Badge tone="good">online</Badge> : error ? <Badge tone="bad">unreachable</Badge> : "checking..."],
    ["Demo mode", health ? (health.demo_mode ? <Badge tone="accent">on: emails go to team inboxes</Badge> : <Badge tone="warn">off: real emails</Badge>) : "-"],
    ["LLM", health ? (health.llm_configured ? "Gemini configured" : <Badge tone="bad">GEMINI_API_KEY missing</Badge>) : "-"],
    ["Storage of runs", health?.database ?? "-"],
  ];

  return (
    <>
      <PageHeader title="Settings" />
      {error && <ErrorBanner message={error} />}
      <Card className="mt-4 max-w-2xl">
        <CardHeader title="Account and system" />
        <dl className="divide-y divide-slate-100">
          {rows.map(([label, value]) => (
            <div key={label} className="grid grid-cols-3 gap-4 px-5 py-3 text-sm">
              <dt className="text-slate-500">{label}</dt>
              <dd className="col-span-2 text-slate-800">{value}</dd>
            </div>
          ))}
        </dl>
        {authEnabled && (
          <div className="border-t border-slate-100 px-5 py-4">
            <Button variant="secondary" onClick={signOut}>
              Sign out
            </Button>
          </div>
        )}
      </Card>
    </>
  );
}

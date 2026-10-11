import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { Button, ErrorBanner, Field, Input } from "../components/ui";
import { useAuth } from "../lib/auth";
import { supabase } from "../lib/supabase";

export default function Login() {
  const { signedIn } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (signedIn) return <Navigate to="/events" replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!supabase) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    const { data, error } =
      mode === "signin"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password });
    setBusy(false);
    if (error) return setError(error.message);
    if (mode === "signup" && !data.session) return setNotice("Check your inbox to confirm your email, then sign in.");
    navigate("/events");
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2">
          <img src="/favicon.svg" alt="" className="h-7 w-7" />
          <span className="text-lg font-semibold">Paytriq</span>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-6">
          <h1 className="text-base font-semibold">{mode === "signin" ? "Sign in" : "Create an account"}</h1>
          <p className="mt-1 text-sm text-slate-500">Find, pitch and close local sponsors for your college event.</p>
          <form onSubmit={submit} className="mt-5 space-y-4">
            <Field label="Email">
              <Input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            </Field>
            <Field label="Password" hint={mode === "signup" ? "At least 6 characters." : undefined}>
              <Input
                type="password"
                required
                minLength={6}
                autoComplete={mode === "signin" ? "current-password" : "new-password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            {error && <ErrorBanner message={error} />}
            {notice && <p className="rounded-md bg-indigo-50 px-3 py-2 text-sm text-indigo-800">{notice}</p>}
            <Button type="submit" busy={busy} className="w-full">
              {mode === "signin" ? "Sign in" : "Sign up"}
            </Button>
          </form>
        </div>
        <button
          className="mt-4 text-sm text-slate-600 hover:text-slate-900"
          onClick={() => setMode(mode === "signin" ? "signup" : "signin")}
        >
          {mode === "signin" ? "New here? Create an account" : "Already have an account? Sign in"}
        </button>
      </div>
    </div>
  );
}

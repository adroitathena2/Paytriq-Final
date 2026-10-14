import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import { authEnabled, supabase } from "./supabase";

interface AuthState {
  loading: boolean;
  email: string | null;
  signedIn: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({ loading: true, email: null, signedIn: false, signOut: async () => {} });

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(authEnabled);

  useEffect(() => {
    if (!supabase) return;
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setLoading(false);
    });
    const { data } = supabase.auth.onAuthStateChange((_event, next) => setSession(next));
    return () => data.subscription.unsubscribe();
  }, []);

  const value: AuthState = authEnabled
    ? {
        loading,
        email: session?.user.email ?? null,
        signedIn: !!session,
        signOut: async () => {
          await supabase?.auth.signOut();
        },
      }
    : { loading: false, email: "dev@localhost", signedIn: true, signOut: async () => {} };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}

export async function accessToken(): Promise<string | null> {
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

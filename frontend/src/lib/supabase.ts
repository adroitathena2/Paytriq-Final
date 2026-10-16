import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

/** Null in local development without Supabase (the backend then runs with AUTH_DISABLED=true). */
export const supabase: SupabaseClient | null = url && anonKey ? createClient(url, anonKey) : null;

export const authEnabled = supabase !== null;

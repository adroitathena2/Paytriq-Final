-- Demo mode: an event can be a fixture-world demo ('demo': real Gemini reasoning, fixture businesses,
-- inbox and evidence) or an offline rehearsal ('rehearsal': scripted LLM, never for submission).
-- Normal events keep demo_scenario null.

alter table public.events
  add column if not exists demo_scenario text check (demo_scenario in ('demo', 'rehearsal'));

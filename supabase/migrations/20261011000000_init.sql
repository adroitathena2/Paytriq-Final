-- Paytriq initial schema: events, runs, agent trace, ROI evidence, storage buckets.
-- The backend connects with the service connection (bypasses RLS) and checks ownership itself;
-- RLS protects direct access from the browser with the anon key.
-- LangGraph's checkpoint tables are created by the backend on startup (AsyncPostgresSaver.setup()).

create extension if not exists pgcrypto;

create table if not exists public.events (
  id                uuid primary key default gen_random_uuid(),
  owner_id          uuid not null,
  name              text not null check (char_length(name) between 2 and 120),
  college           text not null,
  event_date        date not null,
  expected_footfall integer not null check (expected_footfall > 0),
  audience          text not null,
  budget_goal_inr   integer not null check (budget_goal_inr >= 0),
  lat               double precision not null check (lat between -90 and 90),
  lng               double precision not null check (lng between -180 and 180),
  created_at        timestamptz not null default now()
);
create index if not exists events_owner_idx on public.events (owner_id, created_at desc);

create table if not exists public.runs (
  id           uuid primary key default gen_random_uuid(),
  event_id     uuid not null references public.events (id) on delete cascade,
  owner_id     uuid not null,
  status       text not null default 'running'
               check (status in ('running', 'waiting_approval', 'idle', 'completed', 'failed')),
  phase        text not null default 'discovery',
  pending_gate text check (pending_gate in ('shortlist', 'send', 'roi_publish')),
  last_error   text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists runs_event_idx on public.runs (event_id, created_at desc);

create table if not exists public.agent_trace (
  id         bigint generated always as identity primary key,
  run_id     uuid not null references public.runs (id) on delete cascade,
  ts         timestamptz not null default now(),
  agent      text not null,
  event_type text not null,
  payload    jsonb not null default '{}'::jsonb
);
create index if not exists agent_trace_run_idx on public.agent_trace (run_id, id);

create table if not exists public.evidence (
  id           uuid primary key default gen_random_uuid(),
  run_id       uuid not null references public.runs (id) on delete cascade,
  brand_id     text not null,
  kind         text not null check (kind in ('photo', 'link')),
  url          text,
  storage_path text,
  mime_type    text,
  note         text,
  created_at   timestamptz not null default now(),
  check ((kind = 'photo' and storage_path is not null) or (kind = 'link' and url is not null))
);
create index if not exists evidence_run_idx on public.evidence (run_id, created_at);

-- Row level security: users only ever see their own data.
alter table public.events      enable row level security;
alter table public.runs        enable row level security;
alter table public.agent_trace enable row level security;
alter table public.evidence    enable row level security;

create policy "events: owner can read"   on public.events for select using (owner_id = auth.uid());
create policy "events: owner can insert" on public.events for insert with check (owner_id = auth.uid());
create policy "events: owner can update" on public.events for update using (owner_id = auth.uid());
create policy "events: owner can delete" on public.events for delete using (owner_id = auth.uid());

create policy "runs: owner can read" on public.runs for select using (owner_id = auth.uid());

create policy "trace: run owner can read" on public.agent_trace for select
  using (exists (select 1 from public.runs r where r.id = agent_trace.run_id and r.owner_id = auth.uid()));

create policy "evidence: run owner can read" on public.evidence for select
  using (exists (select 1 from public.runs r where r.id = evidence.run_id and r.owner_id = auth.uid()));

-- Storage: proposal PDFs are public (linked from emails), evidence photos are private.
insert into storage.buckets (id, name, public)
values ('proposals', 'proposals', true), ('evidence', 'evidence', false)
on conflict (id) do nothing;

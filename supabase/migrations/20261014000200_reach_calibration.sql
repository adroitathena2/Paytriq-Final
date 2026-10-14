-- L8 ROI calibration: how much of the estimated reach was really delivered, per organizer and category.
-- factor = verified-deliverable reach at actual footfall / estimated reach, kept as a running average.
-- Written only when the organizer approves publishing an ROI report; read by the Proposal agent.

create table if not exists public.reach_calibration (
  owner_id   uuid not null,
  category   text not null,
  factor     double precision not null check (factor >= 0),
  n_events   integer not null default 1 check (n_events >= 1),
  updated_at timestamptz not null default now(),
  primary key (owner_id, category)
);

alter table public.reach_calibration enable row level security;
create policy "calibration: owner can read" on public.reach_calibration for select using (owner_id = auth.uid());

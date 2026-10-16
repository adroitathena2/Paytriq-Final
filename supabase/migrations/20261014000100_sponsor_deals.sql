-- Accepted-deal memory: one row per sponsor that accepted a tier (final price after any counter-offer).
-- Used by the supervisor's A2 arbitration (price vs past deals of the same owner and category).
-- event_id is nullable so demo mode can seed past deals that do not belong to a real event.

create table if not exists public.sponsor_deals (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null,
  event_id    uuid references public.events (id) on delete set null,
  brand_id    text not null,
  category    text not null,
  tier        text not null check (tier in ('Bronze', 'Silver', 'Gold')),
  price_inr   integer not null check (price_inr >= 0),
  accepted_at timestamptz not null default now()
);
create index if not exists sponsor_deals_owner_category_idx on public.sponsor_deals (owner_id, category);

alter table public.sponsor_deals enable row level security;
create policy "deals: owner can read" on public.sponsor_deals for select using (owner_id = auth.uid());

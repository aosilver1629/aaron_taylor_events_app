-- SF Events Voting Workflow — schema
-- Run against Supabase (Postgres). Requires pgcrypto for gen_random_uuid().

create extension if not exists pgcrypto;

create table if not exists events (
  id uuid primary key default gen_random_uuid(),
  event_key text unique not null,      -- sha256(normalize(title)|date|venue)
  title text not null,
  start_at timestamptz not null,
  end_at timestamptz,
  venue text,
  neighborhood text,
  category text,                        -- concert|festival|market|food|outdoor|art|other
  price_range text,
  url text,
  pitch text,                           -- one line on why it might appeal
  source text,
  discovered_at timestamptz default now(),
  calendar_event_id text                -- null until written to calendar
);

create table if not exists ballots (
  id uuid primary key default gen_random_uuid(),
  event_id uuid references events(id),
  person text not null,                 -- 'aaron' | 'tay'
  batch_id uuid not null,               -- one per SMS send
  list_number int not null,             -- the number shown in that SMS
  response text,                        -- null | 'yes' | 'no' | 'expired'
  sent_at timestamptz default now(),
  responded_at timestamptz,
  unique (event_id, person),
  unique (batch_id, person, list_number)
);

create table if not exists sms_log (
  id uuid primary key default gen_random_uuid(),
  person text,
  direction text,                       -- 'in' | 'out'
  body text,
  created_at timestamptz default now()
);

-- Indexes to support the query patterns the jobs actually run.
create index if not exists idx_ballots_person_batch on ballots (person, batch_id);
create index if not exists idx_ballots_event on ballots (event_id);
create index if not exists idx_ballots_response on ballots (response);
create index if not exists idx_events_event_key on events (event_key);
create index if not exists idx_events_calendar_event_id on events (calendar_event_id);
create index if not exists idx_sms_log_person on sms_log (person);

-- sms_log is append-only: a bad parse must be replayable against the raw text.
-- No deletes, no updates. Enforced by convention (the app never issues them),
-- not by a DB trigger, since the service account is the only writer.

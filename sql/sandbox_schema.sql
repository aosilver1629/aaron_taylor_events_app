-- Sandbox events table for research-module tuning (research-tuning branch).
-- Mirrors `events` exactly, column for column, so app.models.Event and
-- Repository code work against it completely unchanged — the only
-- difference is which table Repository(events_table=...) points at.
--
-- Never referenced by ballots or sms_log. Disposable by design: clear and
-- repopulate freely while tuning (see sandbox/reset_events_sandbox.py).
create table if not exists events_sandbox (
  id uuid primary key default gen_random_uuid(),
  event_key text unique not null,
  title text not null,
  start_at timestamptz not null,
  end_at timestamptz,
  venue text,
  neighborhood text,
  category text,
  price_range text,
  url text,
  pitch text,
  source text,
  discovered_at timestamptz default now(),
  calendar_event_id text          -- unused in sandbox; kept so the schema
                                   -- matches `events` column-for-column
);

create index if not exists idx_events_sandbox_event_key on events_sandbox (event_key);

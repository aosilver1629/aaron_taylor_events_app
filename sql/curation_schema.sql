-- Curation layer — experimental, research-tuning branch only. Not yet
-- folded into sql/schema.sql; run manually in the Supabase SQL Editor.
--
-- Phase 1: source registry with health tracking.

create table if not exists sources (
  id uuid primary key default gen_random_uuid(),
  label text unique not null,
  city text not null default 'sf',
  url text not null,
  kind text not null,                -- 'venue' | 'comedy' | 'fairs' | 'food'
  preferred_tier int not null,       -- 1 parser | 2 llm | 4 search (0 api, 3 browser reserved)
  parser_id text,                    -- 'chapel' | 'gamh' | '1015folsom' | null
  extraction_rules text,             -- the per-kind instruction template
  enabled boolean not null default true,
  status text not null default 'healthy',  -- healthy|degraded|silent|blocked
  status_reason text,
  consecutive_zero_runs int not null default 0,
  created_at timestamptz default now()
);

create table if not exists source_runs (
  id uuid primary key default gen_random_uuid(),
  source_id uuid references sources(id) not null,
  run_at timestamptz default now(),
  fetch_method text not null,        -- 'http' | 'search_fallback'
  extract_method text not null,      -- 'regex' | 'llm' | 'none'
  candidates int not null,
  blocked_marker_seen boolean not null default false
);
create index if not exists idx_source_runs_source on source_runs (source_id, run_at desc);

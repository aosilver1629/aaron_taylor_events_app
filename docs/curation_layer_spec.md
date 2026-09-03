# Curation Layer — implementation spec

This is a handoff spec for a coding agent. It describes three additions to the
existing SF events app, in build order: **(1) a source registry with health
tracking, (2) an enrichment & tagging pass, (3) a declarative taste profile**.
The full design rationale, UX mockups, and diagrams live in the companion
design doc ("The Curation Layer" artifact); this file is the executable
subset. Where this spec is silent, follow the conventions already in this
repo (see `docs/BUILD_NOTES.md`).

## Context you need before writing code

Read these first — every phase below plugs into them:

- `app/jobs/research_job.py` — Job 1. Weekly run: fetch structured sources
  (Ticketmaster/Bandsintown) → `run_deterministic_research_call` → per-candidate
  `validate_candidate` → optional `rank_and_select` cap at 12 → `repo.insert_event`.
- `app/research/deterministic_search.py` — the curated-source fetch/extract
  pipeline. Its module-level `SOURCES` list and `_KIND_INSTRUCTIONS` dict are
  what Phase 1 moves into the database. Note its per-source flow: plain HTTP →
  heuristic blocked/empty check → web_search fallback; regex parser first when
  one exists, LLM extraction otherwise.
- `app/research/preferences.py` — the current learned-preference summary
  (category/venue counts over the last 50 resolved ballots). Phase 3 extends it.
- `app/db.py` (`Repository`), `tests/fake_repo.py` (`FakeRepository`) — all DB
  access goes through `Repository`; every new method must be mirrored in
  `FakeRepository` with the same semantics, because the whole test suite runs
  against the fake.
- `app/models.py`, `sql/schema.sql` — dataclass shapes and the Postgres schema.
- Conventions to preserve: model IDs live in module constants (reuse
  `EXTRACT_MODEL = "claude-sonnet-5"` for new extraction/tagging calls and
  `"claude-haiku-4-5-20251001"` where a cheap call suffices, matching
  `deterministic_search.py`); LLM structured output uses a `strict: true` tool
  schema with the auto-then-forced two-turn pattern (see
  `_extract_from_source` and BUILD_NOTES "Research call shape") for calls that
  need reasoning, or a single forced `tool_choice` for mechanical fill-in calls
  (see `_generate_pitches`); every LLM call goes through
  `app/utils/retry.with_retry` and degrades gracefully on failure — a research
  run must never abort because one call failed; timestamps via
  `app/utils/time`; structured logging via `extra={"job_fields": {...}}`.

**The one product invariant, enforced in code and tests:** anything the user
*declared* is a contract; anything *learned* from votes is a suggestion. A
hard-excluded tag must make an event unelectable for ballots no matter what
the learned layer or ranking call says.

---

## Phase 1 — Source registry with health tracking

**Goal:** `SOURCES` becomes data; per-run yield and method become queryable
health state; broken sources become visible within one run instead of never.

### 1.1 Schema (append to `sql/schema.sql`)

```sql
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
```

### 1.2 Code

- New module `app/research/source_registry.py`:
  - `load_sources(repo, city="sf") -> list[dict]` returning dicts shaped
    exactly like today's `SOURCES` entries (label/url/kind/parser +
    extraction_rules), filtered to `enabled`.
  - `record_source_run(repo, source, fetch_method, extract_method, candidates, blocked)` —
    inserts a `source_runs` row, then recomputes status (rules in 1.3).
  - A seed path: `sql/seed_sources.sql` (or a small script) inserting the ten
    current `SOURCES` rows with their current parser assignments and the
    `_KIND_INSTRUCTIONS` template for their kind. Keep `SOURCES` in code as
    the fallback when the table is empty, so nothing breaks mid-migration.
- Modify `run_deterministic_research_call` to accept the source list as a
  parameter (loaded by the job via `load_sources`), and to call
  `record_source_run` per source with what it already logs today (`method`,
  `used`, `len(candidates)`, plus whether `_BLOCKED_MARKERS` matched).
- `Repository` + `FakeRepository`: `get_sources(city)`, `insert_source_run(...)`,
  `get_recent_source_runs(source_id, limit)`, `update_source_status(...)`.

### 1.3 Status rules (dumb on purpose — no ML, no tuning knobs)

Computed after each run, per source, from the last 8 runs:

- → `blocked` if `blocked_marker_seen` on the latest run.
- → `silent` if `candidates == 0` for 2 consecutive runs (and not blocked).
- → `degraded` if latest `candidates < 0.4 * trailing_median` (median of the
  prior runs' candidates, min 3 runs of history before this can fire).
- → `healthy` after one run with none of the above.
- Escalation: a source whose parser (`extract_method='regex'`) found nothing
  already falls through to LLM extraction today — keep that, and record the
  fallback in the run row. Do not build browser automation (tier 3) now.

### 1.4 Ops visibility

- `GET /ops/sources` endpoint in `app/main.py` returning JSON: per source —
  label, tier in use (last run's methods), status, status_reason, last 8
  candidate counts. No UI needed yet; the JSON is the testable surface.
- Log a `source_status_changed` line whenever status changes.

### 1.5 Tests (extend the FakeRepository suite)

- Registry loads and preserves today's exact source behavior (a run with the
  seeded table produces the same fetch/extract decisions as the hardcoded list).
- Status transitions: healthy→degraded on yield drop, degraded→silent on two
  zero runs, →blocked on marker, recovery on a normal run, no `degraded`
  before 3 runs of history.
- `/ops/sources` returns the expected shape.

---

## Phase 2 — Enrichment & tagging pass

**Goal:** every stored event carries taxonomy tags, entities, a one-line
factual gist, and a confidence — the substrate Phase 3 matches against.

### 2.1 Taxonomy

- New file `app/research/taxonomy.py`: a closed two-level tree as plain data.
  Seed v1 (~25 nodes) for SF, e.g.:
  `music/{folk, americana, indie, rock, punk, electronic, hiphop, jazz, classical, other}`,
  `comedy/{standup, improv, other}`, `talks/{author, political, science, other}`,
  `food/{popup, festival, market}`, `fairs/{street-fair, festival}`,
  `art/{gallery, film, theater}`, `outdoor/{run, market, other}`.
  Expose `is_valid_tag(tag: str) -> bool` and `ALL_TAGS`.
- The tagger may **propose** new nodes but never use them: proposals are
  logged (`taxonomy_proposal` job_fields) — promotion is a human editing
  `taxonomy.py`. Do not build a review UI.

### 2.2 Schema

```sql
alter table events add column if not exists tags text[] default '{}';
alter table events add column if not exists entities jsonb default '[]'; -- [{"name":..., "role":"performer|author|speaker"}]
alter table events add column if not exists gist text;
alter table events add column if not exists tag_confidence real;
```

Mirror the new fields on `EventIn` / `Event` in `app/models.py` (defaults so
existing tests keep passing) and in `Repository.insert_event` /
`FakeRepository`.

### 2.3 The tagging call

- New module `app/research/enrichment.py` with
  `enrich_events(events: list[EventIn], settings) -> None` (mutates in place):
  - **One batched call per research run**, not per event. Input: for each
    event, title + venue + category + source + any pitch text. Output via a
    `strict: true` tool `assign_enrichment` returning per event:
    `tags` (array), `entities`, `gist` (≤ 140 chars, factual, no hype),
    `confidence` (0–1). Match responses to events by index, not title.
  - Model: `EXTRACT_MODEL`. Single forced `tool_choice` is fine here (it's a
    fill-in call, like `_generate_pitches`), wrapped in `with_retry`.
  - **Validate output:** drop any tag not in `ALL_TAGS` (log it as a
    proposal); an event with no surviving tags keeps `tags=[]` and its
    existing top-level `category`; on total call failure, all events pass
    through untagged. Enrichment must never block or shrink the run.
- Wire into `run_research_job` after validation, before the ranking call and
  inserts, so tags exist for ranking and storage. Add tagging counts to
  `log_job_run` output (`tagged`, `tag_coverage`).
- Backfill: a small standalone script `scripts/backfill_tags.py` that loads
  stored future events with empty tags and runs them through
  `enrich_events` in batches. Run manually, not scheduled.

### 2.4 Tests

- Tag validation: invalid tags dropped and logged, empty-tag fallback,
  call-failure passthrough (mock the Anthropic client — no live calls in the
  default suite, matching the repo's existing pattern).
- Index-based response matching survives a shuffled/partial model response.
- `run_research_job` inserts events with tags populated end-to-end against
  `FakeRepository` with a stubbed enrichment call.

---

## Phase 3 — Declarative taste profile

**Goal:** user intent becomes a stored, editable contract; matching = filters
and score bumps against Phase 2 tags, plus one batched exemplar call; every
ballot line can say why it matched.

### 3.1 Schema

```sql
create table if not exists taste_profiles (
  id uuid primary key default gen_random_uuid(),
  person text unique not null,             -- 'aaron' | 'tay'
  hard_excludes text[] default '{}',       -- taxonomy nodes
  include_tags text[] default '{}',        -- taxonomy nodes
  include_entities text[] default '{}',    -- normalized names
  exemplars text[] default '{}',           -- max 5, each <= 200 chars (enforce in app)
  updated_at timestamptz default now()
);
```

Defaults: both people get an empty profile row (created lazily). Empty
profile = today's behavior, so this ships dark.

### 3.2 Matching (new module `app/research/matching.py`)

Applied in `run_research_job` between validation and the 12-cap:

1. **Hard excludes** — drop a candidate if any of its tags (or its top-level
   category mapped onto the taxonomy) is in the **union** of both people's
   `hard_excludes`. Log drops with the rule that fired.
2. **Score** — per candidate, a plain numeric score: +2 per matched
   `include_tags` entry (either person), +3 per matched entity, plus the
   learned layer: extend `preferences.build_preference_summary` with a
   counting sibling `build_preference_weights(repo)` returning
   {tag: net_yes_count, venue: net_yes_count, entity: net_yes_count} from
   resolved ballots (now counting tags/entities, not just category/venue) —
   worth +1 per positive net count, capped so learned weight can never exceed
   a single declared include. Learned weights never drop a candidate.
3. **Exemplars** — if either profile has exemplars and >12 candidates remain:
   one batched call (`EXTRACT_MODEL`, forced tool, strict schema): input =
   exemplar texts + per-candidate title/gist/tags; output = per-candidate
   0–10 relevance per exemplar-owner. Add to the score. On failure, skip —
   scores stand without it.
4. Rank by score (tie-break: sooner `start_at`), take 12. This **replaces**
   `rank_and_select` when any profile is non-empty; keep `rank_and_select` as
   the fallback when both profiles are empty.
5. **Match reasons** — attach `match_reasons: list[str]` to each chosen event
   (e.g. `declared:music/folk`, `entity:Watchhouse`, `learned:venue-4-yes`,
   `exemplar:1`) and thread the first reason into the SMS ballot line via
   `app/sms/formatting.py` as a short clause (e.g. "— folk, artist you
   follow"). Respect the existing 1600-char trim logic.

### 3.3 Profile editing surface

Minimal for testing — no auth beyond obscurity, matching the app's current
posture:

- `GET /profile/{person}` → JSON of the profile.
- `POST /profile/{person}` → replace profile fields (validate: tags must be
  in `ALL_TAGS`, ≤5 exemplars, ≤200 chars each; unknown person → 404).
- A static `profile.html` page (same style as the repo's existing static
  pages) that fetches and posts that JSON — chips for excludes/includes,
  textareas for exemplars. Keep it dependency-free vanilla JS.

### 3.4 Tests

- **Invariant test (most important):** an event tagged with a hard-excluded
  node never reaches ballots, even with maximal learned weight and a stubbed
  exemplar call scoring it 10.
- Empty profiles → identical selection to today (regression guard).
- Score composition, learned-weight cap, exemplar-failure passthrough.
- Profile endpoint validation (bad tag, 6th exemplar, unknown person).
- Match reason appears in the formatted SMS line and survives trimming.

---

## Definition of done, per phase

Each phase is a separate commit series, each leaving the suite green:

1. `pytest` green with the existing tests untouched (except mechanical model
   changes) plus the new ones; `sql/schema.sql` applies cleanly to fresh
   Postgres 16.
2. `DRY_RUN=true` end-to-end run works with the phase's feature on and with
   its tables empty (dark-launch safe).
3. No new required env vars; no new external services. All new LLM calls go
   through `with_retry`, are batched (never per-event), and degrade to
   passthrough on failure.
4. Success metrics wired into `log_job_run`: Phase 1 — sources by status per
   run; Phase 2 — `tag_coverage`; Phase 3 — `hit_rate` context (chosen events
   with ≥1 yes) computable from existing ballot data.

Open product decisions D1–D6 in the design doc are resolved to their
recommendations for this build (closed taxonomy in code, web profile page,
5×200-char exemplars, per-person profiles with union-exclude/union-include
merging, no new alerting beyond logs, backfill script yes).

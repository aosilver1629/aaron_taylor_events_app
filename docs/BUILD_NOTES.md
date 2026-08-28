# Build notes

This is a from-scratch build against `sfeventsworkflowspec.md`, done end-to-end
without stopping for UX/logic sign-off, per instructions. Everything the spec
left open got a documented, reasonable default instead of a question. This
file is that documentation, plus an honest accounting of what could and
couldn't be validated inside this build environment, and what to do before
flipping it live.

## What's implemented

All five build-order steps from the spec, plus the scheduler and Railway
deploy config:

1. **Schema + Supabase client + config** — `sql/schema.sql`, `app/config.py`,
   `app/db.py` (a `Repository` wrapping the `supabase-py` client — job code
   never touches the raw client).
2. **Calendar write path** — `app/calendar_client/google_calendar.py`,
   runs every 15 minutes, dedups via `extendedProperties.private.event_key`.
3. **SMS send path + inbound webhook** — `app/sms/provider.py`
   (`SMSProvider` interface, `MockSMSProvider`/`TwilioSMSProvider`),
   `app/main.py` (`POST /sms`, `GET /health`), Twilio signature verification.
4. **Reply parsing + ballot resolution** — `app/sms/reply_parser.py` (Claude
   Haiku call), `app/sms/webhook.py` (resolution logic),
   `tests/fixtures/replies.yaml` (33 cases).
5. **Research job** — `app/jobs/research_job.py` +
   `app/research/*` (Ticketmaster, Bandsintown, Claude research call with
   web search, validation, ranking).
6. **Scheduler** — `app/scheduler.py` (APScheduler, Pacific cron), started
   from the FastAPI lifespan in `app/main.py` so one Railway service runs
   both the API and the jobs.
7. **Deploy config** — `Procfile`, `railway.json`, `.python-version` (3.12).

## Decisions made without asking (and why)

- **DRY_RUN scope.** Read literally, "nothing is sent or written" could mean
  no DB writes either — but that would make DRY_RUN unable to exercise the
  pipeline at all (Job 2 needs events in the DB to send ballots for). I
  interpreted "sent or written" as *external* side effects: DRY_RUN forces
  the Twilio send and the Google Calendar insert to become log-only, but
  `events`/`ballots`/`sms_log` writes to your own Supabase project still
  happen normally. That's what makes DRY_RUN useful for testing the full
  loop. It's also a belt-and-suspenders switch: `SMS_PROVIDER=twilio` +
  `DRY_RUN=true` still forces `MockSMSProvider` (see
  `app/sms/provider.py::get_sms_provider`) — DRY_RUN always wins.
- **STOP/START/HELP.** Not in the spec, but `terms.html`/`privacy.html`
  (already in the repo, presumably written for A2P 10DLC carrier
  registration) promise this behavior. Twilio's own "Advanced Opt-Out"
  normally intercepts these before your webhook even sees them, but I added
  a defensive check in `app/sms/webhook.py` so the reply parser never has to
  see "STOP" as if it were a ballot vote, in case that feature is ever off.
- **Unknown senders.** Any inbound number that isn't Aaron's or Tay's is
  rejected before it's logged or replied to — replying to a wrong number or
  a scanner just confirms the line is live.
- **Model choice.** Reply parser uses `claude-haiku-4-5` (spec calls it "a
  small Claude call"). The research call and the >12-candidate ranking call
  use `claude-opus-5` (needs web search + judgment). Model IDs and API
  shapes (the `web_search_20260209` server tool, `strict: true` tool
  schemas) were checked against current Anthropic API docs.
- **Research call shape.** You can't force `tool_choice` onto a custom
  output tool *and* expect Claude to use web search first — forcing
  `tool_choice` makes that tool its first and only action. So the research
  call is: turn 1 with `tool_choice: auto` (search happens, then it's
  instructed to call `submit_events`); only if that fails is there a turn 2
  with `tool_choice` forced onto `submit_events` (safe by then, since the
  search results are already in the conversation). This *is* "reject and
  retry once on malformed output, don't regex-repair" — just implemented as
  two API turns of one logical call, not two separate calls.
- **Ranking call.** Only fires when validated survivors exceed 12 — a
  separate, forced-tool_choice call (no search needed) picks the best 12
  given the preference summary. If it under-returns, the remainder is
  topped up from the original validated list in order, rather than
  short-changing the week.
- **event_key.** `sha256(normalize(title)|pacific_date|venue)`. Uses the
  Pacific calendar date rather than the exact timestamp, so the same event
  reported with slightly different times by two sources still de-dupes.
- **Reply-parser context.** The numbered list handed to Claude for parsing
  uses the same formatted line that was actually texted (`app/sms/formatting.py`),
  not just the bare event title — otherwise "the free one" or "the market
  one" style replies would have nothing to match against.
- **SMS length trimming.** If the ballot message would exceed 1600 chars,
  events are trimmed off the end of the (already-ranked) list until it
  fits. Trimmed events get no ballot row that week — they'd need to survive
  into a future research run to be voted on. The spec says "trim the list,
  do not send two messages" but doesn't say what happens to the trimmed
  events; this is the simplest reading. At 12 events/week with reasonably
  short titles this should rarely if ever trigger.
- **Job scheduling.** Job 1 (research) and Job 2 (ballots) run as one
  scheduled firing at Sunday 08:00 Pacific, since the spec frames Job 2 as
  "after research inserts events" rather than giving it an independent
  schedule. Job 3 (calendar) is its own 15-minute cron; ballot expiry is
  its own hourly cron. All via `apscheduler`, timezone-aware.
- **Two extra env vars, kept out of `.env.example` on purpose** (the spec
  says the file should have "every key name" from its list, so I kept that
  list exact):
  - `BANDSINTOWN_ARTISTS` — comma-separated favorite-artist list. Spec says
    "empty list is fine at v1"; this defaults to empty and the Bandsintown
    fetch no-ops without it.
  - `ENABLE_SCHEDULER` — defaults to `true`. Set to `false` to boot the API
    without the scheduler thread trying to fire jobs against Supabase
    (useful for `/health`-only deploys or local testing without a DB).

## What was validated in this environment

This sandbox has no Anthropic API key, no Twilio/Google/Ticketmaster/
Bandsintown credentials, and no live Supabase project — network egress
outside the agent proxy is also restricted. Here's what that leaves, and
what it doesn't:

**Validated:**
- `sql/schema.sql` applied cleanly to a real local Postgres 16 instance
  (extension, tables, indexes all succeed).
- `app/db.py`'s Supabase query-builder calls were checked offline against
  the installed `supabase-py`/`postgrest-py` client — confirmed they
  produce correct PostgREST query strings (`select=`, `eq.`, `in.(...)`,
  `is.null`, embedded-resource joins like
  `events(category,venue)`, insert/update bodies) without needing a live
  server.
- 34 automated tests pass (`pytest tests/`), covering: ballot resolution
  (default-to-no, most-recent-batch-only, STOP/HELP/unknown-sender
  handling), research job orchestration (validation, dedup, the 12-event
  cap, the ranking-call path), ballot send (batching, numbering, SMS-length
  trimming), hourly expiry, calendar job (both-yes selection, dedup body
  construction, reminder/extendedProperties), and the FastAPI app itself
  (`/health`, `/sms` happy path, signature rejection, unknown-sender
  rejection) via `TestClient`. All of this runs against an in-memory
  `FakeRepository` (`tests/fake_repo.py`) that mirrors the schema's actual
  constraints (unique batch/person/list_number, the both-yes join logic).
- The app boots for real under `uvicorn`; `/health` returns 200 with zero
  external dependencies (matches `railway.json`'s healthcheck); `/sms`
  correctly fails loudly with a clear error when Supabase isn't configured,
  rather than silently no-opping.

**Not validated — needs real credentials before go-live:**
- The reply parser against real Claude calls. `tests/test_reply_parser.py`
  runs all 33 fixtures live and is what the spec means by "must pass these
  before it goes near a live number" — it's wired to auto-skip without
  `ANTHROPIC_API_KEY` (which is why you'll see 35 skipped tests, not 34
  passed + 35 more). **Run this for real, and get it green, before ever
  setting `SMS_PROVIDER=twilio`.**
- The research call (web search + structured output) against a live
  Anthropic key — same reason.
- Twilio: real SMS send, inbound webhook against a real number, signature
  verification against a real request. `MockSMSProvider` covers the send
  *path*, not Twilio's actual wire behavior.
- Google Calendar: real writes need a real service account with edit
  access to the target calendar.
- Ticketmaster/Bandsintown: real pulls need real API keys. Both fetchers
  degrade to `[]` without one, so nothing breaks if left blank.
- A live Supabase project — everything above about `db.py` is static/
  offline validation, not a live round-trip.

## Go-live checklist

1. Create a Supabase project, run `sql/schema.sql` against it, set
   `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`.
2. Set `ANTHROPIC_API_KEY`, then run:
   ```
   pytest tests/test_reply_parser.py -q
   ```
   This must be 100% green — it's the spec's explicit gate before the
   parser goes near a live number.
3. Google Calendar: create a service account, share the target calendar
   with its email (Editor access), set `GOOGLE_SERVICE_ACCOUNT_JSON` (the
   full JSON key as one string) and `CALENDAR_ID`. Test with `DRY_RUN=true`
   first (payloads log to stdout), then `DRY_RUN=false` against a
   throwaway calendar, per the spec's build order.
4. Twilio: register the number for A2P 10DLC (`privacy.html`/`terms.html`
   already exist in this repo for that), set `TWILIO_ACCOUNT_SID` /
   `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` / `AARON_PHONE` /
   `TAY_PHONE`, and point the number's inbound webhook at
   `https://<your-railway-domain>/sms`. Leave `SMS_PROVIDER=mock` until
   step 2 is green.
5. Optional: `TICKETMASTER_API_KEY`, `BANDSINTOWN_APP_ID` (and
   `BANDSINTOWN_ARTISTS` if you want specific artists tracked) — safe to
   leave blank.
6. Deploy to Railway (`Procfile` / `railway.json` are ready), confirm
   `/health`, then flip `SMS_PROVIDER=twilio` and finally `DRY_RUN=false`
   — last, deliberately, once everything above is green.

## Running locally

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in what you have; blanks are fine for most keys
pytest                  # 34 pass, 35 skip (live-Claude fixtures) without ANTHROPIC_API_KEY
ENABLE_SCHEDULER=false uvicorn app.main:app --reload   # /health works with no DB configured
```

"""FastAPI app: the one webhook endpoint (POST /sms) plus /health.

Scheduled jobs (research, ballot send, expiry, calendar write) are wired up
in app.scheduler and started here via the app lifespan, so `uvicorn
app.main:app` runs the API and the scheduler in one process.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import get_settings
from app.db import Repository
from app.jobs.ballot_job import run_ballot_send_job
from app.jobs.research_job import get_last_run, run_research_job
from app.logging_config import configure_logging
from app.models import EventIn, PEOPLE
from app.research.deterministic_search import KNOWN_PARSER_IDS
from app.research.keys import compute_event_key
from app.research.profile_parser import parse_preference_text
from app.research.source_registry import get_source_health
from app.research.taxonomy import is_valid_tag
from app.sms.provider import get_sms_provider
from app.sms.webhook import handle_inbound_sms, verify_twilio_signature
from app.utils.time import parse_iso_datetime

configure_logging()
logger = logging.getLogger("main")

# Not part of the spec's env key list — an internal escape hatch so the app
# can be booted (e.g. for /health checks, or this build's own validation)
# without a scheduler thread trying to fire jobs against an unconfigured
# Supabase project. Defaults on; set ENABLE_SCHEDULER=false to disable.
ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "true").lower() != "false"


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None
    if ENABLE_SCHEDULER:
        from app.scheduler import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
        logger.info("scheduler_started")
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="SF Events Voting Workflow", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """No landing page of its own yet — sends a browser straight to the
    admin app's first tab. No auth in front of this (or /admin) yet; that's
    a deliberate, known gap, not an oversight, and comes later."""
    return RedirectResponse(url="/admin")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/endpoints", response_class=HTMLResponse)
def endpoints_doc() -> HTMLResponse:
    """Serves endpoints.html — the living route-reference doc for this
    app. Kept current by hand alongside the routes themselves; no DB
    access, just reads the file off disk (same pattern as
    /profile-editor/{person} serving profile.html)."""
    html_path = Path(__file__).resolve().parent.parent / "endpoints.html"
    return HTMLResponse(content=html_path.read_text())


@app.get("/admin", response_class=HTMLResponse)
def admin_ui() -> HTMLResponse:
    """Serves admin.html — a mobile-first admin app (research preview +
    approve, taste-profile editing, source health) that calls the JSON
    routes below entirely client-side. Not an "ops" route itself: same
    no-DB-access, read-the-file-off-disk pattern as /endpoints and
    /profile-editor/{person}. Exists on this same origin (rather than as
    a separately-hosted page) because the browser fetches it makes to
    /ops/research/run, /ops/events/approve, and /profile/{person} would
    otherwise be blocked by CORS — this app has no CORS middleware, by
    design, since every real client is same-origin."""
    html_path = Path(__file__).resolve().parent.parent / "admin.html"
    return HTMLResponse(content=html_path.read_text())


VALID_SOURCE_KINDS = {"venue", "comedy", "fairs", "food"}
# 0 (api) and 3 (browser) are reserved for fetch strategies no code path
# implements yet — see deterministic_search.run_deterministic_research_call,
# which only ever produces http/regex, http/llm, or search_fallback/llm.
# Storing 0/3 is harmless (preferred_tier is never read by the pipeline),
# so validation allows the full range rather than pretending it's 1/2/4 only.
VALID_SOURCE_TIERS = {0, 1, 2, 3, 4}


def _source_validation_error(body: dict) -> str | None:
    """Shared by create and edit — returns an error message, or None if
    every field present in body is valid. Only validates fields that are
    actually present, since edit is a partial update."""
    if "kind" in body and body["kind"] not in VALID_SOURCE_KINDS:
        return f"kind must be one of {sorted(VALID_SOURCE_KINDS)}"
    if "preferred_tier" in body:
        tier = body["preferred_tier"]
        if not isinstance(tier, int) or tier not in VALID_SOURCE_TIERS:
            return f"preferred_tier must be one of {sorted(VALID_SOURCE_TIERS)}"
    if "parser_id" in body and body["parser_id"] is not None:
        if body["parser_id"] not in KNOWN_PARSER_IDS:
            return (
                f"parser_id must be one of {sorted(KNOWN_PARSER_IDS)} or null — "
                "a new parser is a code change, not something this route can create"
            )
    return None


@app.get("/ops/sources")
def ops_sources(city: str = "sf") -> dict:
    """Curation layer, Phase 1. Every source for `city`, enabled or not (a
    paused source stays visible/manageable rather than disappearing): label,
    url/kind/preferred_tier/parser_id/extraction_rules/enabled (the editable
    fields), which fetch/extract methods the latest run actually used,
    health status + reason, and the last 8 runs' candidate counts."""
    repo = Repository()
    return {"sources": get_source_health(repo, city=city)}


@app.post("/ops/sources")
async def ops_sources_create(request: Request) -> dict:
    """Add a new research source. Body: {label, url, kind, preferred_tier,
    parser_id?, extraction_rules?, enabled?} — city defaults to "sf" (the
    only city this app currently runs). Live on the very next research run
    without any other change, since load_sources() reads this table."""
    body = await request.json()
    label = (body.get("label") or "").strip()
    url = (body.get("url") or "").strip()
    if not label or not url:
        return JSONResponse(status_code=400, content={"error": "label and url are required"})
    if "kind" not in body or "preferred_tier" not in body:
        return JSONResponse(status_code=400, content={"error": "kind and preferred_tier are required"})

    error = _source_validation_error(body)
    if error:
        return JSONResponse(status_code=400, content={"error": error})

    repo = Repository()
    existing_labels = {s["label"] for s in repo.get_all_sources(body.get("city", "sf"))}
    if label in existing_labels:
        return JSONResponse(status_code=400, content={"error": f"a source labeled {label!r} already exists"})

    created = repo.insert_source(
        label=label,
        city=body.get("city", "sf"),
        url=url,
        kind=body["kind"],
        preferred_tier=body["preferred_tier"],
        parser_id=body.get("parser_id"),
        extraction_rules=body.get("extraction_rules"),
        enabled=body.get("enabled", True),
    )
    return created


@app.patch("/ops/sources/{source_id}")
async def ops_sources_update(source_id: str, request: Request) -> dict:
    """Edit a source — partial update, same fields as POST /ops/sources, all
    optional. This is also the enable/disable lever: {"enabled": false}
    pauses it (load_sources() excludes it from the very next research run),
    {"enabled": true} resumes it."""
    body = await request.json()
    error = _source_validation_error(body)
    if error:
        return JSONResponse(status_code=400, content={"error": error})

    fields = {
        k: v for k, v in body.items()
        if k in {"label", "url", "kind", "preferred_tier", "parser_id", "extraction_rules", "enabled"}
    }
    if not fields:
        return JSONResponse(status_code=400, content={"error": "no editable fields in body"})

    try:
        source_uuid = UUID(source_id)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": f"unknown source: {source_id}"})

    repo = Repository()
    updated = repo.update_source(source_uuid, **fields)
    if updated is None:
        return JSONResponse(status_code=404, content={"error": f"unknown source: {source_id}"})
    return updated


@app.post("/ops/research/run")
def ops_research_run() -> dict:
    """Manual trigger for Job 1 (research), preview-only. Runs the exact
    same pipeline the scheduler calls every other morning — real
    Ticketmaster/Bandsintown/Claude calls, real per-URL validation, real
    taste-profile matching — but never writes to `events` and never sends
    a ballot; see run_research_job's `persist` docstring for why a preview
    must not be persisted. Inspect the returned/last-run `inserted_events`
    and, when they look right, POST that same list to
    /ops/events/approve to actually mint them and send the real ballot.
    """
    repo = Repository()
    settings = get_settings()
    run_research_job(repo, settings, triggered_by="manual", persist=False)
    return get_last_run() or {"status": "no_run_yet_this_process"}


@app.post("/ops/events/approve")
async def ops_events_approve(request: Request) -> dict:
    """Manual 'approve' step: takes a batch of previewed candidate events —
    the same shape POST /ops/research/run returns, whole or trimmed down to
    the ones actually wanted — mints them into the real `events` table, and
    immediately sends the real SMS ballot for exactly that batch. This is
    the only manual route that writes to `events`; testing-only for now,
    ahead of the research -> preview -> approve -> voting UI.

    Body: {"events": [{title, start_at, ...}, ...]} (same fields as
    models.EventIn — title and start_at required, everything else optional).
    """
    body = await request.json()
    raw_events = body.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        return JSONResponse(status_code=400, content={"error": "events must be a non-empty list"})

    to_insert: list[EventIn] = []
    for raw in raw_events:
        title = (raw.get("title") or "").strip()
        start_at = parse_iso_datetime(raw.get("start_at"))
        if not title or start_at is None:
            return JSONResponse(
                status_code=400,
                content={"error": f"invalid event (bad title/start_at): {raw.get('title')!r}"},
            )
        venue = raw.get("venue")
        event_key = raw.get("event_key") or compute_event_key(title, start_at, venue)
        to_insert.append(
            EventIn(
                event_key=event_key,
                title=title,
                start_at=start_at,
                end_at=parse_iso_datetime(raw.get("end_at")),
                venue=venue,
                neighborhood=raw.get("neighborhood"),
                category=raw.get("category"),
                price_range=raw.get("price_range"),
                url=raw.get("url"),
                pitch=raw.get("pitch"),
                source=raw.get("source"),
                tags=raw.get("tags") or [],
                entities=raw.get("entities") or [],
                gist=raw.get("gist"),
                tag_confidence=raw.get("tag_confidence"),
                match_reasons=raw.get("match_reasons") or [],
            )
        )

    repo = Repository()
    settings = get_settings()

    inserted = []
    skipped_duplicates = []
    for event_in in to_insert:
        # Same event_key dedup guard as the real research pipeline — approving
        # the same preview twice (or a preview that's since gone stale
        # against a real run) must not create a second row for one event.
        if repo.event_key_exists(event_in.event_key):
            skipped_duplicates.append(event_in.title)
            continue
        inserted_event = repo.insert_event(event_in)
        inserted_event.match_reasons = event_in.match_reasons
        inserted.append(inserted_event)

    ballot_result = {"events": 0, "people": 0}
    if inserted:
        ballot_result = run_ballot_send_job(repo, inserted, settings)

    return {
        "inserted": len(inserted),
        "inserted_titles": [e.title for e in inserted],
        "skipped_duplicates": skipped_duplicates,
        "ballot_sent": bool(inserted),
        "ballot_people_notified": ballot_result.get("people", 0),
    }


@app.get("/ops/research/last-run")
def ops_research_last_run() -> dict:
    """Status + result of the most recent research run in this process
    (cron-triggered or manual via POST /ops/research/run). In-memory only
    — resets on redeploy/restart; see run_research_job's module docstring
    for why that tradeoff is fine here."""
    return get_last_run() or {"status": "no_run_yet_this_process"}


MAX_EXEMPLARS = 5
MAX_EXEMPLAR_LEN = 200


def _unknown_person(person: str) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": f"unknown person: {person}"})


@app.get("/profile/{person}")
def get_profile(person: str):
    """Curation layer, Phase 3."""
    if person not in PEOPLE:
        return _unknown_person(person)
    repo = Repository()
    return repo.get_taste_profile(person)


@app.post("/profile/{person}")
async def post_profile(person: str, request: Request):
    """Curation layer, Phase 3. Body: {hard_excludes, include_tags,
    include_entities, exemplars}, all optional lists — omitted fields keep
    their current stored value (this is a partial update, not a replace-all,
    since profile.html only ever submits the fields its form actually has)."""
    if person not in PEOPLE:
        return _unknown_person(person)

    body = await request.json()
    fields = {}

    for key in ("hard_excludes", "include_tags"):
        if key in body:
            tags = body[key]
            invalid = [t for t in tags if not is_valid_tag(t)]
            if invalid:
                return JSONResponse(status_code=400, content={"error": f"invalid tags in {key}: {invalid}"})
            fields[key] = tags

    if "include_entities" in body:
        entities = body["include_entities"]
        if not isinstance(entities, list) or not all(isinstance(e, str) for e in entities):
            return JSONResponse(status_code=400, content={"error": "include_entities must be a list of strings"})
        fields["include_entities"] = entities

    if "exemplars" in body:
        exemplars = body["exemplars"]
        if not isinstance(exemplars, list) or not all(isinstance(e, str) for e in exemplars):
            return JSONResponse(status_code=400, content={"error": "exemplars must be a list of strings"})
        if len(exemplars) > MAX_EXEMPLARS:
            return JSONResponse(status_code=400, content={"error": f"at most {MAX_EXEMPLARS} exemplars"})
        too_long = [e for e in exemplars if len(e) > MAX_EXEMPLAR_LEN]
        if too_long:
            return JSONResponse(
                status_code=400, content={"error": f"exemplars must be at most {MAX_EXEMPLAR_LEN} chars"}
            )
        fields["exemplars"] = exemplars

    repo = Repository()
    return repo.update_taste_profile(person, **fields)


@app.post("/profile/{person}/parse")
async def parse_profile_text(person: str, request: Request):
    """Curation layer — natural-language profile assist. Returns a draft
    {hard_excludes, include_tags, include_entities, exemplars} for
    profile.html to fold into its in-page state; never persists on its
    own — only the user's own Save (POST /profile/{person}) writes
    anything, so a misparsed sentence can be reviewed and deselected
    before it becomes a real constraint."""
    if person not in PEOPLE:
        return _unknown_person(person)
    body = await request.json()
    text = body.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(status_code=400, content={"error": "text is required"})
    return parse_preference_text(text)


@app.get("/profile-editor/{person}", response_class=HTMLResponse)
def profile_editor(person: str):
    """Serves profile.html — the JS on the page itself reads `person` back
    out of the URL path and talks to the JSON endpoints above on this same
    origin, so this exists mainly to give the file somewhere reachable to
    load from (the repo's other static *.html pages are marketing pages
    with no backend behind them, unlike this one)."""
    if person not in PEOPLE:
        return _unknown_person(person)
    html_path = Path(__file__).resolve().parent.parent / "profile.html"
    return HTMLResponse(content=html_path.read_text())


def _request_url(request: Request) -> str:
    """Reconstruct the externally-visible URL for Twilio signature checks.
    Railway terminates TLS at its edge proxy, so trust X-Forwarded-Proto
    over request.url.scheme (which would otherwise read as http).
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}{request.url.path}"


@app.post("/sms")
async def sms_webhook(request: Request) -> Response:
    settings = get_settings()
    form = await request.form()
    params = {k: v for k, v in form.items()}

    signature = request.headers.get("x-twilio-signature")
    if settings.sms_provider == "twilio" and not settings.dry_run:
        url = _request_url(request)
        if not verify_twilio_signature(settings, url, params, signature):
            logger.warning("sms_signature_rejected", extra={"job_fields": {"url": url}})
            return Response(status_code=403, content="invalid signature")

    from_number = params.get("From", "")
    body = params.get("Body", "")

    repo = Repository()
    sms_provider = get_sms_provider()
    handle_inbound_sms(repo, sms_provider, settings, from_number, body)

    # We already sent the reply via the SMS provider above, so return an
    # empty TwiML document rather than letting Twilio auto-reply too.
    return Response(content="<Response></Response>", media_type="application/xml")

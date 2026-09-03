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

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_settings
from app.db import Repository
from app.logging_config import configure_logging
from app.models import PEOPLE
from app.research.source_registry import get_source_health
from app.research.taxonomy import is_valid_tag
from app.sms.provider import get_sms_provider
from app.sms.webhook import handle_inbound_sms, verify_twilio_signature

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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ops/sources")
def ops_sources(city: str = "sf") -> dict:
    """Curation layer, Phase 1 — no UI, this JSON is the testable surface.
    Per source: label, which fetch/extract methods the latest run actually
    used, health status + reason, and the last 8 runs' candidate counts."""
    repo = Repository()
    return {"sources": get_source_health(repo, city=city)}


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

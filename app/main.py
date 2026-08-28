"""FastAPI app: the one webhook endpoint (POST /sms) plus /health.

Scheduled jobs (research, ballot send, expiry, calendar write) are wired up
in app.scheduler and started here via the app lifespan, so `uvicorn
app.main:app` runs the API and the scheduler in one process.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.config import get_settings
from app.db import Repository
from app.logging_config import configure_logging
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

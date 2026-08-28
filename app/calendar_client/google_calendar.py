"""Google Calendar v3 write path (Job 3).

Runs every 15 minutes: selects events where both people voted yes and no
calendar_event_id is set yet, writes each to the shared calendar, and
stores the returned event id. Dedup is enforced two ways: the DB's
`calendar_event_id is null` filter (never re-process a row we already
wrote), and — because that alone doesn't protect against a crash between
"created on Google" and "saved the id back to our row" — a live lookup on
the calendar by the private `event_key` extended property before every
insert.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import Settings, get_settings
from app.db import Repository
from app.logging_config import log_job_run
from app.models import Event
from app.utils.retry import with_retry
from app.utils.time import format_event_time, to_pacific

logger = logging.getLogger("calendar_job")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
REMINDER_MINUTES_BEFORE = 1440


def get_calendar_service():
    settings = get_settings()
    if not settings.google_service_account_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")
    info = json.loads(settings.google_service_account_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _build_event_body(event: Event) -> dict:
    description_parts = []
    if event.pitch:
        description_parts.append(event.pitch)
    if event.price_range:
        description_parts.append(f"Price: {event.price_range}")
    if event.url:
        description_parts.append(event.url)
    description = "\n\n".join(description_parts)

    start = to_pacific(event.start_at)
    body = {
        "summary": event.title,
        "location": event.venue or "",
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": REMINDER_MINUTES_BEFORE},
            ],
        },
        "extendedProperties": {"private": {"event_key": event.event_key}},
    }
    if event.end_at:
        body["end"] = {"dateTime": to_pacific(event.end_at).isoformat()}
    else:
        # Calendar requires an end; default to a 2-hour block.
        body["end"] = {"dateTime": (start + timedelta(hours=2)).isoformat()}
    return body


def find_existing_event_id(service, calendar_id: str, event_key: str) -> str | None:
    result = (
        service.events()
        .list(
            calendarId=calendar_id,
            privateExtendedProperty=f"event_key={event_key}",
            maxResults=1,
            showDeleted=False,
        )
        .execute()
    )
    items = result.get("items", [])
    return items[0]["id"] if items else None


def write_event_to_calendar(service, calendar_id: str, event: Event) -> str:
    """Create the event, guarding against duplicates via event_key. Returns
    the calendar event id (existing or newly created)."""
    existing_id = find_existing_event_id(service, calendar_id, event.event_key)
    if existing_id:
        logger.info(
            "calendar_duplicate_skipped",
            extra={"job_fields": {"event_key": event.event_key, "calendar_event_id": existing_id}},
        )
        return existing_id

    body = _build_event_body(event)
    created = service.events().insert(calendarId=calendar_id, body=body).execute()
    return created["id"]


def run_calendar_job(repo: Repository, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    events = repo.get_events_awaiting_calendar_write()

    if not events:
        log_job_run(logger, "calendar_write", written=0, skipped=0)
        return {"written": 0, "skipped": 0}

    if settings.dry_run:
        for event in events:
            logger.info(
                "dry_run_calendar_write",
                extra={
                    "job_fields": {
                        "title": event.title,
                        "when": format_event_time(event.start_at),
                        "venue": event.venue,
                        "event_key": event.event_key,
                    }
                },
            )
        log_job_run(logger, "calendar_write", written=0, skipped=len(events), dry_run=True)
        return {"written": 0, "skipped": len(events), "dry_run": True}

    service = get_calendar_service()
    written = 0
    failed = 0
    for event in events:
        calendar_event_id = with_retry(
            lambda e=event: write_event_to_calendar(service, settings.calendar_id, e),
            what=f"calendar_write:{event.event_key}",
        )
        if calendar_event_id:
            repo.set_calendar_event_id(event.id, calendar_event_id)
            written += 1
        else:
            failed += 1

    log_job_run(logger, "calendar_write", written=written, failed=failed)
    return {"written": written, "failed": failed}

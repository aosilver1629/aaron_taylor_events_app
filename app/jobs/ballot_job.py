"""Job 2 — Ballots. Sends one SMS per person listing the events research
just inserted, numbered, and creates the matching ballot rows. Runs
immediately after Job 1 (same weekly firing — see app.scheduler), not on
its own schedule.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from app.config import Settings, get_settings
from app.db import Repository
from app.logging_config import log_job_run
from app.models import Event, PEOPLE
from app.sms.formatting import ballot_line
from app.sms.provider import SMSProvider, get_sms_provider

logger = logging.getLogger("ballot_job")

HEADER = 'This week in SF — reply with the numbers you\'re in for (or "none"):'
MAX_SMS_LEN = 1600


def build_message(events: list[Event]) -> tuple[str, list[Event]]:
    """Trims from the end of the list until the message fits Twilio's
    single-segment-friendly budget. Per spec: never split into two sends.
    """
    included = list(events)
    while included:
        lines = [HEADER] + [ballot_line(i, e) for i, e in enumerate(included, start=1)]
        body = "\n".join(lines)
        if len(body) <= MAX_SMS_LEN:
            return body, included
        included = included[:-1]
    return HEADER, []


def run_ballot_send_job(
    repo: Repository,
    events: list[Event],
    settings: Settings | None = None,
    sms_provider: SMSProvider | None = None,
) -> dict:
    settings = settings or get_settings()
    sms_provider = sms_provider or get_sms_provider()

    if not events:
        log_job_run(logger, "ballot_send", events=0, people=0)
        return {"events": 0, "people": 0}

    body, included = build_message(events)
    if not included:
        logger.warning("ballot_send_empty_after_trim", extra={"job_fields": {"event_count": len(events)}})
        return {"events": 0, "people": 0}

    trimmed = len(events) - len(included)
    if trimmed:
        logger.warning(
            "ballot_send_trimmed_for_length",
            extra={"job_fields": {"trimmed": trimmed, "kept": len(included)}},
        )

    sent = 0
    for person in PEOPLE:
        to_number = settings.people.get(person)
        if not to_number:
            logger.error("ballot_send_missing_phone", extra={"job_fields": {"person": person}})
            continue

        batch_id = uuid4()
        entries = [(e.id, i) for i, e in enumerate(included, start=1)]
        repo.create_ballot_batch(person, batch_id, entries)

        # get_sms_provider() already resolves to MockSMSProvider whenever
        # DRY_RUN is true (see app.sms.provider), which logs the body and
        # records it in sms_log without ever hitting Twilio — so there's no
        # separate dry-run branch needed here.
        sms_provider.send(to_number, body)
        sent += 1

    log_job_run(logger, "ballot_send", events=len(included), people=sent, dry_run=settings.dry_run)
    return {"events": len(included), "people": sent}

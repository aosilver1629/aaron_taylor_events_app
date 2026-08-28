"""Inbound SMS webhook: POST /sms. Verifies the Twilio signature, rejects
any number outside the two-person allowlist, logs the raw body (sms_log is
append-only and exists precisely so a bad parse can be replayed), then
resolves it against that person's most recent ballot batch.
"""
from __future__ import annotations

import logging

from twilio.request_validator import RequestValidator

from app.config import Settings
from app.db import Repository
from app.models import Ballot
from app.sms.formatting import event_label
from app.sms.provider import SMSProvider
from app.sms.reply_parser import parse_reply

logger = logging.getLogger("sms_webhook")

OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
OPT_IN_KEYWORDS = {"start", "yes", "unstop"}
HELP_KEYWORDS = {"help", "info"}

OPT_OUT_REPLY = (
    "You've been unsubscribed from SF Activities and won't receive further "
    "messages. Reply START to opt back in."
)
OPT_IN_REPLY = "You're opted back in to SF Activities. Reply STOP anytime to opt out."
HELP_REPLY = (
    "SF Activities: weekly SF event suggestions by text. Reply STOP to "
    "unsubscribe, HELP for this message. Msg & data rates may apply. "
    "aostein1@gmail.com"
)


def verify_twilio_signature(
    settings: Settings, url: str, params: dict, signature: str | None
) -> bool:
    if not signature:
        return False
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.validate(url, params, signature)


def person_for_number(settings: Settings, number: str) -> str | None:
    normalized = (number or "").strip()
    for person, phone in settings.people.items():
        if phone and phone == normalized:
            return person
    return None


def _numbered_events(repo: Repository, ballots: list[Ballot]) -> tuple[list[tuple[int, str]], dict[int, str]]:
    numbered: list[tuple[int, str]] = []
    titles: dict[int, str] = {}
    for ballot in sorted(ballots, key=lambda b: b.list_number):
        event = repo.get_event(ballot.event_id)
        label = event_label(event) if event else f"item {ballot.list_number}"
        title = event.title if event else f"item {ballot.list_number}"
        numbered.append((ballot.list_number, label))
        titles[ballot.list_number] = title
    return numbered, titles


def resolve_reply(repo: Repository, person: str, raw_body: str) -> str:
    """Resolves `raw_body` against `person`'s most recent ballot batch and
    returns the confirmation text to send back. Never resolves against an
    older batch, per spec.
    """
    batch_id = repo.get_latest_batch_id(person)
    if batch_id is None:
        return "No event list has gone out yet — nothing to vote on."

    ballots = repo.get_ballots_for_batch(batch_id, person)
    open_ballots = [b for b in ballots if b.response is None]
    if not open_ballots:
        return "That list is already closed out — nothing open to vote on right now."

    numbered_events, titles = _numbered_events(repo, open_ballots)
    parsed = parse_reply(raw_body, numbered_events)
    yes_numbers = set(parsed.yes)

    yes_titles, no_titles = [], []
    for ballot in open_ballots:
        response = "yes" if ballot.list_number in yes_numbers else "no"
        repo.resolve_ballot(ballot.id, response)
        (yes_titles if response == "yes" else no_titles).append(titles[ballot.list_number])

    if yes_titles:
        return f"Got it — you're in for: {', '.join(yes_titles)}. Everything else logged as no."
    return "Got it — logged as no for everything on this list."


def handle_inbound_sms(
    repo: Repository,
    sms_provider: SMSProvider,
    settings: Settings,
    from_number: str,
    body: str,
) -> str:
    """Core logic, independent of FastAPI, so it's directly unit-testable.
    Returns the text sent back (or "" if nothing was sent, e.g. rejected
    sender).
    """
    person = person_for_number(settings, from_number)
    if person is None:
        logger.warning(
            "sms_rejected_unknown_sender",
            extra={"job_fields": {"from_number": from_number}},
        )
        # Do not log unknown-sender bodies to sms_log (person is unset/None
        # and it's not part of the two-person workflow) and never reply —
        # replying to spam/wrong numbers just confirms the number is live.
        return ""

    repo.log_sms(person, "in", body)

    stripped = body.strip().lower()
    if stripped in OPT_OUT_KEYWORDS:
        reply = OPT_OUT_REPLY
    elif stripped in OPT_IN_KEYWORDS:
        reply = OPT_IN_REPLY
    elif stripped in HELP_KEYWORDS:
        reply = HELP_REPLY
    else:
        reply = resolve_reply(repo, person, body)

    # sms_provider.send() logs the outbound message to sms_log itself
    # (MockSMSProvider and TwilioSMSProvider both do), so it isn't logged
    # again here.
    sms_provider.send(from_number, reply)
    return reply

"""SMSProvider abstraction. Job code never touches the Twilio SDK directly —
it calls `get_sms_provider().send(...)`. This is what lets the entire send
path be built and tested before A2P 10DLC carrier approval lands: flip
SMS_PROVIDER from "mock" to "twilio" and nothing else changes.
"""
from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from functools import lru_cache

from app.config import Settings, get_settings
from app.db import Repository
from app.utils.retry import with_retry

logger = logging.getLogger("sms")


class SMSProvider(ABC):
    @abstractmethod
    def send(self, to: str, body: str) -> str:
        """Send `body` to `to`. Returns the provider message SID."""
        raise NotImplementedError


class MockSMSProvider(SMSProvider):
    """Writes to sms_log with direction='out' and returns a fake SID.
    Used whenever SMS_PROVIDER=mock (the default) or DRY_RUN=true.
    """

    def __init__(self, repo: Repository | None = None, settings: Settings | None = None):
        self._repo = repo
        self._settings = settings or get_settings()

    def send(self, to: str, body: str) -> str:
        fake_sid = f"MOCK{uuid.uuid4().hex[:30].upper()}"
        logger.info(
            "mock_sms_send",
            extra={"job_fields": {"to": to, "body": body, "sid": fake_sid}},
        )
        if self._repo:
            person = _person_for_number(self._settings, to)
            self._repo.log_sms(person, "out", body)
        return fake_sid


class TwilioSMSProvider(SMSProvider):
    def __init__(self, settings: Settings, repo: Repository | None = None):
        from twilio.rest import Client

        self._settings = settings
        self._repo = repo
        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def send(self, to: str, body: str) -> str:
        def _do_send():
            message = self._client.messages.create(
                to=to, from_=self._settings.twilio_from_number, body=body
            )
            return message.sid

        sid = with_retry(_do_send, what=f"twilio_send:{to}", reraise=True)
        if self._repo:
            person = _person_for_number(self._settings, to)
            self._repo.log_sms(person, "out", body)
        return sid


def _person_for_number(settings: Settings, number: str) -> str | None:
    for person, phone in settings.people.items():
        if phone and phone == number:
            return person
    return None


@lru_cache
def get_sms_provider() -> SMSProvider:
    settings = get_settings()
    repo = None
    try:
        repo = Repository()
    except RuntimeError:
        # No Supabase configured (e.g. isolated unit test) — logging-only.
        repo = None

    if settings.sms_provider == "twilio" and not settings.dry_run:
        return TwilioSMSProvider(settings, repo)
    return MockSMSProvider(repo)

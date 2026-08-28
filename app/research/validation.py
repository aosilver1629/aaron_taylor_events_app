"""Per-row validation gate before an event is inserted (spec step 5): the
date parses and falls in the research window, the URL actually resolves,
the title is sane, and it isn't a duplicate already in the DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.db import Repository
from app.models import EventIn
from app.research.keys import compute_event_key
from app.utils.retry import with_retry

logger = logging.getLogger("event_validation")

MAX_TITLE_LEN = 120


@dataclass
class ValidationResult:
    ok: bool
    reason: str | None = None
    event: EventIn | None = None


def _parse_start_at(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _url_returns_2xx(url: str) -> bool:
    def _check():
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.head(url)
            if resp.status_code >= 400:
                # Some event sites don't support HEAD — fall back to GET.
                resp = client.get(url)
            return 200 <= resp.status_code < 300

    result = with_retry(_check, what=f"url_check:{url}")
    return bool(result)


def validate_candidate(
    repo: Repository,
    raw: dict,
    window_start: datetime,
    window_end: datetime,
    seen_keys: set[str],
) -> ValidationResult:
    title = (raw.get("title") or "").strip()
    if not title or len(title) > MAX_TITLE_LEN:
        return ValidationResult(False, f"invalid title: {title!r}")

    start_at = _parse_start_at(raw.get("start_at"))
    if start_at is None:
        return ValidationResult(False, "start_at does not parse")
    if not (window_start <= start_at <= window_end):
        return ValidationResult(False, "start_at outside research window")

    url = (raw.get("url") or "").strip() or None
    if url and not _url_returns_2xx(url):
        return ValidationResult(False, f"url did not return 2xx: {url}")

    venue = raw.get("venue")
    event_key = compute_event_key(title, start_at, venue)
    if event_key in seen_keys:
        return ValidationResult(False, "duplicate within this run")
    if repo.event_key_exists(event_key):
        return ValidationResult(False, "duplicate of existing event")

    end_at = _parse_start_at(raw.get("end_at"))

    event = EventIn(
        event_key=event_key,
        title=title,
        start_at=start_at,
        end_at=end_at,
        venue=venue,
        neighborhood=raw.get("neighborhood"),
        category=raw.get("category"),
        price_range=raw.get("price_range"),
        url=url,
        pitch=raw.get("pitch"),
        source=raw.get("source"),
    )
    return ValidationResult(True, event=event)

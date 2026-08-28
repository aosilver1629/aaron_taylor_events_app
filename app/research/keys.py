"""event_key = sha256(normalize(title)|date|venue), per spec — used both to
de-duplicate against events already in the DB and to catch the same event
surfacing from two different sources (Ticketmaster vs. the web-search pass)
in the same run.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from app.utils.time import to_pacific


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def compute_event_key(title: str, start_at: datetime, venue: str | None) -> str:
    date_part = to_pacific(start_at).date().isoformat()
    venue_part = (venue or "").strip().lower()
    raw = f"{normalize_title(title)}|{date_part}|{venue_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

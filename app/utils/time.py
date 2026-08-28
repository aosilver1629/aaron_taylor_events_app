"""Everything is stored in UTC; every user-facing render is Pacific."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
PACIFIC = ZoneInfo("America/Los_Angeles")


def to_pacific(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(PACIFIC)


def format_event_time(dt: datetime) -> str:
    """e.g. 'Sat 8/29 7pm', matching the SMS format in the spec."""
    local = to_pacific(dt)
    day = local.strftime("%a")
    date = f"{local.month}/{local.day}"
    hour12 = local.strftime("%I").lstrip("0") or "12"
    minute = local.minute
    ampm = local.strftime("%p").lower()
    time_str = f"{hour12}{ampm}" if minute == 0 else f"{hour12}:{minute:02d}{ampm}"
    return f"{day} {date} {time_str}"


def now_utc() -> datetime:
    return datetime.now(UTC)

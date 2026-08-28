"""Shared formatting for a single event's line in a ballot SMS. Used both
when the ballot goes out (Job 2) and when a reply comes back in, so the
reply parser sees the same text the person actually read — that's what
makes "the market one" or "the free one" resolvable.
"""
from __future__ import annotations

from app.models import Event
from app.utils.time import format_event_time


def event_label(event: Event) -> str:
    title_part = event.title
    if event.venue and event.venue.lower() not in event.title.lower():
        title_part = f"{event.title} at {event.venue}"
    price = event.price_range or "free"
    return f"{format_event_time(event.start_at)} — {title_part} ({price})"


def ballot_line(list_number: int, event: Event) -> str:
    return f"{list_number}. {event_label(event)}"

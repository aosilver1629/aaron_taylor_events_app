"""Shared formatting for a single event's line in a ballot SMS. Used both
when the ballot goes out (Job 2) and when a reply comes back in, so the
reply parser sees the same text the person actually read — that's what
makes "the market one" or "the free one" resolvable.
"""
from __future__ import annotations

from app.models import Event
from app.utils.time import format_event_time

# Titles/venues come from scraped pages or an LLM, so "smart" typography
# shows up unpredictably. A single non-GSM-7 character anywhere in an SMS
# forces the whole message into UCS-2 encoding (67 chars/segment instead of
# 153) — a message that looks well within the app's length budget can
# balloon to 2x the real segment count and get rejected outright by the
# carrier (seen live: a 909-char, 12-event ballot needed 14 segments and
# came back Twilio error 30019, "message exceeds carrier limit"). Normalize
# at the shared source so the outbound SMS and the reply parser's
# reconstructed numbered list stay identical, per the module docstring.
_GSM7_REPLACEMENTS = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "‘": "'",
    "’": "'",  # curly single quotes
    "“": '"',
    "”": '"',  # curly double quotes
    "…": "...",  # ellipsis
    " ": " ",  # non-breaking space
}


def gsm7_safe(text: str) -> str:
    for bad, good in _GSM7_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return "".join(c for c in text if ord(c) < 128)


def _humanize_match_reason(reason: str) -> str | None:
    """Curation layer, Phase 3 — turns a matching.py reason code (e.g.
    "declared:music/folk", "learned:venue-2-yes") into a short trailing
    clause for the ballot line. Unknown/malformed codes return None rather
    than raising — a missing "why" is better than a broken SMS."""
    kind, _, rest = reason.partition(":")
    if not rest:
        return None
    if kind == "declared":
        return f"you like {rest.rsplit('/', 1)[-1]}"
    if kind == "entity":
        return f"features {rest}"
    if kind == "learned":
        if rest.startswith("tag-"):
            tag = rest[len("tag-"):].rsplit("-", 2)[0]
            return f"similar to {tag.rsplit('/', 1)[-1]} you've liked"
        if rest.startswith("venue-"):
            return "at a venue you've liked"
        if rest.startswith("entity-"):
            name = rest[len("entity-"):].rsplit("-", 2)[0]
            return f"you've liked {name} before"
        return None
    if kind == "exemplar":
        return f"matches {rest}'s taste"
    return None


def event_label(event: Event) -> str:
    title_part = gsm7_safe(event.title)
    venue = gsm7_safe(event.venue) if event.venue else None
    if venue and venue.lower() not in title_part.lower():
        title_part = f"{title_part} at {venue}"
    price = gsm7_safe(event.price_range) if event.price_range else "free"
    label = f"{format_event_time(event.start_at)} - {title_part} ({price})"
    if event.match_reasons:
        why = _humanize_match_reason(event.match_reasons[0])
        if why:
            label = f"{label} - {gsm7_safe(why)}"
    return label


def ballot_line(list_number: int, event: Event) -> str:
    return f"{list_number}. {event_label(event)}"

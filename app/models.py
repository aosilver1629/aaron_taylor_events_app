"""Shared data shapes. Kept as plain dataclasses (not ORM models) since the
DB access layer talks to Supabase's PostgREST API in dict form — these exist
to give job code and tests a typed shape to pass around instead of raw dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

VALID_CATEGORIES = {
    "concert",
    "festival",
    "market",
    "food",
    "outdoor",
    "art",
    "other",
}

PEOPLE = ("aaron", "tay")


@dataclass
class EventIn:
    event_key: str
    title: str
    start_at: datetime
    end_at: datetime | None = None
    venue: str | None = None
    neighborhood: str | None = None
    category: str | None = None
    price_range: str | None = None
    url: str | None = None
    pitch: str | None = None
    source: str | None = None
    # Curation layer, Phase 2 (enrichment/tagging) — defaults keep every
    # existing EventIn(...) call site (and test) passing unchanged.
    tags: list[str] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)  # [{"name": ..., "role": "performer|author|speaker"}]
    gist: str | None = None
    tag_confidence: float | None = None
    # Curation layer, Phase 3 (taste-profile matching) — why this event was
    # selected this run. In-memory only, not a DB column: set by
    # matching.select_with_matching, copied onto the Event research_job.py
    # gets back from insert_event (which doesn't persist it), and consumed
    # immediately by the same run's ballot-send SMS formatting.
    match_reasons: list[str] = field(default_factory=list)


@dataclass
class Event:
    id: UUID
    event_key: str
    title: str
    start_at: datetime
    end_at: datetime | None
    venue: str | None
    neighborhood: str | None
    category: str | None
    price_range: str | None
    url: str | None
    pitch: str | None
    source: str | None
    discovered_at: datetime
    calendar_event_id: str | None
    # Curation layer, Phase 2 (enrichment/tagging) — defaults keep every
    # existing Event(...) call site (and test) passing unchanged.
    tags: list[str] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    gist: str | None = None
    tag_confidence: float | None = None
    # Curation layer, Phase 3 — see EventIn.match_reasons; not read back
    # from a DB row (from_row never sets it), only assigned in-process.
    match_reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict) -> "Event":
        return cls(
            id=UUID(row["id"]) if isinstance(row["id"], str) else row["id"],
            event_key=row["event_key"],
            title=row["title"],
            start_at=_parse_dt(row["start_at"]),
            end_at=_parse_dt(row.get("end_at")),
            venue=row.get("venue"),
            neighborhood=row.get("neighborhood"),
            category=row.get("category"),
            price_range=row.get("price_range"),
            url=row.get("url"),
            pitch=row.get("pitch"),
            source=row.get("source"),
            discovered_at=_parse_dt(row.get("discovered_at")) or datetime.now(),
            calendar_event_id=row.get("calendar_event_id"),
            tags=row.get("tags") or [],
            entities=row.get("entities") or [],
            gist=row.get("gist"),
            tag_confidence=row.get("tag_confidence"),
        )


@dataclass
class Ballot:
    id: UUID
    event_id: UUID
    person: str
    batch_id: UUID
    list_number: int
    response: str | None
    sent_at: datetime
    responded_at: datetime | None

    @classmethod
    def from_row(cls, row: dict) -> "Ballot":
        return cls(
            id=row["id"],
            event_id=row["event_id"],
            person=row["person"],
            batch_id=row["batch_id"],
            list_number=row["list_number"],
            response=row.get("response"),
            sent_at=_parse_dt(row.get("sent_at")),
            responded_at=_parse_dt(row.get("responded_at")),
        )


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass
class ReplyParseResult:
    yes: list[int] = field(default_factory=list)
    no: list[int] = field(default_factory=list)
    unmapped_raw: str | None = None

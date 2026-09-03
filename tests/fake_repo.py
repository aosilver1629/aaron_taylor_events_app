"""In-memory stand-in for app.db.Repository, implementing the same method
surface so job/webhook logic can be exercised in tests without a live
Supabase project. Mirrors the SQL schema's constraints closely enough to
catch real bugs (unique batch/person/list_number, "both yes" join logic).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.models import Ballot, Event, EventIn
from app.utils.time import now_utc


class FakeRepository:
    def __init__(self):
        self.events: dict[UUID, Event] = {}
        self.ballots: dict[UUID, Ballot] = {}
        self.sms_log: list[dict] = []
        # Curation layer (Phase 1+): tests populate `self.sources[id] = {...}`
        # directly, same pattern as `self.events`/`self.ballots` — no
        # separate seed method, since real Repository seeding happens via
        # raw SQL (sql/seed_sources.sql), not through the app.
        self.sources: dict[UUID, dict] = {}
        self.source_runs: list[dict] = []

    # ---- events ----

    def event_key_exists(self, event_key: str) -> bool:
        return any(e.event_key == event_key for e in self.events.values())

    def insert_event(self, event: EventIn) -> Event:
        event_id = uuid4()
        record = Event(
            id=event_id,
            event_key=event.event_key,
            title=event.title,
            start_at=event.start_at,
            end_at=event.end_at,
            venue=event.venue,
            neighborhood=event.neighborhood,
            category=event.category,
            price_range=event.price_range,
            url=event.url,
            pitch=event.pitch,
            source=event.source,
            discovered_at=now_utc(),
            calendar_event_id=None,
            tags=list(event.tags),
            entities=list(event.entities),
            gist=event.gist,
            tag_confidence=event.tag_confidence,
        )
        self.events[event_id] = record
        return record

    def get_event(self, event_id: UUID) -> Event | None:
        return self.events.get(event_id)

    def set_calendar_event_id(self, event_id: UUID, calendar_event_id: str) -> None:
        self.events[event_id].calendar_event_id = calendar_event_id

    def update_event_enrichment(
        self, event_id: UUID, tags: list[str], entities: list[dict], gist, tag_confidence
    ) -> None:
        event = self.events[event_id]
        event.tags = tags
        event.entities = entities
        event.gist = gist
        event.tag_confidence = tag_confidence

    def get_events_with_empty_tags(self, limit: int = 200) -> list[Event]:
        return [e for e in self.events.values() if not e.tags][:limit]

    def get_events_awaiting_calendar_write(self) -> list[Event]:
        yes_people: dict[UUID, set[str]] = {}
        for ballot in self.ballots.values():
            if ballot.response == "yes":
                yes_people.setdefault(ballot.event_id, set()).add(ballot.person)
        return [
            e
            for e in self.events.values()
            if e.calendar_event_id is None
            and {"aaron", "tay"}.issubset(yes_people.get(e.id, set()))
        ]

    # ---- ballots ----

    def create_ballot_batch(
        self, person: str, batch_id: UUID, entries: list[tuple[UUID, int]]
    ) -> None:
        for event_id, list_number in entries:
            ballot_id = uuid4()
            self.ballots[ballot_id] = Ballot(
                id=ballot_id,
                event_id=event_id,
                person=person,
                batch_id=batch_id,
                list_number=list_number,
                response=None,
                sent_at=now_utc(),
                responded_at=None,
            )

    def get_latest_batch_id(self, person: str) -> UUID | None:
        person_ballots = [b for b in self.ballots.values() if b.person == person]
        if not person_ballots:
            return None
        return max(person_ballots, key=lambda b: b.sent_at).batch_id

    def get_ballot(self, batch_id: UUID, person: str, list_number: int) -> Ballot | None:
        for b in self.ballots.values():
            if b.batch_id == batch_id and b.person == person and b.list_number == list_number:
                return b
        return None

    def get_ballots_for_batch(self, batch_id: UUID, person: str) -> list[Ballot]:
        return [
            b for b in self.ballots.values() if b.batch_id == batch_id and b.person == person
        ]

    def resolve_ballot(self, ballot_id: UUID, response: str) -> None:
        b = self.ballots[ballot_id]
        b.response = response
        b.responded_at = now_utc()

    def expire_stale_ballots(self, older_than_hours: int = 48) -> int:
        cutoff = now_utc() - timedelta(hours=older_than_hours)
        count = 0
        for b in self.ballots.values():
            if b.response is None and b.sent_at < cutoff:
                b.response = "expired"
                count += 1
        return count

    def get_recent_resolved_ballots(self, limit: int = 50) -> list[dict]:
        resolved = [b for b in self.ballots.values() if b.response in ("yes", "no")]
        resolved.sort(key=lambda b: b.responded_at or datetime.min, reverse=True)
        out = []
        for b in resolved[:limit]:
            event = self.events.get(b.event_id)
            out.append(
                {
                    "response": b.response,
                    "event_id": str(b.event_id),
                    "person": b.person,
                    "events": {
                        "category": event.category if event else None,
                        "venue": event.venue if event else None,
                    },
                }
            )
        return out

    # ---- sms_log ----

    def log_sms(self, person: str | None, direction: str, body: str) -> None:
        self.sms_log.append({"person": person, "direction": direction, "body": body})

    # ---- sources / source_runs (curation layer, Phase 1) ----

    def get_sources(self, city: str = "sf") -> list[dict]:
        return [
            s for s in self.sources.values()
            if s.get("city", "sf") == city and s.get("enabled", True)
        ]

    def insert_source_run(
        self, source_id, fetch_method: str, extract_method: str, candidates: int, blocked_marker_seen: bool
    ) -> None:
        self.source_runs.append(
            {
                "id": uuid4(),
                "source_id": source_id,
                "run_at": now_utc(),
                "fetch_method": fetch_method,
                "extract_method": extract_method,
                "candidates": candidates,
                "blocked_marker_seen": blocked_marker_seen,
            }
        )

    def get_recent_source_runs(self, source_id, limit: int = 8) -> list[dict]:
        runs = [r for r in self.source_runs if r["source_id"] == source_id]
        runs.sort(key=lambda r: r["run_at"], reverse=True)
        return runs[:limit]

    def update_source_status(
        self, source_id, status: str, status_reason: str | None, consecutive_zero_runs: int = 0
    ) -> None:
        if source_id in self.sources:
            self.sources[source_id]["status"] = status
            self.sources[source_id]["status_reason"] = status_reason
            self.sources[source_id]["consecutive_zero_runs"] = consecutive_zero_runs

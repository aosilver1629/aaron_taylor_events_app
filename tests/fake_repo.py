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
        )
        self.events[event_id] = record
        return record

    def get_event(self, event_id: UUID) -> Event | None:
        return self.events.get(event_id)

    def set_calendar_event_id(self, event_id: UUID, calendar_event_id: str) -> None:
        self.events[event_id].calendar_event_id = calendar_event_id

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

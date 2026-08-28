from __future__ import annotations

from datetime import timedelta

from app.calendar_client.google_calendar import _build_event_body, run_calendar_job
from app.config import Settings
from app.models import EventIn
from app.utils.time import now_utc
from tests.fake_repo import FakeRepository


def _settings(**overrides) -> Settings:
    base = dict(dry_run=True, calendar_id="cal123")
    base.update(overrides)
    return Settings(**base)


def test_dry_run_never_writes_calendar_id():
    repo = FakeRepository()
    event = repo.insert_event(
        EventIn(event_key="k1", title="Both Yes Show", start_at=now_utc() + timedelta(days=1))
    )
    from uuid import uuid4

    repo.create_ballot_batch("aaron", uuid4(), [(event.id, 1)])
    repo.create_ballot_batch("tay", uuid4(), [(event.id, 1)])
    for b in repo.ballots.values():
        repo.resolve_ballot(b.id, "yes")

    result = run_calendar_job(repo, _settings(dry_run=True))

    assert result["written"] == 0
    assert repo.events[event.id].calendar_event_id is None


def test_only_both_yes_events_are_candidates():
    repo = FakeRepository()
    event = repo.insert_event(
        EventIn(event_key="k1", title="Only Aaron Yes", start_at=now_utc() + timedelta(days=1))
    )
    from uuid import uuid4

    repo.create_ballot_batch("aaron", uuid4(), [(event.id, 1)])
    repo.create_ballot_batch("tay", uuid4(), [(event.id, 1)])
    ballots = list(repo.ballots.values())
    repo.resolve_ballot(ballots[0].id, "yes")
    repo.resolve_ballot(ballots[1].id, "no")

    assert repo.get_events_awaiting_calendar_write() == []


def test_event_body_includes_reminder_and_event_key():
    event_in = EventIn(
        event_key="abc123",
        title="Test Show",
        start_at=now_utc() + timedelta(days=1),
        venue="The Fillmore",
        pitch="Great band",
        price_range="$50",
        url="https://example.com/show",
    )
    from tests.fake_repo import FakeRepository as FR

    repo = FR()
    event = repo.insert_event(event_in)

    body = _build_event_body(event)

    assert body["summary"] == "Test Show"
    assert body["location"] == "The Fillmore"
    assert "Great band" in body["description"]
    assert "$50" in body["description"]
    assert "https://example.com/show" in body["description"]
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}]
    assert body["extendedProperties"]["private"]["event_key"] == "abc123"
    assert "start" in body and "end" in body

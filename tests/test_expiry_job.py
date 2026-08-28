from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.jobs.expiry_job import run_expiry_job
from app.models import EventIn
from app.utils.time import now_utc
from tests.fake_repo import FakeRepository


def test_stale_ballots_expire_after_48_hours():
    repo = FakeRepository()
    event = repo.insert_event(
        EventIn(event_key="k1", title="Show", start_at=now_utc() + timedelta(days=1))
    )
    batch_id = uuid4()
    repo.create_ballot_batch("aaron", batch_id, [(event.id, 1)])
    ballot = repo.get_ballots_for_batch(batch_id, "aaron")[0]
    ballot.sent_at = now_utc() - timedelta(hours=49)

    result = run_expiry_job(repo)

    assert result == {"expired": 1}
    assert repo.get_ballots_for_batch(batch_id, "aaron")[0].response == "expired"


def test_recent_ballots_do_not_expire():
    repo = FakeRepository()
    event = repo.insert_event(
        EventIn(event_key="k1", title="Show", start_at=now_utc() + timedelta(days=1))
    )
    batch_id = uuid4()
    repo.create_ballot_batch("aaron", batch_id, [(event.id, 1)])

    result = run_expiry_job(repo)

    assert result == {"expired": 0}
    assert repo.get_ballots_for_batch(batch_id, "aaron")[0].response is None


def test_already_resolved_ballots_are_untouched():
    repo = FakeRepository()
    event = repo.insert_event(
        EventIn(event_key="k1", title="Show", start_at=now_utc() + timedelta(days=1))
    )
    batch_id = uuid4()
    repo.create_ballot_batch("aaron", batch_id, [(event.id, 1)])
    ballot = repo.get_ballots_for_batch(batch_id, "aaron")[0]
    ballot.sent_at = now_utc() - timedelta(hours=49)
    repo.resolve_ballot(ballot.id, "yes")

    result = run_expiry_job(repo)

    assert result == {"expired": 0}
    assert repo.get_ballots_for_batch(batch_id, "aaron")[0].response == "yes"

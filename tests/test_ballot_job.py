from __future__ import annotations

from datetime import timedelta

from app.config import Settings
from app.jobs.ballot_job import build_message, run_ballot_send_job
from app.models import EventIn
from app.sms.provider import MockSMSProvider
from app.utils.time import now_utc
from tests.fake_repo import FakeRepository


def _settings(**overrides) -> Settings:
    base = dict(dry_run=True, sms_provider="mock", aaron_phone="+15550001111", tay_phone="+15550002222")
    base.update(overrides)
    return Settings(**base)


def _make_event(repo: FakeRepository, title: str) -> object:
    return repo.insert_event(
        EventIn(
            event_key=f"key-{title}",
            title=title,
            start_at=now_utc() + timedelta(days=2),
            venue="Some Venue",
            category="concert",
            price_range="$20",
        )
    )


def test_ballot_send_creates_batches_for_both_people():
    repo = FakeRepository()
    events = [_make_event(repo, f"Show {i}") for i in range(3)]
    settings = _settings()
    provider = MockSMSProvider(repo, settings)

    result = run_ballot_send_job(repo, events, settings, provider)

    assert result == {"events": 3, "people": 2}
    aaron_batch = repo.get_latest_batch_id("aaron")
    tay_batch = repo.get_latest_batch_id("tay")
    assert aaron_batch is not None and tay_batch is not None
    assert aaron_batch != tay_batch

    aaron_ballots = repo.get_ballots_for_batch(aaron_batch, "aaron")
    assert sorted(b.list_number for b in aaron_ballots) == [1, 2, 3]
    assert all(b.response is None for b in aaron_ballots)


def test_outbound_sms_logged_for_both_people():
    repo = FakeRepository()
    events = [_make_event(repo, "Show A")]
    settings = _settings()
    run_ballot_send_job(repo, events, settings, MockSMSProvider(repo, settings))

    outbound = [row for row in repo.sms_log if row["direction"] == "out"]
    assert len(outbound) == 2
    assert {row["person"] for row in outbound} == {"aaron", "tay"}


def test_no_events_sends_nothing():
    repo = FakeRepository()
    result = run_ballot_send_job(repo, [], _settings(), MockSMSProvider(repo))
    assert result == {"events": 0, "people": 0}
    assert repo.sms_log == []


def test_build_message_trims_to_fit_1600_chars():
    repo = FakeRepository()
    long_title = "A" * 200
    events = [_make_event(repo, f"{long_title} {i}") for i in range(20)]

    body, included = build_message(events)

    assert len(body) <= 1600
    assert len(included) < len(events)
    assert len(included) >= 1


def test_build_message_never_splits_into_two_sends():
    # Trimming, not segmentation, is the only overflow strategy.
    repo = FakeRepository()
    events = [_make_event(repo, "X" * 300) for _ in range(12)]
    body, included = build_message(events)
    assert len(body) <= 1600

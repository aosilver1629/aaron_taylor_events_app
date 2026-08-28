"""Unit tests for app.sms.webhook ballot resolution logic, against
FakeRepository so no live Supabase/Anthropic calls are needed. The reply
parser itself is monkeypatched — its own correctness is covered by
tests/test_reply_parser.py (which needs a live key); here we're testing
the surrounding logic: most-recent-batch-only, default-to-no for anything
the parser didn't return, and the confirmation text.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from app.config import Settings
from app.models import EventIn, ReplyParseResult
from app.sms import webhook as webhook_mod
from app.sms.provider import MockSMSProvider
from app.utils.time import now_utc
from tests.fake_repo import FakeRepository


def _settings(**overrides) -> Settings:
    base = dict(
        dry_run=True,
        sms_provider="mock",
        aaron_phone="+15550001111",
        tay_phone="+15550002222",
    )
    base.update(overrides)
    return Settings(**base)


def _seed_batch(repo: FakeRepository, person: str, titles: list[str]) -> tuple:
    batch_id = uuid4()
    entries = []
    for i, title in enumerate(titles, start=1):
        event = repo.insert_event(
            EventIn(
                event_key=f"key-{title}-{uuid4()}",
                title=title,
                start_at=now_utc() + timedelta(days=3),
                venue="Test Venue",
                category="concert",
                price_range="$10",
            )
        )
        entries.append((event.id, i))
    repo.create_ballot_batch(person, batch_id, entries)
    return batch_id, entries


def test_default_to_no_for_unmentioned_numbers(monkeypatch):
    repo = FakeRepository()
    batch_id, entries = _seed_batch(repo, "aaron", ["Show A", "Show B", "Show C"])

    monkeypatch.setattr(
        webhook_mod, "parse_reply", lambda body, numbered: ReplyParseResult(yes=[1], no=[])
    )

    reply = webhook_mod.resolve_reply(repo, "aaron", "just 1")

    ballots = repo.get_ballots_for_batch(batch_id, "aaron")
    responses = {b.list_number: b.response for b in ballots}
    assert responses == {1: "yes", 2: "no", 3: "no"}
    assert "Show A" in reply
    assert "Show B" not in reply


def test_reply_only_resolves_most_recent_batch(monkeypatch):
    repo = FakeRepository()
    old_batch_id, _ = _seed_batch(repo, "tay", ["Old Show"])
    # Simulate the old batch already having been answered and expired.
    for b in repo.get_ballots_for_batch(old_batch_id, "tay"):
        repo.resolve_ballot(b.id, "expired")

    new_batch_id, _ = _seed_batch(repo, "tay", ["New Show 1", "New Show 2"])

    monkeypatch.setattr(
        webhook_mod, "parse_reply", lambda body, numbered: ReplyParseResult(yes=[1], no=[])
    )
    webhook_mod.resolve_reply(repo, "tay", "1")

    old_ballots = repo.get_ballots_for_batch(old_batch_id, "tay")
    assert all(b.response == "expired" for b in old_ballots), "old batch must not be touched"

    new_ballots = repo.get_ballots_for_batch(new_batch_id, "tay")
    assert {b.list_number: b.response for b in new_ballots} == {1: "yes", 2: "no"}


def test_no_open_batch_returns_friendly_message():
    repo = FakeRepository()
    reply = webhook_mod.resolve_reply(repo, "aaron", "1")
    assert "no event list" in reply.lower() or "nothing" in reply.lower()


def test_closed_batch_returns_friendly_message(monkeypatch):
    repo = FakeRepository()
    batch_id, _ = _seed_batch(repo, "aaron", ["Show A"])
    for b in repo.get_ballots_for_batch(batch_id, "aaron"):
        repo.resolve_ballot(b.id, "yes")

    reply = webhook_mod.resolve_reply(repo, "aaron", "1")
    assert "closed" in reply.lower()


def test_unknown_sender_is_rejected_and_never_replied():
    repo = FakeRepository()
    settings = _settings()
    provider = MockSMSProvider(repo, settings)

    reply = webhook_mod.handle_inbound_sms(repo, provider, settings, "+19998887777", "1")

    assert reply == ""
    assert repo.sms_log == []  # never logged, never replied


def test_stop_keyword_does_not_hit_reply_parser(monkeypatch):
    repo = FakeRepository()
    settings = _settings()
    provider = MockSMSProvider(repo, settings)

    def _boom(*args, **kwargs):
        raise AssertionError("parse_reply should not be called for STOP")

    monkeypatch.setattr(webhook_mod, "parse_reply", _boom)

    reply = webhook_mod.handle_inbound_sms(repo, provider, settings, settings.aaron_phone, "STOP")
    assert "unsubscribed" in reply.lower()


def test_help_keyword_returns_help_text():
    repo = FakeRepository()
    settings = _settings()
    provider = MockSMSProvider(repo, settings)

    reply = webhook_mod.handle_inbound_sms(repo, provider, settings, settings.tay_phone, "help")
    assert "stop" in reply.lower()


def test_both_yes_triggers_calendar_readiness(monkeypatch):
    repo = FakeRepository()
    event = repo.insert_event(
        EventIn(
            event_key="k1",
            title="Both Yes Show",
            start_at=now_utc() + timedelta(days=1),
        )
    )
    batch_a = uuid4()
    batch_b = uuid4()
    repo.create_ballot_batch("aaron", batch_a, [(event.id, 1)])
    repo.create_ballot_batch("tay", batch_b, [(event.id, 1)])

    assert repo.get_events_awaiting_calendar_write() == []

    for b in repo.ballots.values():
        repo.resolve_ballot(b.id, "yes")

    ready = repo.get_events_awaiting_calendar_write()
    assert [e.id for e in ready] == [event.id]

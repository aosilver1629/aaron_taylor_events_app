"""Curation layer, Phase 3: the "why" clause app.sms.formatting threads
from Event.match_reasons into the ballot line.
"""
from __future__ import annotations

from datetime import timedelta

from app.models import EventIn
from app.sms.formatting import event_label
from app.utils.time import format_event_time, now_utc
from tests.fake_repo import FakeRepository


def _event(repo: FakeRepository, match_reasons=None, title="Some Show"):
    event = repo.insert_event(
        EventIn(
            event_key=f"key-{title}",
            title=title,
            start_at=now_utc() + timedelta(days=2),
            venue="Some Venue",
            category="concert",
            price_range="$20",
        )
    )
    event.match_reasons = match_reasons or []
    return event


def test_no_match_reasons_leaves_label_unchanged():
    repo = FakeRepository()
    event = _event(repo)
    label = event_label(event)
    assert label == f"{format_event_time(event.start_at)} - Some Show at Some Venue ($20)"


def test_declared_reason_appends_readable_clause():
    repo = FakeRepository()
    event = _event(repo, match_reasons=["declared:music/folk"])
    label = event_label(event)
    assert label.endswith("you like folk")


def test_entity_reason_appends_readable_clause():
    repo = FakeRepository()
    event = _event(repo, match_reasons=["entity:Watchhouse"])
    label = event_label(event)
    assert label.endswith("features Watchhouse")


def test_only_first_reason_is_used():
    repo = FakeRepository()
    event = _event(repo, match_reasons=["declared:music/folk", "entity:Watchhouse"])
    label = event_label(event)
    assert label.endswith("you like folk")
    assert "Watchhouse" not in label


def test_reason_clause_is_gsm7_safe():
    repo = FakeRepository()
    event = _event(repo, match_reasons=["entity:Cafe—Del’Sol"])
    label = event_label(event)
    assert all(ord(c) < 128 for c in label)

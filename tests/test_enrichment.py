"""Curation layer, Phase 2: enrich_events, fully mocked (no live Claude
calls in the default suite, matching the repo's existing pattern — see
tests/test_reply_parser.py's requires_live_claude skip).
"""
from __future__ import annotations

from datetime import datetime, timezone

import app.research.enrichment as enrichment_mod
from app.models import EventIn


class _FakeToolUseBlock:
    def __init__(self, name, input_):
        self.type = "tool_use"
        self.name = name
        self.input = input_


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeSettings:
    anthropic_api_key = "test"


def _event(title="Some Show") -> EventIn:
    return EventIn(
        event_key=f"key-{title}",
        title=title,
        start_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        venue="Test Venue",
        category="concert",
        pitch="fun show",
        source="Test Source",
    )


def _patch_response(monkeypatch, response: _FakeResponse) -> None:
    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return response

    monkeypatch.setattr(enrichment_mod, "Anthropic", lambda **k: _FakeClient())


def _patch_raising(monkeypatch, exc: Exception) -> None:
    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise exc

    monkeypatch.setattr(enrichment_mod, "Anthropic", lambda **k: _FakeClient())


def test_enrich_events_noop_on_empty_list(monkeypatch):
    calls = []
    monkeypatch.setattr(enrichment_mod, "Anthropic", lambda **k: calls.append(1))
    enrichment_mod.enrich_events([], settings=None)
    assert calls == []


def test_valid_tags_are_assigned(monkeypatch):
    events = [_event("Show A")]
    _patch_response(
        monkeypatch,
        _FakeResponse(
            [
                _FakeToolUseBlock(
                    "assign_enrichment",
                    {
                        "events": [
                            {
                                "index": 0,
                                "tags": ["music/folk"],
                                "entities": [{"name": "Watchhouse", "role": "performer"}],
                                "gist": "A folk duo plays an intimate acoustic set.",
                                "confidence": 0.9,
                            }
                        ]
                    },
                )
            ]
        ),
    )

    enrichment_mod.enrich_events(events, _FakeSettings())

    assert events[0].tags == ["music/folk"]
    assert events[0].entities == [{"name": "Watchhouse", "role": "performer"}]
    assert events[0].gist == "A folk duo plays an intimate acoustic set."
    assert events[0].tag_confidence == 0.9


def test_invalid_tags_dropped_and_logged(monkeypatch, caplog):
    import logging

    events = [_event("Show B")]
    _patch_response(
        monkeypatch,
        _FakeResponse(
            [
                _FakeToolUseBlock(
                    "assign_enrichment",
                    {
                        "events": [
                            {
                                "index": 0,
                                "tags": ["music/folk", "totally/madeup"],
                                "entities": [],
                                "gist": "gist",
                                "confidence": 0.5,
                            }
                        ]
                    },
                )
            ]
        ),
    )

    with caplog.at_level(logging.INFO, logger="enrichment"):
        enrichment_mod.enrich_events(events, _FakeSettings())

    assert events[0].tags == ["music/folk"]
    assert any("taxonomy_proposal" in r.message for r in caplog.records)


def test_event_with_no_surviving_tags_keeps_empty_list_and_category(monkeypatch):
    events = [_event("Show C")]
    events[0].category = "concert"
    _patch_response(
        monkeypatch,
        _FakeResponse(
            [
                _FakeToolUseBlock(
                    "assign_enrichment",
                    {
                        "events": [
                            {"index": 0, "tags": ["nonsense/tag"], "entities": [], "gist": "g", "confidence": 0.1}
                        ]
                    },
                )
            ]
        ),
    )

    enrichment_mod.enrich_events(events, _FakeSettings())

    assert events[0].tags == []
    assert events[0].category == "concert"  # untouched


def test_call_failure_leaves_all_events_untagged(monkeypatch):
    events = [_event("Show D"), _event("Show E")]
    _patch_raising(monkeypatch, RuntimeError("boom"))
    # This test cares about failure *handling*, not with_retry's real
    # exponential-backoff timing — call fn() once, skip the real sleeps.
    monkeypatch.setattr(enrichment_mod, "with_retry", lambda fn, **kw: fn())

    enrichment_mod.enrich_events(events, _FakeSettings())

    assert events[0].tags == []
    assert events[1].tags == []
    assert events[0].gist is None


def test_index_matching_survives_shuffled_partial_response(monkeypatch):
    events = [_event("A"), _event("B"), _event("C")]
    # Response only covers indices 2 and 0, out of order — index 1 (Show B)
    # should simply stay untagged, not break the others.
    _patch_response(
        monkeypatch,
        _FakeResponse(
            [
                _FakeToolUseBlock(
                    "assign_enrichment",
                    {
                        "events": [
                            {"index": 2, "tags": ["comedy/standup"], "entities": [], "gist": "g2", "confidence": 0.7},
                            {"index": 0, "tags": ["music/rock"], "entities": [], "gist": "g0", "confidence": 0.6},
                        ]
                    },
                )
            ]
        ),
    )

    enrichment_mod.enrich_events(events, _FakeSettings())

    assert events[0].tags == ["music/rock"]
    assert events[1].tags == []
    assert events[2].tags == ["comedy/standup"]

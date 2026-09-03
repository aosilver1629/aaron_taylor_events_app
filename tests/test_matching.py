"""Curation layer, Phase 3: app.research.matching, fully mocked (no live
Claude calls in the default suite, matching the repo's existing pattern —
see tests/test_enrichment.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

import app.research.matching as matching_mod
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


def _patch_response(monkeypatch, response: _FakeResponse) -> None:
    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return response

    monkeypatch.setattr(matching_mod, "Anthropic", lambda **k: _FakeClient())
    monkeypatch.setattr(matching_mod, "get_settings", lambda: _FakeSettings())


def _patch_raising(monkeypatch, exc: Exception) -> None:
    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise exc

    monkeypatch.setattr(matching_mod, "Anthropic", lambda **k: _FakeClient())
    monkeypatch.setattr(matching_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(matching_mod, "with_retry", lambda fn, **kw: fn())


def _event(key, title, tags=None, entities=None, venue="Test Venue", category="concert", days=1) -> EventIn:
    return EventIn(
        event_key=key,
        title=title,
        start_at=datetime(2026, 9, 10 + days, tzinfo=timezone.utc),
        venue=venue,
        category=category,
        pitch="fun",
        source="test",
        tags=tags or [],
        entities=entities or [],
    )


def _profile(**overrides) -> dict:
    base = {"hard_excludes": [], "include_tags": [], "include_entities": [], "exemplars": []}
    base.update(overrides)
    return base


# ---- hard_excluded ----


def test_hard_excluded_by_direct_tag_hit():
    event = _event("e1", "Metal Show", tags=["music/rock"])
    profiles = [_profile(hard_excludes=["music/rock"])]
    assert matching_mod.hard_excluded(event, profiles) == "music/rock"


def test_not_excluded_when_no_tag_overlap():
    event = _event("e1", "Folk Show", tags=["music/folk"])
    profiles = [_profile(hard_excludes=["music/rock"])]
    assert matching_mod.hard_excluded(event, profiles) is None


def test_excludes_union_across_both_profiles():
    event = _event("e1", "Standup Night", tags=["comedy/standup"])
    profiles = [_profile(hard_excludes=["music/rock"]), _profile(hard_excludes=["comedy/standup"])]
    assert matching_mod.hard_excluded(event, profiles) == "comedy/standup"


def test_category_fallback_only_when_tags_empty():
    event = _event("e1", "Untagged Concert", tags=[], category="concert")
    profiles = [_profile(hard_excludes=["music/rock"])]
    assert matching_mod.hard_excluded(event, profiles) == "music/rock"


def test_category_fallback_does_not_fire_when_tags_present():
    # Tagging succeeded and didn't match music/rock — the coarse category
    # fallback must not override a real (non-matching) tag-level result.
    event = _event("e1", "Jazz Show", tags=["music/jazz"], category="concert")
    profiles = [_profile(hard_excludes=["music/rock"])]
    assert matching_mod.hard_excluded(event, profiles) is None


# ---- the invariant: hard exclude beats everything, always ----


def test_hard_exclude_wins_even_with_max_learned_and_exemplar_score(monkeypatch):
    excluded = _event("bad", "Metal Night", tags=["music/rock"])
    survivor = _event("good", "Folk Night", tags=["music/folk"])

    profiles = {
        "aaron": _profile(hard_excludes=["music/rock"], exemplars=["loud metal shows"]),
        "tay": _profile(),
    }
    weights = {"tag": {"music/rock": 5}, "venue": {}, "entity": {}}

    _patch_response(
        monkeypatch,
        _FakeResponse(
            [
                _FakeToolUseBlock(
                    "score_exemplar_relevance",
                    {"scores": [{"event_key": "bad", "person": "aaron", "relevance": 10}]},
                )
            ]
        ),
    )

    chosen = matching_mod.select_with_matching([excluded, survivor], profiles, weights, cap=1)

    assert [e.event_key for e in chosen] == ["good"]


# ---- scoring ----


def test_declared_include_tag_scores_and_reasons():
    event = _event("e1", "Folk Show", tags=["music/folk"])
    profiles = {"aaron": _profile(include_tags=["music/folk"]), "tay": _profile()}
    chosen = matching_mod.select_with_matching([event], profiles, {}, cap=5)
    assert chosen[0].match_reasons == ["declared:music/folk"]


def test_declared_include_entity_scores_higher_than_tag():
    tag_event = _event("e1", "Folk Show", tags=["music/folk"], days=1)
    entity_event = _event("e2", "Watchhouse Show", entities=[{"name": "Watchhouse", "role": "performer"}], days=2)
    profiles = {
        "aaron": _profile(include_tags=["music/folk"], include_entities=["watchhouse"]),
        "tay": _profile(),
    }
    chosen = matching_mod.select_with_matching([tag_event, entity_event], profiles, {}, cap=1)
    assert chosen[0].event_key == "e2"


def test_learned_weight_is_capped_below_a_single_declared_include():
    # Many positive-net-count learned signals on one event still can't
    # outscore a single declared include tag on another.
    declared_event = _event("declared", "Folk Show", tags=["music/folk"], days=1)
    learned_event = _event(
        "learned", "Loud Show", tags=["music/rock", "music/punk"], venue="The Venue", days=2,
        entities=[{"name": "Some Band", "role": "performer"}],
    )
    profiles = {"aaron": _profile(include_tags=["music/folk"]), "tay": _profile()}
    weights = {
        "tag": {"music/rock": 3, "music/punk": 3},
        "venue": {"The Venue": 3},
        "entity": {"Some Band": 3},
    }
    chosen = matching_mod.select_with_matching([declared_event, learned_event], profiles, weights, cap=1)
    assert chosen[0].event_key == "declared"


def test_exemplar_scoring_skipped_when_under_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(matching_mod, "Anthropic", lambda **k: calls.append(1))
    event = _event("e1", "Folk Show", tags=["music/folk"])
    profiles = {"aaron": _profile(exemplars=["some exemplar"]), "tay": _profile()}
    matching_mod.select_with_matching([event], profiles, {}, cap=5)
    assert calls == []


def test_exemplar_call_failure_leaves_scores_intact(monkeypatch):
    events = [_event(f"e{i}", f"Show {i}", days=i) for i in range(3)]
    profiles = {"aaron": _profile(exemplars=["some exemplar"]), "tay": _profile()}
    _patch_raising(monkeypatch, RuntimeError("boom"))

    chosen = matching_mod.select_with_matching(events, profiles, {}, cap=2)

    assert len(chosen) == 2


def test_empty_profiles_score_zero_and_sort_by_start_at():
    events = [_event("e2", "Later", days=5), _event("e1", "Earlier", days=1)]
    profiles = {"aaron": _profile(), "tay": _profile()}
    chosen = matching_mod.select_with_matching(events, profiles, {}, cap=5)
    assert [e.event_key for e in chosen] == ["e1", "e2"]

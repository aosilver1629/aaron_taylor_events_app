"""Curation layer: app.research.profile_parser, fully mocked (no live
Claude calls in the default suite, matching the repo's existing pattern —
see tests/test_enrichment.py).
"""
from __future__ import annotations

import app.research.profile_parser as parser_mod


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

    monkeypatch.setattr(parser_mod, "Anthropic", lambda **k: _FakeClient())
    monkeypatch.setattr(parser_mod, "get_settings", lambda: _FakeSettings())


def _patch_raising(monkeypatch, exc: Exception) -> None:
    class _FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise exc

    monkeypatch.setattr(parser_mod, "Anthropic", lambda **k: _FakeClient())
    monkeypatch.setattr(parser_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(parser_mod, "with_retry", lambda fn, **kw: fn())


def _tool_response(**fields):
    base = {"hard_excludes": [], "include_tags": [], "include_entities": [], "exemplars": []}
    base.update(fields)
    return _FakeResponse([_FakeToolUseBlock("parse_preferences", base)])


def test_empty_text_short_circuits_without_a_call(monkeypatch):
    calls = []
    monkeypatch.setattr(parser_mod, "Anthropic", lambda **k: calls.append(1))
    result = parser_mod.parse_preference_text("   ")
    assert result == parser_mod.EMPTY_PARSE_RESULT
    assert calls == []


def test_valid_fields_pass_through(monkeypatch):
    _patch_response(
        monkeypatch,
        _tool_response(
            hard_excludes=["music/punk"],
            include_tags=["music/folk"],
            include_entities=["Watchhouse"],
            exemplars=["outdoor food markets with live music"],
        ),
    )
    result = parser_mod.parse_preference_text("no punk, I love Watchhouse and outdoor food markets")
    assert result == {
        "hard_excludes": ["music/punk"],
        "include_tags": ["music/folk"],
        "include_entities": ["Watchhouse"],
        "exemplars": ["outdoor food markets with live music"],
    }


def test_invalid_tags_dropped_and_logged(monkeypatch, caplog):
    import logging

    _patch_response(
        monkeypatch,
        _tool_response(hard_excludes=["music/folk", "totally/madeup"]),
    )
    with caplog.at_level(logging.INFO, logger="profile_parser"):
        result = parser_mod.parse_preference_text("no folk or made-up stuff")
    assert result["hard_excludes"] == ["music/folk"]
    assert any("taxonomy_proposal" in r.message for r in caplog.records)


def test_exemplars_capped_at_five_and_truncated(monkeypatch):
    long_one = "x" * 300
    _patch_response(
        monkeypatch,
        _tool_response(exemplars=[f"e{i}" for i in range(4)] + [long_one, "overflow"]),
    )
    result = parser_mod.parse_preference_text("lots of vibes")
    assert len(result["exemplars"]) == 5
    assert len(result["exemplars"][4]) == parser_mod.MAX_EXEMPLAR_LEN


def test_call_failure_returns_empty_result(monkeypatch):
    _patch_raising(monkeypatch, RuntimeError("boom"))
    result = parser_mod.parse_preference_text("no metal")
    assert result == parser_mod.EMPTY_PARSE_RESULT


def test_entities_are_stripped_and_blank_entries_dropped(monkeypatch):
    _patch_response(monkeypatch, _tool_response(include_entities=["  Watchhouse  ", "", "   "]))
    result = parser_mod.parse_preference_text("I like Watchhouse")
    assert result["include_entities"] == ["Watchhouse"]

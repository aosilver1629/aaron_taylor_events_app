"""Curation layer, Phase 1: run_deterministic_research_call must work with
an arbitrary `sources` list (not just the hardcoded default) and invoke
`record_run` once per source with the right data — this is the whole
mechanism that lets the source registry replace the hardcoded list without
any change to the fetch/extract logic itself. Network/Claude calls are
monkeypatched out entirely.
"""
from __future__ import annotations

import app.research.deterministic_search as ds_mod


def test_default_sources_used_when_none_passed(monkeypatch):
    seen_sources = []

    def fake_get_source_text(source):
        seen_sources.append(source["label"])
        return "", "http", False

    monkeypatch.setattr(ds_mod, "_get_source_text", fake_get_source_text)
    monkeypatch.setattr(ds_mod, "_extract_from_source", lambda *a, **k: [])
    monkeypatch.setattr(ds_mod, "_run_regex_parser", lambda *a, **k: [])

    ds_mod.run_deterministic_research_call("", "")

    assert seen_sources == [s["label"] for s in ds_mod.SOURCES]


def test_custom_sources_list_is_used_instead_of_default(monkeypatch):
    custom_sources = [
        {"label": "Custom A", "url": "https://a.example.com", "kind": "venue"},
        {"label": "Custom B", "url": "https://b.example.com", "kind": "food"},
    ]
    seen_sources = []

    def fake_get_source_text(source):
        seen_sources.append(source["label"])
        return "", "http", False

    monkeypatch.setattr(ds_mod, "_get_source_text", fake_get_source_text)
    monkeypatch.setattr(ds_mod, "_extract_from_source", lambda *a, **k: [])
    monkeypatch.setattr(ds_mod, "_run_regex_parser", lambda *a, **k: [])

    ds_mod.run_deterministic_research_call("", "", sources=custom_sources)

    assert seen_sources == ["Custom A", "Custom B"]


def test_record_run_called_once_per_source_with_expected_args(monkeypatch):
    custom_sources = [{"label": "Custom A", "url": "https://a.example.com", "kind": "venue"}]

    monkeypatch.setattr(ds_mod, "_get_source_text", lambda source: ("some text", "http", False))
    monkeypatch.setattr(ds_mod, "_extract_from_source", lambda *a, **k: [{"title": "Event"}])
    monkeypatch.setattr(ds_mod, "_run_regex_parser", lambda *a, **k: [])

    calls = []
    ds_mod.run_deterministic_research_call(
        "", "", sources=custom_sources, record_run=lambda *args: calls.append(args)
    )

    assert len(calls) == 1
    source, fetch_method, extract_method, candidate_count, blocked = calls[0]
    assert source["label"] == "Custom A"
    assert fetch_method == "http"
    assert extract_method == "llm"
    assert candidate_count == 1
    assert blocked is False


def test_record_run_reflects_regex_success(monkeypatch):
    custom_sources = [
        {"label": "Custom A", "url": "https://a.example.com", "kind": "venue", "parser": "fake"}
    ]

    monkeypatch.setattr(ds_mod, "_get_source_text", lambda source: ("some text", "http", False))
    monkeypatch.setattr(ds_mod, "_run_regex_parser", lambda *a, **k: [{"title": "Regex Event"}])

    def fail_extract(*a, **k):
        raise AssertionError("LLM extraction should not run when the regex parser found something")

    monkeypatch.setattr(ds_mod, "_extract_from_source", fail_extract)

    calls = []
    ds_mod.run_deterministic_research_call(
        "", "", sources=custom_sources, record_run=lambda *args: calls.append(args)
    )

    _, _, extract_method, candidate_count, _ = calls[0]
    assert extract_method == "regex"
    assert candidate_count == 1


def test_no_record_run_call_when_none_provided(monkeypatch):
    custom_sources = [{"label": "Custom A", "url": "https://a.example.com", "kind": "venue"}]
    monkeypatch.setattr(ds_mod, "_get_source_text", lambda source: ("", "http", False))
    monkeypatch.setattr(ds_mod, "_extract_from_source", lambda *a, **k: [])
    monkeypatch.setattr(ds_mod, "_run_regex_parser", lambda *a, **k: [])

    # Should not raise even though record_run defaults to None.
    result = ds_mod.run_deterministic_research_call("", "", sources=custom_sources)
    assert result == []

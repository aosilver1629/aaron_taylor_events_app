"""Curation layer, Phase 1: source registry loading, status-transition
rules, and run recording. All against FakeRepository — no live network or
Supabase needed.
"""
from __future__ import annotations

from uuid import uuid4

from app.research import source_registry as registry_mod
from app.research.deterministic_search import SOURCES
from tests.fake_repo import FakeRepository


def _seed_source(repo: FakeRepository, **overrides) -> dict:
    source_id = overrides.pop("id", uuid4())
    row = {
        "id": source_id,
        "label": "Test Venue",
        "city": "sf",
        "url": "https://example.com",
        "kind": "venue",
        "preferred_tier": 2,
        "parser_id": None,
        "extraction_rules": None,
        "enabled": True,
        "status": "healthy",
        "status_reason": None,
        "consecutive_zero_runs": 0,
    }
    row.update(overrides)
    repo.sources[source_id] = row
    return row


# ---- load_sources ---------------------------------------------------------


def test_load_sources_falls_back_to_hardcoded_list_when_table_empty():
    repo = FakeRepository()
    assert registry_mod.load_sources(repo) == list(SOURCES)


def test_load_sources_shapes_db_rows_like_hardcoded_entries():
    repo = FakeRepository()
    row = _seed_source(
        repo,
        label="The Chapel",
        url="https://thechapelsf.com/calendar/",
        kind="venue",
        parser_id="chapel",
        extraction_rules="Some template {label} {url}",
    )
    loaded = registry_mod.load_sources(repo)
    assert len(loaded) == 1
    entry = loaded[0]
    assert entry["id"] == row["id"]
    assert entry["label"] == "The Chapel"
    assert entry["url"] == "https://thechapelsf.com/calendar/"
    assert entry["kind"] == "venue"
    assert entry["parser"] == "chapel"
    assert entry["extraction_rules"] == "Some template {label} {url}"


def test_load_sources_omits_parser_key_when_no_parser_id():
    repo = FakeRepository()
    _seed_source(repo, parser_id=None)
    entry = registry_mod.load_sources(repo)[0]
    assert "parser" not in entry


def test_load_sources_filters_disabled_and_other_cities():
    repo = FakeRepository()
    _seed_source(repo, label="Disabled Source", enabled=False)
    _seed_source(repo, label="NYC Source", city="nyc")
    _seed_source(repo, label="SF Source", city="sf", enabled=True)
    loaded = registry_mod.load_sources(repo, city="sf")
    assert [s["label"] for s in loaded] == ["SF Source"]


# ---- status computation ----------------------------------------------------


def _run(candidates: int, blocked: bool = False) -> dict:
    return {"candidates": candidates, "blocked_marker_seen": blocked}


def test_status_healthy_with_no_run_history():
    status, reason, zero_streak = registry_mod._compute_status([])
    assert status == "healthy"
    assert reason is None
    assert zero_streak == 0


def test_status_blocked_on_latest_marker():
    recent = [_run(0, blocked=True), _run(5), _run(6)]
    status, reason, _ = registry_mod._compute_status(recent)
    assert status == "blocked"
    assert reason is not None


def test_status_silent_after_two_consecutive_zero_runs():
    recent = [_run(0), _run(0), _run(8)]
    status, reason, zero_streak = registry_mod._compute_status(recent)
    assert status == "silent"
    assert zero_streak == 2


def test_status_not_silent_on_single_zero_run():
    recent = [_run(0), _run(8), _run(9)]
    status, _, zero_streak = registry_mod._compute_status(recent)
    assert status == "healthy"
    assert zero_streak == 1


def test_status_no_degraded_before_three_runs_of_history():
    # Only 2 prior runs — degraded must not fire no matter how low latest is.
    recent = [_run(1), _run(10), _run(10)]
    status, _, _ = registry_mod._compute_status(recent)
    assert status == "healthy"


def test_status_degraded_when_latest_below_40_percent_of_trailing_median():
    # prior = [10, 10, 10] -> median 10; latest 3 < 0.4*10 = 4
    recent = [_run(3), _run(10), _run(10), _run(10)]
    status, reason, _ = registry_mod._compute_status(recent)
    assert status == "degraded"
    assert reason is not None


def test_status_healthy_when_latest_holds_above_degraded_threshold():
    # prior = [10, 10, 10] -> median 10; latest 5 >= 0.4*10 = 4
    recent = [_run(5), _run(10), _run(10), _run(10)]
    status, _, _ = registry_mod._compute_status(recent)
    assert status == "healthy"


def test_status_recovers_to_healthy_after_normal_run():
    recent = [_run(10), _run(0), _run(0)]
    status, _, zero_streak = registry_mod._compute_status(recent)
    assert status == "healthy"
    assert zero_streak == 0


# ---- record_source_run ------------------------------------------------------


def test_record_source_run_noop_for_hardcoded_fallback_source():
    repo = FakeRepository()
    hardcoded_source = dict(SOURCES[0])  # no "id" key
    result = registry_mod.record_source_run(repo, hardcoded_source, "http", "llm", 3, False)
    assert result is None
    assert repo.source_runs == []


def test_record_source_run_persists_run_and_updates_status():
    repo = FakeRepository()
    row = _seed_source(repo, status="healthy")
    source = {"id": row["id"], "label": row["label"], "status": "healthy"}

    status = registry_mod.record_source_run(repo, source, "http", "regex", 5, False)

    assert status == "healthy"
    assert len(repo.source_runs) == 1
    assert repo.source_runs[0]["candidates"] == 5
    assert repo.sources[row["id"]]["status"] == "healthy"


def test_record_source_run_transitions_to_blocked():
    repo = FakeRepository()
    row = _seed_source(repo, status="healthy")
    source = {"id": row["id"], "label": row["label"], "status": "healthy"}

    status = registry_mod.record_source_run(repo, source, "search_fallback", "llm", 0, True)

    assert status == "blocked"
    assert repo.sources[row["id"]]["status"] == "blocked"
    assert repo.sources[row["id"]]["status_reason"] is not None


def test_record_source_run_logs_on_status_change(caplog):
    import logging

    repo = FakeRepository()
    row = _seed_source(repo, status="healthy")
    source = {"id": row["id"], "label": row["label"], "status": "healthy"}

    with caplog.at_level(logging.INFO, logger="source_registry"):
        registry_mod.record_source_run(repo, source, "search_fallback", "llm", 0, True)

    assert any("source_status_changed" in r.message for r in caplog.records)


# ---- get_source_health ------------------------------------------------------


def test_get_source_health_shape():
    repo = FakeRepository()
    row = _seed_source(repo, label="Test Venue", status="degraded", status_reason="low yield")
    registry_mod.record_source_run(
        repo, {"id": row["id"], "label": row["label"], "status": "healthy"}, "http", "regex", 4, False
    )

    health = registry_mod.get_source_health(repo)

    assert len(health) == 1
    entry = health[0]
    assert entry["label"] == "Test Venue"
    assert entry["tier_in_use"] == "http/regex"
    assert entry["recent_candidates"] == [4]
    assert "status" in entry
    assert "status_reason" in entry

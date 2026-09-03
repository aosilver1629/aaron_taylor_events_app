"""Research job orchestration, with the Ticketmaster/Bandsintown fetches and
the Claude calls all monkeypatched — this tests validation, dedup, and the
12-event cap, not the live external calls (those require real credentials
and are out of scope for this build environment; see docs/BUILD_NOTES.md).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import app.jobs.research_job as research_job_mod
from app.utils.time import now_utc
from tests.fake_repo import FakeRepository


@pytest.fixture(autouse=True)
def _no_op_enrichment(monkeypatch):
    """Curation layer Phase 2 wired enrich_events unconditionally into
    run_research_job. Without this, every test below (none of which care
    about tagging) would hit with_retry's real exponential backoff against
    a real/failing Anthropic call. The one test that does care about
    tagging overrides this with its own monkeypatch.setattr call."""
    monkeypatch.setattr(research_job_mod, "enrich_events", lambda events, settings: None)


def _candidate(title, days_from_now=3, venue="Test Venue", category="concert", url=None):
    start = now_utc() + timedelta(days=days_from_now)
    return {
        "title": title,
        "start_at": start.isoformat(),
        "venue": venue,
        "category": category,
        "price_range": "$10",
        "url": url,
        "pitch": "fun",
        "source": "test",
    }


def test_valid_candidates_are_inserted(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    candidates = [_candidate(f"Show {i}") for i in range(5)]
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)

    result = research_job_mod.run_research_job(repo)

    assert result["inserted"] == 5
    assert len(repo.events) == 5


def test_bad_rows_are_rejected(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    too_far = _candidate("Far Future Show", days_from_now=100)
    no_title = _candidate("")
    bad_date = {**_candidate("Bad Date"), "start_at": "not-a-date"}
    good = _candidate("Good Show")

    monkeypatch.setattr(
        research_job_mod, "run_research_call", lambda ctx, pref, **kw: [too_far, no_title, bad_date, good]
    )

    result = research_job_mod.run_research_job(repo)

    assert result["inserted"] == 1
    assert result["rejected"] == 3
    assert list(repo.events.values())[0].title == "Good Show"


def test_duplicate_within_run_is_rejected(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    dup = _candidate("Same Show", days_from_now=5, venue="Same Venue")
    dup2 = _candidate("Same Show", days_from_now=5, venue="Same Venue")
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: [dup, dup2])

    result = research_job_mod.run_research_job(repo)
    assert result["inserted"] == 1


def test_duplicate_of_existing_event_is_rejected(monkeypatch):
    repo = FakeRepository()
    existing_candidate = _candidate("Existing Show", days_from_now=5, venue="V")
    from app.research.validation import validate_candidate
    from app.utils.time import now_utc as _now

    window_start = _now()
    window_end = window_start + timedelta(days=21)
    result = validate_candidate(repo, existing_candidate, window_start, window_end, set())
    repo.insert_event(result.event)

    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(
        research_job_mod, "run_research_call", lambda ctx, pref, **kw: [existing_candidate]
    )

    job_result = research_job_mod.run_research_job(repo)
    assert job_result["inserted"] == 0
    assert len(repo.events) == 1  # still just the pre-seeded one


def test_more_than_cap_triggers_ranking_call(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    candidates = [_candidate(f"Show {i}", venue=f"Venue {i}") for i in range(15)]
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)

    rank_calls = []

    def fake_rank(candidate_summaries, preference_summary, cap):
        rank_calls.append((len(candidate_summaries), cap))
        return [c["event_key"] for c in candidate_summaries[:cap]]

    monkeypatch.setattr(research_job_mod, "rank_and_select", fake_rank)

    result = research_job_mod.run_research_job(repo)

    assert rank_calls == [(15, research_job_mod.EVENT_CAP)]
    assert result["inserted"] == research_job_mod.EVENT_CAP


def test_at_or_under_cap_skips_ranking_call(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    candidates = [_candidate(f"Show {i}", venue=f"Venue {i}") for i in range(research_job_mod.EVENT_CAP)]
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)

    def fail_rank(*a, **k):
        raise AssertionError("rank_and_select should not be called at/under the cap")

    monkeypatch.setattr(research_job_mod, "rank_and_select", fail_rank)

    result = research_job_mod.run_research_job(repo)
    assert result["inserted"] == research_job_mod.EVENT_CAP


def test_enrichment_tags_flow_through_to_inserted_events(monkeypatch):
    """Curation layer Phase 2: enrich_events runs before ranking/storage, so
    inserted events carry tags, and run_research_job reports tag_coverage."""
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    candidates = [_candidate("Show 1"), _candidate("Show 2", venue="Other Venue")]
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)

    def fake_enrich(events, settings):
        for i, e in enumerate(events):
            e.tags = ["music/folk"] if i == 0 else []  # only tag the first

    monkeypatch.setattr(research_job_mod, "enrich_events", fake_enrich)

    result = research_job_mod.run_research_job(repo)

    assert result["inserted"] == 2
    assert result["tagged"] == 1
    assert result["tag_coverage"] == 0.5
    tagged_titles = {e.title for e in repo.events.values() if e.tags}
    assert tagged_titles == {"Show 1"}

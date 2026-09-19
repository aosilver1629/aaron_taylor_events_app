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


@pytest.fixture(autouse=True)
def _reset_last_run():
    """_last_run is module-level global state (backs GET /ops/research/
    last-run) — reset it around every test so tests don't leak state into
    each other regardless of run order."""
    research_job_mod._last_run = None
    yield
    research_job_mod._last_run = None


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


def test_empty_profiles_leave_selection_unchanged(monkeypatch):
    """Curation layer Phase 3 regression guard: with both taste profiles
    empty (FakeRepository's default), matching.select_with_matching must
    not run — behavior stays byte-identical to the pre-Phase-3 cap logic."""
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    candidates = [_candidate(f"Show {i}", venue=f"Venue {i}") for i in range(research_job_mod.EVENT_CAP)]
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)

    def fail_matching(*a, **k):
        raise AssertionError("select_with_matching should not run when both profiles are empty")

    monkeypatch.setattr(research_job_mod, "select_with_matching", fail_matching)

    result = research_job_mod.run_research_job(repo)
    assert result["inserted"] == research_job_mod.EVENT_CAP


def test_hard_exclude_holds_even_at_or_under_cap(monkeypatch):
    """The one product invariant: a hard exclude is a genuine constraint,
    not just a ranking signal, so matching must run (and filter) even when
    the candidate set is at/under EVENT_CAP — the legacy rank_and_select
    path only fires over the cap and would never apply this filter."""
    repo = FakeRepository()
    repo.taste_profiles["aaron"] = {
        "person": "aaron", "hard_excludes": ["music/rock"],
        "include_tags": [], "include_entities": [], "exemplars": [],
    }
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    candidates = [_candidate("Rock Show"), _candidate("Folk Show", venue="Other Venue")]
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)

    def fake_enrich(events, settings):
        for e in events:
            e.tags = ["music/rock"] if e.title == "Rock Show" else ["music/folk"]

    monkeypatch.setattr(research_job_mod, "enrich_events", fake_enrich)

    def fail_rank(*a, **k):
        raise AssertionError("rank_and_select should not run when a profile is set")

    monkeypatch.setattr(research_job_mod, "rank_and_select", fail_rank)

    result = research_job_mod.run_research_job(repo)

    assert result["inserted"] == 1
    assert list(repo.events.values())[0].title == "Folk Show"


def test_match_reasons_copied_onto_inserted_events(monkeypatch):
    repo = FakeRepository()
    repo.taste_profiles["aaron"] = {
        "person": "aaron", "hard_excludes": [], "include_tags": ["music/folk"],
        "include_entities": [], "exemplars": [],
    }
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])

    candidates = [_candidate("Folk Show")]
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)

    def fake_enrich(events, settings):
        for e in events:
            e.tags = ["music/folk"]

    monkeypatch.setattr(research_job_mod, "enrich_events", fake_enrich)

    result = research_job_mod.run_research_job(repo)

    assert result["inserted"] == 1
    inserted = result["inserted_events"][0]
    assert inserted.match_reasons == ["declared:music/folk"]


def test_get_last_run_is_none_before_any_run():
    assert research_job_mod.get_last_run() is None


def test_run_records_last_run_state(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: [_candidate("Show 1")])

    research_job_mod.run_research_job(repo, triggered_by="manual")

    last_run = research_job_mod.get_last_run()
    assert last_run is not None
    assert last_run["triggered_by"] == "manual"
    assert last_run["inserted"] == 1
    assert last_run["inserted_titles"] == ["Show 1"]
    assert last_run["ballot_sent"] is False
    assert last_run["ballot_people_notified"] == 0


def test_run_defaults_triggered_by_to_scheduler(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: [])

    research_job_mod.run_research_job(repo)

    assert research_job_mod.get_last_run()["triggered_by"] == "scheduler"


def test_mark_ballot_sent_updates_last_run(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: [_candidate("Show 1")])

    research_job_mod.run_research_job(repo)
    research_job_mod.mark_ballot_sent(True, people=2)

    last_run = research_job_mod.get_last_run()
    assert last_run["ballot_sent"] is True
    assert last_run["ballot_people_notified"] == 2


def test_mark_ballot_sent_is_noop_when_no_run_yet():
    research_job_mod.mark_ballot_sent(True, people=2)
    assert research_job_mod.get_last_run() is None


def test_persist_false_does_not_write_to_events(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: [_candidate("Show 1")])

    result = research_job_mod.run_research_job(repo, persist=False)

    assert result["persisted"] is False
    assert result["inserted"] == 1
    assert result["inserted_events"][0].title == "Show 1"
    assert repo.events == {}


def test_persist_false_still_records_last_run(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: [_candidate("Show 1")])

    research_job_mod.run_research_job(repo, persist=False, triggered_by="manual")

    last_run = research_job_mod.get_last_run()
    assert last_run["persisted"] is False
    assert last_run["inserted"] == 1
    assert last_run["inserted_titles"] == ["Show 1"]
    assert repo.events == {}


def test_persist_true_is_still_the_default(monkeypatch):
    """The scheduler's automatic path never passes persist explicitly —
    it must keep inserting for real."""
    repo = FakeRepository()
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: [_candidate("Show 1")])

    result = research_job_mod.run_research_job(repo)

    assert result["persisted"] is True
    assert len(repo.events) == 1

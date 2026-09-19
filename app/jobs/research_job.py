"""Job 1 — Research. Runs weekly, Sunday 08:00 America/Los_Angeles (wired up
in app.scheduler). Pulls Ticketmaster + Bandsintown deterministically, pulls
a curated list of SF sources (see app.research.deterministic_search — regex-
parsed where possible, Claude-extracted otherwise), validates every
candidate, and caps the final list at 12.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from app.config import Settings, get_settings
from app.db import Repository
from app.logging_config import log_job_run
from app.research.bandsintown import fetch_bandsintown_events
from app.research.claude_research import rank_and_select
from app.research.deterministic_search import run_deterministic_research_call as run_research_call
from app.research.enrichment import enrich_events
from app.research.matching import select_with_matching
from app.research.preferences import build_preference_summary, build_preference_weights
from app.research.source_registry import load_sources, record_source_run
from app.research.ticketmaster import fetch_ticketmaster_events
from app.research.validation import validate_candidate
from app.utils.time import now_utc

logger = logging.getLogger("research_job")

WINDOW_DAYS = 21
EVENT_CAP = 12

# In-memory only, deliberately — this backs GET /ops/research/last-run for a
# lightweight "is research working" check, not a durable audit log (that's
# what Railway's structured logs via log_job_run are for). Resets on process
# restart/redeploy; the scheduler and API server share this process, so it
# always reflects whichever run — cron or manual via POST /ops/research/run
# — actually happened most recently.
_last_run: dict | None = None


def get_last_run() -> dict | None:
    return _last_run


def mark_ballot_sent(sent: bool, people: int = 0) -> None:
    """Called by whoever sends the ballot after a research run (the
    scheduler's research_and_ballots, or the manual endpoint when
    send_ballot=true) — run_research_job itself never sends a ballot, so it
    can't know this on its own."""
    if _last_run is not None:
        _last_run["ballot_sent"] = sent
        _last_run["ballot_people_notified"] = people


def _structured_context_text(structured: list[dict]) -> str:
    if not structured:
        return ""
    # A compact, deterministic JSON blob — cheap in tokens and unambiguous
    # for the model to read back.
    return json.dumps(structured, indent=None, default=str)


def run_research_job(
    repo: Repository, settings: Settings | None = None, triggered_by: str = "scheduler"
) -> dict:
    settings = settings or get_settings()
    window_start = now_utc()
    window_end = window_start + timedelta(days=WINDOW_DAYS)

    ticketmaster_events = fetch_ticketmaster_events(settings, window_days=WINDOW_DAYS)
    bandsintown_events = fetch_bandsintown_events(settings)
    structured = ticketmaster_events + bandsintown_events

    preference_summary = build_preference_summary(repo)

    sources = load_sources(repo)
    raw_candidates = run_research_call(
        _structured_context_text(structured),
        preference_summary,
        sources=sources,
        record_run=lambda source, fm, em, c, b: record_source_run(repo, source, fm, em, c, b),
    )

    seen_keys: set[str] = set()
    validated = []
    rejected = []
    for raw in raw_candidates:
        result = validate_candidate(repo, raw, window_start, window_end, seen_keys)
        if result.ok and result.event is not None:
            validated.append(result.event)
            seen_keys.add(result.event.event_key)
        else:
            rejected.append({"title": raw.get("title"), "reason": result.reason})

    if rejected:
        logger.info("research_candidates_rejected", extra={"job_fields": {"rejected": rejected}})

    # Enrichment before ranking/storage — Phase 3's matching needs tags to
    # score candidates, and inserts need them persisted. One batched call
    # for the whole run; never blocks or shrinks it on failure (see
    # app.research.enrichment).
    enrich_events(validated, settings)
    tagged = sum(1 for e in validated if e.tags)
    tag_coverage = tagged / len(validated) if validated else 0.0

    profiles = {"aaron": repo.get_taste_profile("aaron"), "tay": repo.get_taste_profile("tay")}
    any_profile_set = any(
        profile.get("hard_excludes") or profile.get("include_tags")
        or profile.get("include_entities") or profile.get("exemplars")
        for profile in profiles.values()
    )

    if any_profile_set:
        # A hard exclude is a genuine constraint, not just a ranking signal
        # — it must apply even to a candidate set at or under EVENT_CAP, so
        # this branch runs regardless of len(validated) (unlike the legacy
        # rank_and_select path below, which only fires over the cap).
        weights = build_preference_weights(repo)
        final_events = select_with_matching(validated, profiles, weights, EVENT_CAP)
    elif len(validated) > EVENT_CAP:
        candidate_summaries = [
            {
                "event_key": e.event_key,
                "title": e.title,
                "category": e.category,
                "venue": e.venue,
            }
            for e in validated
        ]
        chosen_keys = rank_and_select(candidate_summaries, preference_summary, EVENT_CAP)
        by_key = {e.event_key: e for e in validated}
        final_events = [by_key[k] for k in chosen_keys if k in by_key]
        # Ranking call is best-effort; if it returned fewer than the cap
        # (e.g. partial validity), top up with the remaining validated
        # events in their original order rather than under-filling the week.
        if len(final_events) < min(EVENT_CAP, len(validated)):
            already = {e.event_key for e in final_events}
            for e in validated:
                if len(final_events) >= EVENT_CAP:
                    break
                if e.event_key not in already:
                    final_events.append(e)
    else:
        final_events = validated

    inserted = []
    for event_in in final_events:
        try:
            inserted_event = repo.insert_event(event_in)
            # match_reasons is in-memory only (not a DB column — see
            # models.EventIn.match_reasons) — insert_event's row doesn't
            # carry it, so copy it onto the object insert_event returns.
            inserted_event.match_reasons = event_in.match_reasons
            inserted.append(inserted_event)
        except Exception:
            logger.exception(
                "event_insert_failed", extra={"job_fields": {"event_key": event_in.event_key}}
            )

    log_job_run(
        logger,
        "research",
        triggered_by=triggered_by,
        ticketmaster_found=len(ticketmaster_events),
        bandsintown_found=len(bandsintown_events),
        claude_candidates=len(raw_candidates),
        validated=len(validated),
        rejected=len(rejected),
        inserted=len(inserted),
        tagged=tagged,
        tag_coverage=round(tag_coverage, 2),
        dry_run=settings.dry_run,
    )

    global _last_run
    _last_run = {
        "ran_at": now_utc().isoformat(),
        "triggered_by": triggered_by,
        "ticketmaster_found": len(ticketmaster_events),
        "bandsintown_found": len(bandsintown_events),
        "claude_candidates": len(raw_candidates),
        "validated": len(validated),
        "rejected": len(rejected),
        "inserted": len(inserted),
        "inserted_titles": [e.title for e in inserted],
        "tagged": tagged,
        "tag_coverage": round(tag_coverage, 2),
        "ballot_sent": False,
        "ballot_people_notified": 0,
    }

    return {
        "ticketmaster_found": len(ticketmaster_events),
        "bandsintown_found": len(bandsintown_events),
        "claude_candidates": len(raw_candidates),
        "validated": len(validated),
        "rejected": len(rejected),
        "inserted": len(inserted),
        "inserted_events": inserted,
        "tagged": tagged,
        "tag_coverage": tag_coverage,
    }

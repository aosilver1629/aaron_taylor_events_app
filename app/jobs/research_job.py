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
from app.research.preferences import build_preference_summary
from app.research.ticketmaster import fetch_ticketmaster_events
from app.research.validation import validate_candidate
from app.utils.time import now_utc

logger = logging.getLogger("research_job")

WINDOW_DAYS = 21
EVENT_CAP = 12


def _structured_context_text(structured: list[dict]) -> str:
    if not structured:
        return ""
    # A compact, deterministic JSON blob — cheap in tokens and unambiguous
    # for the model to read back.
    return json.dumps(structured, indent=None, default=str)


def run_research_job(repo: Repository, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    window_start = now_utc()
    window_end = window_start + timedelta(days=WINDOW_DAYS)

    ticketmaster_events = fetch_ticketmaster_events(settings, window_days=WINDOW_DAYS)
    bandsintown_events = fetch_bandsintown_events(settings)
    structured = ticketmaster_events + bandsintown_events

    preference_summary = build_preference_summary(repo)

    raw_candidates = run_research_call(_structured_context_text(structured), preference_summary)

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

    if len(validated) > EVENT_CAP:
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
            inserted.append(repo.insert_event(event_in))
        except Exception:
            logger.exception(
                "event_insert_failed", extra={"job_fields": {"event_key": event_in.event_key}}
            )

    log_job_run(
        logger,
        "research",
        ticketmaster_found=len(ticketmaster_events),
        bandsintown_found=len(bandsintown_events),
        claude_candidates=len(raw_candidates),
        validated=len(validated),
        rejected=len(rejected),
        inserted=len(inserted),
        dry_run=settings.dry_run,
    )
    return {
        "ticketmaster_found": len(ticketmaster_events),
        "bandsintown_found": len(bandsintown_events),
        "claude_candidates": len(raw_candidates),
        "validated": len(validated),
        "rejected": len(rejected),
        "inserted": len(inserted),
        "inserted_events": inserted,
    }

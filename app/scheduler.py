"""Wires all four jobs to their schedules (all times Pacific). Started from
app.main's FastAPI lifespan so `uvicorn app.main:app` runs the API and the
scheduler together — this is a single-process Railway service, not a
separate worker.

The research+ballots job originally ran weekly (Sunday 08:00, per spec) on
a CronTrigger. Overridden on request to run every other morning at 09:00
Pacific, starting 2026-09-03 — a fixed calendar date rather than "N days
from whenever this deploys," since that's literally what was asked for.
IntervalTrigger(days=2, start_date=...) is the right primitive for "every
other day" — cron expressions can't express an every-N-days cadence
anchored to an arbitrary start date.
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.calendar_client.google_calendar import run_calendar_job
from app.config import get_settings
from app.db import Repository
from app.jobs.ballot_job import run_ballot_send_job
from app.jobs.expiry_job import run_expiry_job
from app.jobs.research_job import run_research_job
from app.utils.time import PACIFIC

logger = logging.getLogger("scheduler")

RESEARCH_AND_BALLOTS_START = datetime(2026, 9, 3, 9, 0, tzinfo=PACIFIC)


def _run_safely(name: str, fn) -> None:
    try:
        fn()
    except Exception:
        logger.exception("job_crashed", extra={"job_fields": {"job": name}})


def research_and_ballots() -> None:
    """Job 1 immediately followed by Job 2 — ballots go out for whatever
    research just inserted, per spec ("After research inserts events,
    send...").
    """
    repo = Repository()
    settings = get_settings()
    research_result = run_research_job(repo, settings)
    events = research_result.get("inserted_events", [])
    if not events:
        logger.info("no_new_events_skipping_ballot_send")
        return
    run_ballot_send_job(repo, events, settings)


def calendar_write_tick() -> None:
    repo = Repository()
    run_calendar_job(repo)


def expiry_tick() -> None:
    repo = Repository()
    run_expiry_job(repo)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=PACIFIC)

    scheduler.add_job(
        lambda: _run_safely("research_and_ballots", research_and_ballots),
        IntervalTrigger(days=2, start_date=RESEARCH_AND_BALLOTS_START, timezone=PACIFIC),
        id="research_and_ballots",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_safely("calendar_write", calendar_write_tick),
        CronTrigger(minute="*/15", timezone=PACIFIC),
        id="calendar_write",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_safely("ballot_expiry", expiry_tick),
        CronTrigger(minute=0, timezone=PACIFIC),
        id="ballot_expiry",
        max_instances=1,
        coalesce=True,
    )
    return scheduler

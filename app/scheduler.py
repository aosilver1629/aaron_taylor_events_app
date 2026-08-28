"""Wires all four jobs to their cron schedules (all times Pacific, per
spec). Started from app.main's FastAPI lifespan so `uvicorn app.main:app`
runs the API and the scheduler together — this is a single-process
Railway service, not a separate worker.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.calendar_client.google_calendar import run_calendar_job
from app.config import get_settings
from app.db import Repository
from app.jobs.ballot_job import run_ballot_send_job
from app.jobs.expiry_job import run_expiry_job
from app.jobs.research_job import run_research_job
from app.utils.time import PACIFIC

logger = logging.getLogger("scheduler")


def _run_safely(name: str, fn) -> None:
    try:
        fn()
    except Exception:
        logger.exception("job_crashed", extra={"job_fields": {"job": name}})


def weekly_research_and_ballots() -> None:
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
        lambda: _run_safely("weekly_research_and_ballots", weekly_research_and_ballots),
        CronTrigger(day_of_week="sun", hour=8, minute=0, timezone=PACIFIC),
        id="weekly_research_and_ballots",
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

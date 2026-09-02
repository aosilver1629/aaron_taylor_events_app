"""Verifies the schedule (all times Pacific): research+ballots every other
morning at 09:00 starting 2026-09-03 (overridden from the original weekly
Sunday 08:00 spec on request), calendar write every 15 minutes, hourly
ballot expiry. Doesn't start the scheduler (no real Supabase in this
environment) — just inspects the configured jobs.
"""
from __future__ import annotations

from app.scheduler import RESEARCH_AND_BALLOTS_START, build_scheduler
from app.utils.time import PACIFIC


def test_jobs_registered_with_correct_ids():
    scheduler = build_scheduler()
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"research_and_ballots", "calendar_write", "ballot_expiry"}


def test_research_and_ballots_job_runs_every_other_day_at_9am_pacific():
    scheduler = build_scheduler()
    job = scheduler.get_job("research_and_ballots")
    trigger = job.trigger
    assert trigger.interval.days == 2
    assert trigger.start_date == RESEARCH_AND_BALLOTS_START
    assert trigger.timezone == PACIFIC


def test_calendar_job_runs_every_15_minutes():
    scheduler = build_scheduler()
    job = scheduler.get_job("calendar_write")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["minute"] == "*/15"


def test_expiry_job_runs_hourly():
    scheduler = build_scheduler()
    job = scheduler.get_job("ballot_expiry")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["minute"] == "0"
    assert fields["hour"] == "*"

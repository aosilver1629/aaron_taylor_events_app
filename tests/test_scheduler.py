"""Verifies the cron schedule matches spec (all times Pacific): weekly
Sunday 08:00 research+ballots, calendar write every 15 minutes, hourly
ballot expiry. Doesn't start the scheduler (no real Supabase in this
environment) — just inspects the configured jobs.
"""
from __future__ import annotations

from app.scheduler import build_scheduler
from app.utils.time import PACIFIC


def test_jobs_registered_with_correct_ids():
    scheduler = build_scheduler()
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"weekly_research_and_ballots", "calendar_write", "ballot_expiry"}


def test_weekly_job_is_sunday_8am_pacific():
    scheduler = build_scheduler()
    job = scheduler.get_job("weekly_research_and_ballots")
    trigger = job.trigger
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["day_of_week"] == "sun"
    assert fields["hour"] == "8"
    assert fields["minute"] == "0"
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

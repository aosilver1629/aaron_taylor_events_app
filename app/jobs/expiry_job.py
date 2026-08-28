"""Hourly job: any ballot sent more than 48 hours ago with no response
becomes 'expired'.
"""
from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.db import Repository
from app.logging_config import log_job_run

logger = logging.getLogger("expiry_job")


def run_expiry_job(repo: Repository, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    expired = repo.expire_stale_ballots(older_than_hours=48)
    log_job_run(logger, "ballot_expiry", expired=expired)
    return {"expired": expired}

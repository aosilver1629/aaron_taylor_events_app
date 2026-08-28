"""Retry wrapper for external calls: retry twice with backoff, then log and
move on. Per spec, a failed external call must never roll back unrelated
work (e.g. a failed Twilio send must not undo a research insert), so this
never raises past the caller unless `reraise=True` is passed explicitly.
"""
from __future__ import annotations

import logging
from typing import Callable, TypeVar

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("external_call")

T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    what: str,
    reraise: bool = False,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T | None:
    """Call `fn`, retrying twice (3 attempts total) with exponential backoff.

    On final failure: log and return None, unless `reraise` is True (used
    only where the caller has no sane fallback, e.g. nothing to persist).
    """

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(exceptions),
        reraise=True,
    )
    def _call() -> T:
        return fn()

    try:
        return _call()
    except Exception:
        logger.exception("external_call_failed", extra={"job_fields": {"what": what}})
        if reraise:
            raise
        return None

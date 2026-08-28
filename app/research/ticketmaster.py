"""Deterministic structured pull #1: Ticketmaster Discovery API. Results are
fed to the Claude research call as context ("do not re-find them"), not
inserted directly — the research call is what produces the final candidate
list so everything goes through the same validation path.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from app.config import Settings
from app.utils.retry import with_retry
from app.utils.time import now_utc

logger = logging.getLogger("ticketmaster")

DISCOVERY_URL = "https://app.ticketmaster.com/discovery/v2/events.json"


def fetch_ticketmaster_events(settings: Settings, window_days: int = 21) -> list[dict]:
    if not settings.ticketmaster_api_key:
        logger.info("ticketmaster_skipped_no_key")
        return []

    start = now_utc()
    end = start + timedelta(days=window_days)
    params = {
        "apikey": settings.ticketmaster_api_key,
        "city": "San Francisco",
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sort": "date,asc",
        "size": 50,
    }

    def _do_fetch():
        with httpx.Client(timeout=15) as client:
            resp = client.get(DISCOVERY_URL, params=params)
            resp.raise_for_status()
            return resp.json()

    data = with_retry(_do_fetch, what="ticketmaster_fetch")
    if not data:
        return []

    events = data.get("_embedded", {}).get("events", [])
    results = []
    for e in events:
        try:
            results.append(_normalize(e))
        except Exception:
            logger.warning("ticketmaster_normalize_failed", extra={"job_fields": {"id": e.get("id")}})
    return results


def _normalize(e: dict) -> dict:
    dates = e.get("dates", {}).get("start", {})
    start_at = dates.get("dateTime")
    if not start_at:
        # No confirmed time — skip rather than guess.
        raise ValueError("no dateTime")
    venue = None
    venues = e.get("_embedded", {}).get("venues", [])
    if venues:
        venue = venues[0].get("name")

    price_range = None
    price_ranges = e.get("priceRanges")
    if price_ranges:
        pr = price_ranges[0]
        price_range = f"${pr.get('min', '?')}-${pr.get('max', '?')}"

    category = None
    classifications = e.get("classifications", [])
    if classifications:
        segment = classifications[0].get("segment", {}).get("name", "").lower()
        category = _map_category(segment)

    return {
        "title": e.get("name"),
        "start_at": start_at,
        "venue": venue,
        "url": e.get("url"),
        "price_range": price_range,
        "category": category,
        "source": "ticketmaster",
    }


def _map_category(segment: str) -> str:
    mapping = {
        "music": "concert",
        "arts & theatre": "art",
        "film": "art",
        "sports": "outdoor",
    }
    return mapping.get(segment, "other")

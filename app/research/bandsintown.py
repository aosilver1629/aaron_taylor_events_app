"""Deterministic structured pull #2: Bandsintown, for a configured list of
favorite artists. Spec allows an empty artist list at v1 — this returns []
immediately in that case rather than calling the API at all.
"""
from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.utils.retry import with_retry

logger = logging.getLogger("bandsintown")

BASE_URL = "https://rest.bandsintown.com/artists/{artist}/events"


def fetch_bandsintown_events(settings: Settings) -> list[dict]:
    artists = settings.bandsintown_artist_list
    if not artists or not settings.bandsintown_app_id:
        return []

    results = []
    for artist in artists:
        events = _fetch_for_artist(settings, artist)
        results.extend(events)
    return results


def _fetch_for_artist(settings: Settings, artist: str) -> list[dict]:
    params = {"app_id": settings.bandsintown_app_id, "date": "upcoming"}

    def _do_fetch():
        with httpx.Client(timeout=15) as client:
            resp = client.get(BASE_URL.format(artist=artist), params=params)
            resp.raise_for_status()
            return resp.json()

    data = with_retry(_do_fetch, what=f"bandsintown_fetch:{artist}")
    if not data:
        return []

    results = []
    for e in data:
        try:
            results.append(_normalize(e, artist))
        except Exception:
            logger.warning(
                "bandsintown_normalize_failed",
                extra={"job_fields": {"artist": artist, "id": e.get("id")}},
            )
    return results


def _normalize(e: dict, artist: str) -> dict:
    venue = e.get("venue", {})
    if (venue.get("city") or "").strip().lower() not in ("san francisco", "sf"):
        # Bandsintown returns all of an artist's tour dates; keep only SF.
        raise ValueError("not SF")
    return {
        "title": f"{artist} at {venue.get('name', 'TBA')}",
        "start_at": e.get("datetime"),
        "venue": venue.get("name"),
        "url": e.get("url"),
        "price_range": None,
        "category": "concert",
        "source": "bandsintown",
    }

"""Supabase-backed repository. All state lives in Postgres via Supabase's
PostgREST API (the `supabase` client), per spec. This module is the only
place that talks to the `events` / `ballots` / `sms_log` tables — job code
calls Repository methods, never the raw client.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from uuid import UUID, uuid4

from supabase import Client, create_client

from app.config import get_settings
from app.models import Ballot, Event, EventIn
from app.utils.time import now_utc

logger = logging.getLogger("db")


@lru_cache
def get_client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY are not set. "
            "The app cannot read or write state without them."
        )
    return create_client(settings.supabase_url, settings.supabase_service_key)


class Repository:
    """Thin wrapper over the Supabase client, one method per query the app
    actually needs. Kept free of business logic (ranking, validation,
    numbering) so job code stays testable against `FakeRepository`.
    """

    def __init__(self, client: Client | None = None):
        self._client = client or get_client()

    # ---- events -----------------------------------------------------

    def event_key_exists(self, event_key: str) -> bool:
        res = (
            self._client.table("events")
            .select("id")
            .eq("event_key", event_key)
            .limit(1)
            .execute()
        )
        return len(res.data) > 0

    def insert_event(self, event: EventIn) -> Event:
        row = {
            "event_key": event.event_key,
            "title": event.title,
            "start_at": event.start_at.isoformat(),
            "end_at": event.end_at.isoformat() if event.end_at else None,
            "venue": event.venue,
            "neighborhood": event.neighborhood,
            "category": event.category,
            "price_range": event.price_range,
            "url": event.url,
            "pitch": event.pitch,
            "source": event.source,
        }
        res = self._client.table("events").insert(row).execute()
        return Event.from_row(res.data[0])

    def get_event(self, event_id: UUID) -> Event | None:
        res = (
            self._client.table("events")
            .select("*")
            .eq("id", str(event_id))
            .limit(1)
            .execute()
        )
        return Event.from_row(res.data[0]) if res.data else None

    def set_calendar_event_id(self, event_id: UUID, calendar_event_id: str) -> None:
        self._client.table("events").update(
            {"calendar_event_id": calendar_event_id}
        ).eq("id", str(event_id)).execute()

    def get_events_awaiting_calendar_write(self) -> list[Event]:
        """Events with no calendar_event_id yet where BOTH people said yes.

        PostgREST can't express the "both yes" join/aggregate directly, so
        this fetches the small candidate set (events not yet on the
        calendar) and cross-references ballots in Python — cheap since the
        job runs every 15 minutes against a handful of open events.
        """
        events_res = (
            self._client.table("events")
            .select("*")
            .is_("calendar_event_id", "null")
            .execute()
        )
        if not events_res.data:
            return []
        event_ids = [row["id"] for row in events_res.data]
        ballots_res = (
            self._client.table("ballots")
            .select("event_id,person,response")
            .in_("event_id", event_ids)
            .eq("response", "yes")
            .execute()
        )
        yes_people_by_event: dict[str, set[str]] = {}
        for row in ballots_res.data:
            yes_people_by_event.setdefault(row["event_id"], set()).add(row["person"])

        ready_ids = {
            eid
            for eid, people in yes_people_by_event.items()
            if {"aaron", "tay"}.issubset(people)
        }
        return [Event.from_row(row) for row in events_res.data if row["id"] in ready_ids]

    # ---- ballots ------------------------------------------------------

    def create_ballot_batch(
        self, person: str, batch_id: UUID, entries: list[tuple[UUID, int]]
    ) -> None:
        """entries: list of (event_id, list_number)."""
        rows = [
            {
                "event_id": str(event_id),
                "person": person,
                "batch_id": str(batch_id),
                "list_number": list_number,
                "response": None,
            }
            for event_id, list_number in entries
        ]
        if rows:
            self._client.table("ballots").insert(rows).execute()

    def get_latest_batch_id(self, person: str) -> UUID | None:
        res = (
            self._client.table("ballots")
            .select("batch_id,sent_at")
            .eq("person", person)
            .order("sent_at", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        return UUID(res.data[0]["batch_id"])

    def get_ballot(self, batch_id: UUID, person: str, list_number: int) -> Ballot | None:
        res = (
            self._client.table("ballots")
            .select("*")
            .eq("batch_id", str(batch_id))
            .eq("person", person)
            .eq("list_number", list_number)
            .limit(1)
            .execute()
        )
        return Ballot.from_row(res.data[0]) if res.data else None

    def get_ballots_for_batch(self, batch_id: UUID, person: str) -> list[Ballot]:
        res = (
            self._client.table("ballots")
            .select("*")
            .eq("batch_id", str(batch_id))
            .eq("person", person)
            .execute()
        )
        return [Ballot.from_row(row) for row in res.data]

    def resolve_ballot(self, ballot_id: UUID, response: str) -> None:
        self._client.table("ballots").update(
            {"response": response, "responded_at": now_utc().isoformat()}
        ).eq("id", str(ballot_id)).execute()

    def expire_stale_ballots(self, older_than_hours: int = 48) -> int:
        cutoff = now_utc() - timedelta(hours=older_than_hours)
        res = (
            self._client.table("ballots")
            .select("id")
            .is_("response", "null")
            .lt("sent_at", cutoff.isoformat())
            .execute()
        )
        ids = [row["id"] for row in res.data]
        if ids:
            self._client.table("ballots").update({"response": "expired"}).in_(
                "id", ids
            ).execute()
        return len(ids)

    def get_recent_resolved_ballots(self, limit: int = 50) -> list[dict]:
        """For the preference-context summary: recent yes/no ballots joined
        with their event's category/venue.
        """
        res = (
            self._client.table("ballots")
            .select("response,event_id,person,events(category,venue)")
            .in_("response", ["yes", "no"])
            .order("responded_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    # ---- sms_log --------------------------------------------------------

    def log_sms(self, person: str | None, direction: str, body: str) -> None:
        self._client.table("sms_log").insert(
            {"person": person, "direction": direction, "body": body}
        ).execute()


def new_uuid() -> UUID:
    return uuid4()

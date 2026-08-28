"""Config loading. All values come from the environment (Railway secrets in
prod, a local .env for development). See .env.example for the full key list.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Safety switches
    dry_run: bool = True
    sms_provider: str = "mock"  # "mock" | "twilio"

    # Anthropic
    anthropic_api_key: str = ""

    # Supabase
    supabase_url: str = ""
    supabase_service_key: str = ""

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Participants (hardcoded to exactly two people, per spec)
    aaron_phone: str = ""
    tay_phone: str = ""

    # Google Calendar
    google_service_account_json: str = ""  # raw JSON string (Railway secret)
    calendar_id: str = ""

    # Event sources
    ticketmaster_api_key: str = ""
    bandsintown_app_id: str = ""
    # Not in the spec's required key list (spec says an empty list is fine at
    # v1), but wired up so favorite artists can be added later without a code
    # change. Comma-separated artist names.
    bandsintown_artists: str = ""

    @property
    def bandsintown_artist_list(self) -> list[str]:
        return [a.strip() for a in self.bandsintown_artists.split(",") if a.strip()]

    @property
    def people(self) -> dict[str, str]:
        """person key -> phone number, the fixed two-person roster."""
        return {"aaron": self.aaron_phone, "tay": self.tay_phone}


@lru_cache
def get_settings() -> Settings:
    return Settings()

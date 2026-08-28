"""FastAPI app tests: /health and /sms, with Repository and the SMS
provider monkeypatched to a FakeRepository/MockSMSProvider pair so no real
Supabase/Twilio credentials are needed. The scheduler is disabled via
ENABLE_SCHEDULER=false (set in conftest) so app startup doesn't try to hit
a real Supabase project.
"""
from __future__ import annotations

import os

os.environ.setdefault("ENABLE_SCHEDULER", "false")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.sms.provider import MockSMSProvider  # noqa: E402
from tests.fake_repo import FakeRepository  # noqa: E402


def _settings(**overrides) -> Settings:
    base = dict(
        dry_run=True,
        sms_provider="mock",
        aaron_phone="+15550001111",
        tay_phone="+15550002222",
        twilio_auth_token="test_token",
    )
    base.update(overrides)
    return Settings(**base)


def test_health():
    with TestClient(main_mod.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_sms_webhook_happy_path(monkeypatch):
    settings = _settings()
    repo = FakeRepository()
    provider = MockSMSProvider(repo, settings)

    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)
    monkeypatch.setattr(main_mod, "get_sms_provider", lambda: provider)

    with TestClient(main_mod.app) as client:
        resp = client.post(
            "/sms", data={"From": settings.aaron_phone, "Body": "help", "To": "+15559999999"}
        )

    assert resp.status_code == 200
    assert "<Response>" in resp.text
    assert any(row["direction"] == "out" for row in repo.sms_log)


def test_sms_webhook_rejects_unknown_number(monkeypatch):
    settings = _settings()
    repo = FakeRepository()
    provider = MockSMSProvider(repo, settings)

    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)
    monkeypatch.setattr(main_mod, "get_sms_provider", lambda: provider)

    with TestClient(main_mod.app) as client:
        resp = client.post("/sms", data={"From": "+19998887777", "Body": "1"})

    assert resp.status_code == 200
    assert repo.sms_log == []


def test_sms_webhook_rejects_bad_twilio_signature(monkeypatch):
    settings = _settings(sms_provider="twilio", dry_run=False)
    repo = FakeRepository()

    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)
    monkeypatch.setattr(main_mod, "verify_twilio_signature", lambda *a, **k: False)

    with TestClient(main_mod.app) as client:
        resp = client.post(
            "/sms",
            data={"From": settings.aaron_phone, "Body": "1"},
            headers={"X-Twilio-Signature": "bogus"},
        )

    assert resp.status_code == 403

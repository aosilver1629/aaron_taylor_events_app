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


def test_ops_sources_shape(monkeypatch):
    from uuid import uuid4

    repo = FakeRepository()
    source_id = uuid4()
    repo.sources[source_id] = {
        "id": source_id,
        "label": "Test Venue",
        "city": "sf",
        "url": "https://example.com",
        "kind": "venue",
        "preferred_tier": 1,
        "parser_id": None,
        "extraction_rules": None,
        "enabled": True,
        "status": "healthy",
        "status_reason": None,
        "consecutive_zero_runs": 0,
    }
    repo.insert_source_run(source_id, "http", "regex", 5, False)

    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.get("/ops/sources")

    assert resp.status_code == 200
    body = resp.json()
    assert "sources" in body
    assert len(body["sources"]) == 1
    entry = body["sources"][0]
    assert entry["label"] == "Test Venue"
    assert entry["tier_in_use"] == "http/regex"
    assert entry["status"] == "healthy"
    assert entry["recent_candidates"] == [5]


def test_get_profile_lazily_creates_empty_profile(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.get("/profile/aaron")

    assert resp.status_code == 200
    body = resp.json()
    assert body["person"] == "aaron"
    assert body["hard_excludes"] == []


def test_get_profile_unknown_person_404(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.get("/profile/nobody")

    assert resp.status_code == 404


def test_post_profile_saves_valid_fields(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post(
            "/profile/tay",
            json={
                "hard_excludes": ["music/rock"],
                "include_tags": ["music/folk"],
                "include_entities": ["Watchhouse"],
                "exemplars": ["outdoor food markets with live music"],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["hard_excludes"] == ["music/rock"]
    assert body["include_entities"] == ["Watchhouse"]
    assert repo.taste_profiles["tay"]["include_tags"] == ["music/folk"]


def test_post_profile_rejects_invalid_tag(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post("/profile/aaron", json={"hard_excludes": ["not/a/real/tag"]})

    assert resp.status_code == 400


def test_post_profile_rejects_too_many_exemplars(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post("/profile/aaron", json={"exemplars": [f"e{i}" for i in range(6)]})

    assert resp.status_code == 400


def test_post_profile_rejects_overlong_exemplar(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post("/profile/aaron", json={"exemplars": ["x" * 201]})

    assert resp.status_code == 400


def test_post_profile_unknown_person_404(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post("/profile/nobody", json={"hard_excludes": []})

    assert resp.status_code == 404


def test_profile_editor_serves_html():
    with TestClient(main_mod.app) as client:
        resp = client.get("/profile-editor/aaron")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Event taste profile" in resp.text


def test_profile_editor_unknown_person_404():
    with TestClient(main_mod.app) as client:
        resp = client.get("/profile-editor/nobody")

    assert resp.status_code == 404

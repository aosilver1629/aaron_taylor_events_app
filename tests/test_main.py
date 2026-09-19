"""FastAPI app tests: /health and /sms, with Repository and the SMS
provider monkeypatched to a FakeRepository/MockSMSProvider pair so no real
Supabase/Twilio credentials are needed. The scheduler is disabled via
ENABLE_SCHEDULER=false (set in conftest) so app startup doesn't try to hit
a real Supabase project.
"""
from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("ENABLE_SCHEDULER", "false")

from fastapi.testclient import TestClient  # noqa: E402

import app.jobs.research_job as research_job_mod  # noqa: E402
import app.main as main_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.sms.provider import MockSMSProvider  # noqa: E402
from app.utils.time import now_utc  # noqa: E402
from tests.fake_repo import FakeRepository  # noqa: E402


def _research_candidate(title: str) -> dict:
    return {
        "title": title,
        "start_at": (now_utc() + timedelta(days=3)).isoformat(),
        "venue": "Test Venue",
        "category": "concert",
        "price_range": "$10",
        "url": None,
        "pitch": "fun",
        "source": "test",
    }


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


def test_endpoints_doc_serves_html():
    with TestClient(main_mod.app) as client:
        resp = client.get("/endpoints")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Route Ledger" in resp.text
    assert "/ops/research/run" in resp.text


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


def test_ops_sources_includes_disabled(monkeypatch):
    from uuid import uuid4

    repo = FakeRepository()
    source_id = uuid4()
    repo.sources[source_id] = {
        "id": source_id, "label": "Paused Source", "city": "sf", "url": "https://example.com",
        "kind": "venue", "preferred_tier": 2, "parser_id": None, "extraction_rules": None,
        "enabled": False, "status": "healthy", "status_reason": None, "consecutive_zero_runs": 0,
    }
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.get("/ops/sources")

    body = resp.json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["enabled"] is False


def test_ops_sources_create(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post(
            "/ops/sources",
            json={"label": "New Venue", "url": "https://newvenue.example.com", "kind": "venue", "preferred_tier": 2},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["label"] == "New Venue"
    assert body["enabled"] is True
    assert len(repo.sources) == 1


def test_ops_sources_create_rejects_duplicate_label(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)
    payload = {"label": "New Venue", "url": "https://a.example.com", "kind": "venue", "preferred_tier": 2}

    with TestClient(main_mod.app) as client:
        first = client.post("/ops/sources", json=payload)
        second = client.post("/ops/sources", json=payload)

    assert first.status_code == 200
    assert second.status_code == 400


def test_ops_sources_create_rejects_invalid_kind(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post(
            "/ops/sources",
            json={"label": "X", "url": "https://x.example.com", "kind": "nightclub", "preferred_tier": 2},
        )

    assert resp.status_code == 400


def test_ops_sources_create_rejects_unknown_parser_id(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post(
            "/ops/sources",
            json={
                "label": "X", "url": "https://x.example.com", "kind": "venue",
                "preferred_tier": 1, "parser_id": "made_up_parser",
            },
        )

    assert resp.status_code == 400


def test_ops_sources_update_toggles_enabled(monkeypatch):
    from uuid import uuid4

    repo = FakeRepository()
    source_id = uuid4()
    repo.sources[source_id] = {
        "id": source_id, "label": "Test Venue", "city": "sf", "url": "https://example.com",
        "kind": "venue", "preferred_tier": 2, "parser_id": None, "extraction_rules": None,
        "enabled": True, "status": "healthy", "status_reason": None, "consecutive_zero_runs": 0,
    }
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.patch(f"/ops/sources/{source_id}", json={"enabled": False})

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert repo.sources[source_id]["enabled"] is False


def test_ops_sources_update_unknown_id_404(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.patch("/ops/sources/00000000-0000-0000-0000-000000000000", json={"enabled": False})

    assert resp.status_code == 404


def test_ops_sources_update_rejects_invalid_tier(monkeypatch):
    from uuid import uuid4

    repo = FakeRepository()
    source_id = uuid4()
    repo.sources[source_id] = {
        "id": source_id, "label": "Test Venue", "city": "sf", "url": "https://example.com",
        "kind": "venue", "preferred_tier": 2, "parser_id": None, "extraction_rules": None,
        "enabled": True, "status": "healthy", "status_reason": None, "consecutive_zero_runs": 0,
    }
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.patch(f"/ops/sources/{source_id}", json={"preferred_tier": 99})

    assert resp.status_code == 400


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


def test_admin_ui_serves_html():
    with TestClient(main_mod.app) as client:
        resp = client.get("/admin")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Run research preview" in resp.text


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


def _patch_research_pipeline(monkeypatch, candidates):
    monkeypatch.setattr(research_job_mod, "fetch_ticketmaster_events", lambda s, window_days=21: [])
    monkeypatch.setattr(research_job_mod, "fetch_bandsintown_events", lambda s: [])
    monkeypatch.setattr(research_job_mod, "run_research_call", lambda ctx, pref, **kw: candidates)
    monkeypatch.setattr(research_job_mod, "enrich_events", lambda events, settings: None)


def test_ops_research_run_never_inserts_or_sends_ballot(monkeypatch):
    research_job_mod._last_run = None
    repo = FakeRepository()
    _patch_research_pipeline(monkeypatch, [_research_candidate("Show 1")])
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    def fail_ballot(*a, **k):
        raise AssertionError("run_ballot_send_job should never run from a preview")

    monkeypatch.setattr(main_mod, "run_ballot_send_job", fail_ballot)

    with TestClient(main_mod.app) as client:
        resp = client.post("/ops/research/run")

    assert resp.status_code == 200
    body = resp.json()
    assert body["triggered_by"] == "manual"
    assert body["persisted"] is False
    assert body["inserted"] == 1
    assert body["inserted_titles"] == ["Show 1"]
    assert body["ballot_sent"] is False
    assert repo.sms_log == []
    assert repo.events == {}


def test_ops_events_approve_mints_and_sends_ballot(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    calls = []

    def fake_ballot_send(repo_arg, events, settings):
        calls.append([e.title for e in events])
        return {"events": len(events), "people": 2}

    monkeypatch.setattr(main_mod, "run_ballot_send_job", fake_ballot_send)

    with TestClient(main_mod.app) as client:
        resp = client.post(
            "/ops/events/approve",
            json={"events": [_research_candidate("Show 1")]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert calls == [["Show 1"]]
    assert body["inserted"] == 1
    assert body["inserted_titles"] == ["Show 1"]
    assert body["ballot_sent"] is True
    assert body["ballot_people_notified"] == 2
    assert len(repo.events) == 1


def test_ops_events_approve_rejects_empty_list(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        resp = client.post("/ops/events/approve", json={"events": []})

    assert resp.status_code == 400


def test_ops_events_approve_rejects_missing_title(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    bad = _research_candidate("Show 1")
    bad["title"] = ""

    with TestClient(main_mod.app) as client:
        resp = client.post("/ops/events/approve", json={"events": [bad]})

    assert resp.status_code == 400
    assert repo.events == {}


def test_ops_events_approve_skips_duplicate_event_key(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    def fake_ballot_send(repo_arg, events, settings):
        return {"events": len(events), "people": 2}

    monkeypatch.setattr(main_mod, "run_ballot_send_job", fake_ballot_send)

    candidate = _research_candidate("Show 1")

    with TestClient(main_mod.app) as client:
        first = client.post("/ops/events/approve", json={"events": [candidate]})
        second = client.post("/ops/events/approve", json={"events": [candidate]})

    assert first.status_code == 200
    assert first.json()["inserted"] == 1
    assert second.status_code == 200
    body = second.json()
    assert body["inserted"] == 0
    assert body["skipped_duplicates"] == ["Show 1"]
    assert body["ballot_sent"] is False
    assert len(repo.events) == 1


def test_ops_research_last_run_reflects_last_call(monkeypatch):
    research_job_mod._last_run = None
    repo = FakeRepository()
    _patch_research_pipeline(monkeypatch, [_research_candidate("Show 1")])
    monkeypatch.setattr(main_mod, "Repository", lambda: repo)

    with TestClient(main_mod.app) as client:
        before = client.get("/ops/research/last-run")
        assert before.json() == {"status": "no_run_yet_this_process"}

        client.post("/ops/research/run")
        after = client.get("/ops/research/last-run")

    assert after.json()["inserted"] == 1
    assert after.json()["inserted_titles"] == ["Show 1"]


def test_parse_profile_text_returns_draft(monkeypatch):
    def fake_parse(text, settings=None):
        assert text == "no metal, I love Watchhouse"
        return {
            "hard_excludes": ["music/rock"],
            "include_tags": [],
            "include_entities": ["Watchhouse"],
            "exemplars": [],
        }

    monkeypatch.setattr(main_mod, "parse_preference_text", fake_parse)

    with TestClient(main_mod.app) as client:
        resp = client.post("/profile/aaron/parse", json={"text": "no metal, I love Watchhouse"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["hard_excludes"] == ["music/rock"]
    assert body["include_entities"] == ["Watchhouse"]


def test_parse_profile_text_requires_text(monkeypatch):
    monkeypatch.setattr(main_mod, "parse_preference_text", lambda text, settings=None: {})

    with TestClient(main_mod.app) as client:
        resp = client.post("/profile/aaron/parse", json={"text": "   "})

    assert resp.status_code == 400


def test_parse_profile_text_unknown_person_404(monkeypatch):
    monkeypatch.setattr(main_mod, "parse_preference_text", lambda text, settings=None: {})

    with TestClient(main_mod.app) as client:
        resp = client.post("/profile/nobody/parse", json={"text": "no metal"})

    assert resp.status_code == 404

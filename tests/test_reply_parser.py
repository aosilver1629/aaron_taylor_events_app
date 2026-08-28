"""Drives app.sms.reply_parser.parse_reply against every case in
tests/fixtures/replies.yaml.

Per spec: "The parser must pass these before it goes near a live number."
That's a statement about the real Claude call, so this test hits the real
Anthropic API and is skipped when no ANTHROPIC_API_KEY is configured. That
means it does NOT run in this build environment (no key is available here)
and MUST be run — and pass — with a real ANTHROPIC_API_KEY before
SMS_PROVIDER is ever flipped to "twilio" against a live number. See
docs/BUILD_NOTES.md.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from app.models import ReplyParseResult

requires_live_claude = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — reply parser fixtures require a live Claude call",
)

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "replies.yaml"


def load_cases():
    data = yaml.safe_load(FIXTURES_PATH.read_text())
    return data["cases"]


@requires_live_claude
@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["name"])
def test_reply_fixture(case):
    from app.sms.reply_parser import parse_reply

    numbered_events = [(n, label) for n, label in case["numbered_events"]]
    result: ReplyParseResult = parse_reply(case["reply"], numbered_events)

    assert sorted(result.yes) == sorted(case["expected"]["yes"]), (
        f"{case['name']}: yes mismatch — got {result.yes}, "
        f"expected {case['expected']['yes']}"
    )
    assert sorted(result.no) == sorted(case["expected"]["no"]), (
        f"{case['name']}: no mismatch — got {result.no}, "
        f"expected {case['expected']['no']}"
    )


def test_fixture_file_has_at_least_30_cases():
    cases = load_cases()
    assert len(cases) >= 30, f"only {len(cases)} fixtures — spec requires at least 30"

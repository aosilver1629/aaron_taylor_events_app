"""Curation layer, Phase 2 — batched tagging/enrichment pass over a whole
research run's validated candidates. One call per run, not per event — per
spec (docs/curation_layer_spec.md §2.3). Matches responses to events by
index, not title, since titles aren't guaranteed unique within a run.

Never blocks or shrinks the run: any failure (missing key, API error,
malformed response, an individual event missing from the response) just
leaves the affected event(s) untagged — enrichment is additive, not a gate.
"""
from __future__ import annotations

import logging

from anthropic import Anthropic

from app.models import EventIn
from app.research.claude_research import _find_tool_use
from app.research.taxonomy import ALL_TAGS, is_valid_tag
from app.utils.retry import with_retry

logger = logging.getLogger("enrichment")

EXTRACT_MODEL = "claude-sonnet-5"
GIST_MAX_LEN = 140
_VALID_ENTITY_ROLES = ("performer", "author", "speaker")

ENRICH_TOOL = {
    "name": "assign_enrichment",
    "description": (
        "Assign taxonomy tags, named entities, a factual gist, and a confidence "
        "score to each event, matched by index."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["events"],
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["index", "tags", "entities", "gist", "confidence"],
                    "properties": {
                        "index": {"type": "integer"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "entities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "role"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "role": {"type": "string", "enum": list(_VALID_ENTITY_ROLES)},
                                },
                            },
                        },
                        "gist": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                },
            }
        },
    },
}


def _build_system_prompt() -> str:
    tag_list = "\n".join(f"- {tag}" for tag in sorted(ALL_TAGS))
    return f"""You are tagging San Francisco Bay Area events against a closed \
taxonomy. For each event, given its title/venue/category/source/pitch, assign:
- tags: zero or more tags from this exact list — use only these, do not \
invent new ones:
{tag_list}
- entities: performers/authors/speakers explicitly named in the event's \
title or pitch, each with a role
- gist: one factual sentence (<= {GIST_MAX_LEN} chars) describing the \
event — no hype, no opinions
- confidence: 0.0-1.0, how confident you are in the tags you assigned

If a real category isn't covered by the closed list above, propose it \
anyway as a free-text tag rather than forcing a wrong-fit existing one — \
it will be reviewed for the taxonomy separately and won't be used from \
this run either way, so don't let a missing category stop you from tagging \
what you can.

Match every event to its "index" from the input list exactly. Call \
assign_enrichment exactly once with one entry per event, in any order."""


def _build_user_message(events: list[EventIn]) -> str:
    lines = [
        f"{i}. title={e.title!r} venue={e.venue!r} category={e.category!r} "
        f"source={e.source!r} pitch={e.pitch!r}"
        for i, e in enumerate(events)
    ]
    return "Events:\n" + "\n".join(lines) + "\n\nCall assign_enrichment."


def enrich_events(events: list[EventIn], settings) -> None:
    """Mutates `events` in place, setting .tags / .entities / .gist /
    .tag_confidence. Leaves every event untagged (all default to []/None)
    on any failure rather than raising — a research run must never abort
    because enrichment failed."""
    if not events:
        return

    client = Anthropic(api_key=settings.anthropic_api_key)
    system = _build_system_prompt()
    user = _build_user_message(events)

    def _call():
        return client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=4000,
            system=system,
            tools=[ENRICH_TOOL],
            tool_choice={"type": "tool", "name": "assign_enrichment"},
            messages=[{"role": "user", "content": user}],
        )

    try:
        response = with_retry(_call, what="enrichment_tag_events", reraise=True)
    except Exception:
        logger.exception("enrichment_failed", extra={"job_fields": {"event_count": len(events)}})
        return

    submit = _find_tool_use(response, "assign_enrichment")
    if submit is None:
        logger.warning("enrichment_no_tool_call", extra={"job_fields": {"event_count": len(events)}})
        return

    by_index = {
        row["index"]: row
        for row in submit.input.get("events", [])
        if isinstance(row, dict) and isinstance(row.get("index"), int)
    }

    for i, event in enumerate(events):
        row = by_index.get(i)
        if row is None:
            continue  # this event alone stays untagged; doesn't affect the others

        valid_tags = []
        for tag in row.get("tags", []):
            if is_valid_tag(tag):
                valid_tags.append(tag)
            else:
                logger.info(
                    "taxonomy_proposal",
                    extra={"job_fields": {"proposed_tag": tag, "event_title": event.title}},
                )
        event.tags = valid_tags

        event.entities = [
            entity
            for entity in row.get("entities", [])
            if isinstance(entity, dict) and entity.get("name") and entity.get("role") in _VALID_ENTITY_ROLES
        ]

        gist = row.get("gist")
        event.gist = gist[:GIST_MAX_LEN] if isinstance(gist, str) else None

        confidence = row.get("confidence")
        event.tag_confidence = float(confidence) if isinstance(confidence, (int, float)) else None

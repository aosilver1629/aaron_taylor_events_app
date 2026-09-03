"""Curation layer — natural-language taste-profile parsing. One strict tool
call turns a free-text description ("no metal, but I love Watchhouse and
outdoor food markets") into draft taste_profiles fields.

This never persists anything itself: app.main's endpoint returns the draft
to profile.html, which folds it into the in-page state for the user to
review — and deselect — before their own Save click writes anything. An
LLM misreading "not really into jazz" as a hard exclude should never
become a real constraint without a human looking at it first, so this is
deliberately a suggestion generator, not a write path.
"""
from __future__ import annotations

import logging

from anthropic import Anthropic

from app.config import get_settings
from app.research.claude_research import _find_tool_use
from app.research.taxonomy import TAXONOMY, is_valid_tag
from app.utils.retry import with_retry

logger = logging.getLogger("profile_parser")

EXTRACT_MODEL = "claude-sonnet-5"
MAX_EXEMPLARS = 5
MAX_EXEMPLAR_LEN = 200

EMPTY_PARSE_RESULT = {
    "hard_excludes": [],
    "include_tags": [],
    "include_entities": [],
    "exemplars": [],
}

PARSE_TOOL = {
    "name": "parse_preferences",
    "description": "Extract taste-profile fields from a free-text description of event preferences.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["hard_excludes", "include_tags", "include_entities", "exemplars"],
        "properties": {
            "hard_excludes": {"type": "array", "items": {"type": "string"}},
            "include_tags": {"type": "array", "items": {"type": "string"}},
            "include_entities": {"type": "array", "items": {"type": "string"}},
            "exemplars": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def _taxonomy_block() -> str:
    return "\n".join(f"{top}: {', '.join(subs)}" for top, subs in TAXONOMY.items())


def _system_prompt() -> str:
    return (
        "You turn a free-text description of someone's event preferences into "
        "structured taste-profile fields. Valid tags are exactly these "
        'top-level/sub-tag pairs, written as "top/sub":\n\n'
        f"{_taxonomy_block()}\n\n"
        "hard_excludes: tags for things they explicitly say they never want "
        '(e.g. "no metal", "don\'t show me improv").\n'
        "include_tags: tags for things they say they like or want more of.\n"
        "include_entities: specific performer/artist/author/venue names they "
        "mention positively, as plain text.\n"
        "exemplars: short phrases (under 200 characters each, at most 5) "
        "capturing a described vibe that doesn't map cleanly to one tag "
        '(e.g. "outdoor food markets with live music"). Only use exemplars '
        "for things that don't already reduce to a tag or entity above - "
        "don't restate a tag as an exemplar too.\n"
        "Only use tags from the list above, in \"top/sub\" form. If nothing in "
        "the text maps to a field, return an empty list for it."
    )


def parse_preference_text(text: str, settings=None) -> dict:
    """Returns {"hard_excludes": [...], "include_tags": [...],
    "include_entities": [...], "exemplars": [...]}. Invalid tags are
    dropped (logged as a taxonomy_proposal, mirroring app.research.enrichment)
    rather than surfaced to the user as garbage. On any call failure,
    returns EMPTY_PARSE_RESULT rather than raising — a parse assist that
    doesn't work is just an empty draft, not a broken page."""
    text = (text or "").strip()
    if not text:
        return dict(EMPTY_PARSE_RESULT)

    settings = settings or get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)

    def _call():
        return client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=1024,
            system=_system_prompt(),
            tools=[PARSE_TOOL],
            tool_choice={"type": "tool", "name": "parse_preferences"},
            messages=[{"role": "user", "content": text}],
        )

    try:
        response = with_retry(_call, what="profile_parse_text", reraise=True)
    except Exception:
        logger.exception("profile_parse_failed", extra={"job_fields": {"text_len": len(text)}})
        return dict(EMPTY_PARSE_RESULT)

    submit = _find_tool_use(response, "parse_preferences")
    if submit is None:
        return dict(EMPTY_PARSE_RESULT)

    raw = submit.input
    raw_hard_excludes = raw.get("hard_excludes", [])
    raw_include_tags = raw.get("include_tags", [])
    hard_excludes = [t for t in raw_hard_excludes if is_valid_tag(t)]
    include_tags = [t for t in raw_include_tags if is_valid_tag(t)]
    dropped = [t for t in raw_hard_excludes + raw_include_tags if not is_valid_tag(t)]
    if dropped:
        logger.info("profile_parse_taxonomy_proposal", extra={"job_fields": {"proposed": dropped}})

    include_entities = [e.strip() for e in raw.get("include_entities", []) if isinstance(e, str) and e.strip()]
    exemplars = [
        e.strip()[:MAX_EXEMPLAR_LEN] for e in raw.get("exemplars", []) if isinstance(e, str) and e.strip()
    ][:MAX_EXEMPLARS]

    return {
        "hard_excludes": hard_excludes,
        "include_tags": include_tags,
        "include_entities": include_entities,
        "exemplars": exemplars,
    }

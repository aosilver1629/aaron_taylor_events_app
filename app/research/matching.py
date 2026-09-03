"""Curation layer, Phase 3 — declarative taste-profile matching. Per spec
(docs/curation_layer_spec.md §3.2). Replaces claude_research.rank_and_select
in run_research_job whenever either person's taste profile has any content;
empty profiles fall back to rank_and_select unchanged (regression guard).

The one product invariant, carried from the spec verbatim: anything the
user *declared* (hard_excludes, include_tags/entities) is a contract;
anything *learned* from votes is a suggestion. A hard exclude makes an
event unelectable no matter what the learned layer or an exemplar call
says — enforced by filtering excluded candidates out entirely, before any
scoring happens, not by trying to out-negative-weight them.
"""
from __future__ import annotations

import logging

from anthropic import Anthropic

from app.config import get_settings
from app.models import EventIn
from app.research.claude_research import _find_tool_use
from app.utils.retry import with_retry

logger = logging.getLogger("matching")

EXTRACT_MODEL = "claude-sonnet-5"

INCLUDE_TAG_SCORE = 2
INCLUDE_ENTITY_SCORE = 3
# Flat cap on total learned-layer contribution per event, however many
# positive net-count signals line up — kept strictly below INCLUDE_TAG_SCORE
# (2) so a purely-learned event can never outscore, or even tie, an event
# with a single declared include. Plan: "learned weight capped at +1/single
# declared include."
LEARNED_WEIGHT_CAP = 1

# Best-effort fallback only — see hard_excluded()'s docstring for when this
# actually applies (only when Phase 2 tagging produced no tags at all).
_CATEGORY_TO_TAXONOMY_TOP = {
    "concert": "music",
    "food": "food",
    "outdoor": "outdoor",
    "art": "art",
    "festival": "fairs",
    "market": "food",
    "other": None,
}


def hard_excluded(event: EventIn, profiles: list[dict]) -> str | None:
    """Returns the excluding tag if `event` should be dropped entirely,
    else None. Union of both profiles' hard_excludes.

    Primary signal: a direct hit against event.tags (Phase 2 enrichment).
    When enrichment produced no tags at all (call failed, or genuinely
    nothing matched the taxonomy), falls back to a coarse category ->
    taxonomy-top mapping so a hard exclude still has *some* effect on an
    untagged event — this fallback only fires when tags is empty, so it
    never second-guesses a real tag-level result.
    """
    excludes: set[str] = set()
    for profile in profiles:
        excludes.update(profile.get("hard_excludes") or [])
    if not excludes:
        return None

    hit = set(event.tags or []) & excludes
    if hit:
        return sorted(hit)[0]

    if not event.tags and event.category:
        top = _CATEGORY_TO_TAXONOMY_TOP.get(event.category)
        if top:
            top_hits = [ex for ex in excludes if ex == top or ex.startswith(f"{top}/")]
            if top_hits:
                return sorted(top_hits)[0]

    return None


def _score_event(event: EventIn, include_tags: set[str], include_entities: set[str], weights: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    for tag in event.tags or []:
        if tag in include_tags:
            score += INCLUDE_TAG_SCORE
            reasons.append(f"declared:{tag}")

    for entity in event.entities or []:
        name = entity.get("name") if isinstance(entity, dict) else None
        if name and name.lower() in include_entities:
            score += INCLUDE_ENTITY_SCORE
            reasons.append(f"entity:{name}")

    learned_raw = 0
    learned_reasons: list[str] = []
    for tag in event.tags or []:
        net = weights.get("tag", {}).get(tag, 0)
        if net > 0:
            learned_raw += 1
            learned_reasons.append(f"learned:tag-{tag}-{net}-yes")
    if event.venue:
        net = weights.get("venue", {}).get(event.venue, 0)
        if net > 0:
            learned_raw += 1
            learned_reasons.append(f"learned:venue-{net}-yes")
    for entity in event.entities or []:
        name = entity.get("name") if isinstance(entity, dict) else None
        if name:
            net = weights.get("entity", {}).get(name, 0)
            if net > 0:
                learned_raw += 1
                learned_reasons.append(f"learned:entity-{name}-{net}-yes")

    learned_score = min(learned_raw, LEARNED_WEIGHT_CAP)
    score += learned_score
    if learned_score > 0 and learned_reasons:
        reasons.append(learned_reasons[0])

    return score, reasons


EXEMPLAR_TOOL = {
    "name": "score_exemplar_relevance",
    "description": "Score each candidate event's relevance (0-10) to each profile owner's exemplars.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["scores"],
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["event_key", "person", "relevance"],
                    "properties": {
                        "event_key": {"type": "string"},
                        "person": {"type": "string"},
                        "relevance": {"type": "number"},
                    },
                },
            }
        },
    },
}


def _score_exemplars(candidates: list[EventIn], profiles_by_person: dict[str, dict]) -> dict[tuple[str, str], float]:
    """One batched call, per spec — never per candidate. Returns
    {(event_key, person): relevance}. Skip-on-failure: an empty dict here
    just means no exemplar bonus is added; scores otherwise stand."""
    exemplar_people = {p: prof for p, prof in profiles_by_person.items() if prof.get("exemplars")}
    if not exemplar_people or not candidates:
        return {}

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)

    exemplar_lines = [
        f"{person} exemplar {i}: {text}"
        for person, prof in exemplar_people.items()
        for i, text in enumerate(prof["exemplars"])
    ]
    candidate_lines = [
        f"{c.event_key}: title={c.title!r} gist={(c.gist or '')!r} tags={c.tags}" for c in candidates
    ]
    system = (
        "You score how relevant each candidate event is to each person's stated exemplars "
        "(examples of events they love). For each (event, person-with-exemplars) pair, give "
        "a 0-10 relevance score reflecting how similar the candidate is to that person's "
        "exemplars. Call score_exemplar_relevance exactly once, covering every candidate for "
        "every person listed under exemplars."
    )
    user = (
        "Exemplars:\n" + "\n".join(exemplar_lines) + "\n\nCandidates:\n" + "\n".join(candidate_lines)
        + "\n\nScore every candidate against every person with exemplars."
    )

    def _call():
        return client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=4000,
            system=system,
            tools=[EXEMPLAR_TOOL],
            tool_choice={"type": "tool", "name": "score_exemplar_relevance"},
            messages=[{"role": "user", "content": user}],
        )

    try:
        response = with_retry(_call, what="matching_exemplar_score", reraise=True)
    except Exception:
        logger.exception("exemplar_scoring_failed", extra={"job_fields": {"candidate_count": len(candidates)}})
        return {}

    submit = _find_tool_use(response, "score_exemplar_relevance")
    if submit is None:
        return {}

    result: dict[tuple[str, str], float] = {}
    for row in submit.input.get("scores", []):
        if not isinstance(row, dict):
            continue
        event_key, person, relevance = row.get("event_key"), row.get("person"), row.get("relevance")
        if event_key and person and isinstance(relevance, (int, float)):
            result[(event_key, person)] = float(relevance)
    return result


def select_with_matching(
    candidates: list[EventIn],
    profiles: dict[str, dict],
    weights: dict,
    cap: int,
) -> list[EventIn]:
    """Hard-exclude -> score (declared + capped-learned) -> optional
    exemplar bonus -> rank -> cap. Sets .match_reasons on each event this
    function returns (first reason is what app.sms.formatting threads into
    the ballot line)."""
    profile_list = list(profiles.values())

    survivors = []
    for event in candidates:
        excluding_tag = hard_excluded(event, profile_list)
        if excluding_tag:
            logger.info(
                "matching_hard_excluded",
                extra={"job_fields": {"title": event.title, "excluded_by": excluding_tag}},
            )
            continue
        survivors.append(event)

    include_tags: set[str] = set()
    include_entities: set[str] = set()
    for profile in profile_list:
        include_tags.update(profile.get("include_tags") or [])
        include_entities.update((name or "").lower() for name in (profile.get("include_entities") or []))

    scored = [(event, *_score_event(event, include_tags, include_entities, weights)) for event in survivors]

    if len(scored) > cap and any(p.get("exemplars") for p in profile_list):
        relevance = _score_exemplars(survivors, profiles)
        rescored = []
        for event, score, reasons in scored:
            for person in profiles:
                rel = relevance.get((event.event_key, person))
                if rel:
                    score += rel
                    reasons = reasons + [f"exemplar:{person}"]
            rescored.append((event, score, reasons))
        scored = rescored

    scored.sort(key=lambda t: (-t[1], t[0].start_at))
    chosen = scored[:cap]

    for event, _score, reasons in chosen:
        event.match_reasons = reasons

    return [event for event, _score, _reasons in chosen]

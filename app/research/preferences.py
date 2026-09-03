"""The original learning mechanism, per spec: summarize which categories/
venues got two yes votes vs. two no votes across the last 50 resolved
ballots, and inject that into the research prompt. No embeddings, no ML —
just counts.

Curation layer, Phase 3 adds a counting sibling, build_preference_weights,
sharing this module's _group_by_event grouping (unanimous 2-person
agreement per event) rather than duplicating it — the two functions differ
only in what they do with the same grouped data: one formats a sentence,
the other returns net counts for matching.py's scoring.
"""
from __future__ import annotations

from collections import defaultdict

from app.db import Repository


def _group_by_event(rows: list[dict]) -> dict[str, dict]:
    by_event: dict[str, dict] = {}
    for row in rows:
        event_id = row["event_id"]
        events_sub = row.get("events") or {}
        entry = by_event.setdefault(
            event_id,
            {
                "responses": {},
                "category": events_sub.get("category"),
                "venue": events_sub.get("venue"),
                "tags": events_sub.get("tags") or [],
                "entities": events_sub.get("entities") or [],
            },
        )
        entry["responses"][row["person"]] = row["response"]
    return by_event


def build_preference_summary(repo: Repository, limit: int = 50) -> str:
    rows = repo.get_recent_resolved_ballots(limit=limit)
    if not rows:
        return "No voting history yet — no preference signal available."

    by_event = _group_by_event(rows)

    both_yes_categories: dict[str, int] = defaultdict(int)
    both_no_categories: dict[str, int] = defaultdict(int)
    both_yes_venues: dict[str, int] = defaultdict(int)
    both_no_venues: dict[str, int] = defaultdict(int)

    for entry in by_event.values():
        responses = entry["responses"]
        if set(responses.values()) == {"yes"} and len(responses) == 2:
            if entry["category"]:
                both_yes_categories[entry["category"]] += 1
            if entry["venue"]:
                both_yes_venues[entry["venue"]] += 1
        elif set(responses.values()) == {"no"} and len(responses) == 2:
            if entry["category"]:
                both_no_categories[entry["category"]] += 1
            if entry["venue"]:
                both_no_venues[entry["venue"]] += 1

    lines = []
    if both_yes_categories:
        top = sorted(both_yes_categories.items(), key=lambda x: -x[1])
        lines.append("Categories both people said yes to: " + ", ".join(f"{c} ({n})" for c, n in top))
    if both_no_categories:
        top = sorted(both_no_categories.items(), key=lambda x: -x[1])
        lines.append("Categories both people said no to: " + ", ".join(f"{c} ({n})" for c, n in top))
    if both_yes_venues:
        top = sorted(both_yes_venues.items(), key=lambda x: -x[1])
        lines.append("Venues both people said yes to: " + ", ".join(f"{v} ({n})" for v, n in top))
    if both_no_venues:
        top = sorted(both_no_venues.items(), key=lambda x: -x[1])
        lines.append("Venues both people said no to: " + ", ".join(f"{v} ({n})" for v, n in top))

    if not lines:
        return "No clear category/venue pattern yet in recent voting history."
    return "\n".join(lines)


def build_preference_weights(repo: Repository, limit: int = 50) -> dict[str, dict[str, int]]:
    """Curation layer, Phase 3. Returns {"tag": {tag: net_yes_count},
    "venue": {venue: net_yes_count}, "entity": {entity_name: net_yes_count}}
    — net_yes_count is yes occurrences minus no occurrences, counted only
    across unanimous 2-person events (a split vote contributes to neither,
    same rule build_preference_summary uses). matching.py's scoring treats
    any positive net count as a weak "learned" signal, capped well below a
    single declared taste-profile include."""
    rows = repo.get_recent_resolved_ballots(limit=limit)
    by_event = _group_by_event(rows)

    tag_net: dict[str, int] = defaultdict(int)
    venue_net: dict[str, int] = defaultdict(int)
    entity_net: dict[str, int] = defaultdict(int)

    for entry in by_event.values():
        responses = entry["responses"]
        if len(responses) != 2:
            continue
        values = set(responses.values())
        if values == {"yes"}:
            delta = 1
        elif values == {"no"}:
            delta = -1
        else:
            continue

        if entry["venue"]:
            venue_net[entry["venue"]] += delta
        for tag in entry["tags"]:
            tag_net[tag] += delta
        for entity in entry["entities"]:
            name = entity.get("name") if isinstance(entity, dict) else None
            if name:
                entity_net[name] += delta

    return {"tag": dict(tag_net), "venue": dict(venue_net), "entity": dict(entity_net)}

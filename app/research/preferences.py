"""The only learning mechanism, per spec: summarize which categories/venues
got two yes votes vs. two no votes across the last 50 resolved ballots, and
inject that into the research prompt. No embeddings, no ML — just counts.
"""
from __future__ import annotations

from collections import defaultdict

from app.db import Repository


def _group_by_event(rows: list[dict]) -> dict[str, dict]:
    by_event: dict[str, dict] = {}
    for row in rows:
        event_id = row["event_id"]
        entry = by_event.setdefault(
            event_id,
            {
                "responses": {},
                "category": (row.get("events") or {}).get("category"),
                "venue": (row.get("events") or {}).get("venue"),
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

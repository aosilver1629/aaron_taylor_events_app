"""Curation layer, Phase 2 — a closed 2-level taxonomy for event tagging.

Deliberately closed: the enrichment tagger may *propose* new nodes (logged,
never used — see app.research.enrichment), but promoting a proposal into
the tree is a human editing this file, not a review UI. Per spec
(docs/curation_layer_spec.md §2.1).
"""
from __future__ import annotations

TAXONOMY: dict[str, list[str]] = {
    "music": [
        "folk", "americana", "indie", "rock", "punk", "electronic",
        "hiphop", "jazz", "classical", "other",
    ],
    "comedy": ["standup", "improv", "other"],
    "talks": ["author", "political", "science", "other"],
    "food": ["popup", "festival", "market"],
    "fairs": ["street-fair", "festival"],
    "art": ["gallery", "film", "theater"],
    "outdoor": ["run", "market", "other"],
}

ALL_TAGS: frozenset[str] = frozenset(
    f"{top}/{sub}" for top, subs in TAXONOMY.items() for sub in subs
)


def is_valid_tag(tag: str) -> bool:
    return tag in ALL_TAGS

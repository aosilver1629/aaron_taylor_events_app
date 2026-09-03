"""Curation layer, Phase 2 — one-off backfill for events stored before
enrichment was wired into run_research_job. Run manually, not scheduled:

    python3 scripts/backfill_tags.py [--table events_sandbox] [--batch-size 20]

Loads stored events with empty tags, runs them through enrich_events in
batches (matching the same batched-per-run pattern research_job.py uses —
never per-event), and persists via update_event_enrichment.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.db import Repository  # noqa: E402
from app.research.enrichment import enrich_events  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default="events", help="Events table to backfill (default: events)")
    parser.add_argument("--batch-size", type=int, default=20, help="Events per enrichment call")
    parser.add_argument("--limit", type=int, default=200, help="Max events to fetch")
    args = parser.parse_args()

    settings = get_settings()
    repo = Repository(events_table=args.table)

    events = repo.get_events_with_empty_tags(limit=args.limit)
    if not events:
        print(f"No untagged events found in {args.table}.")
        return

    print(f"Backfilling {len(events)} event(s) in {args.table}, {args.batch_size} per call...")

    tagged = 0
    for start in range(0, len(events), args.batch_size):
        batch = events[start:start + args.batch_size]
        # enrich_events expects EventIn-like objects (.title/.venue/.category/
        # .source/.pitch and settable .tags/.entities/.gist/.tag_confidence)
        # — Event has all of these, so it works unchanged.
        enrich_events(batch, settings)
        for event in batch:
            if event.tags:
                tagged += 1
            repo.update_event_enrichment(event.id, event.tags, event.entities, event.gist, event.tag_confidence)
        print(f"  batch {start // args.batch_size + 1}: {len(batch)} events processed")

    print(f"Done. {tagged}/{len(events)} events got at least one tag.")


if __name__ == "__main__":
    main()

"""Runs the research pipeline (app.jobs.research_job.run_research_job)
against events_sandbox instead of the real events table — same validation,
same dedup, same window logic, just pointed at a disposable table via
Repository(events_table=...). Nothing here ever touches ballots, Calendar,
or Twilio.

Usage: python3 sandbox/run_sandbox_research.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Repository  # noqa: E402
from app.jobs.research_job import run_research_job  # noqa: E402

SANDBOX_TABLE = "events_sandbox"


def main() -> None:
    repo = Repository(events_table=SANDBOX_TABLE)
    result = run_research_job(repo)

    print(f"Ticketmaster found: {result['ticketmaster_found']}")
    print(f"Bandsintown found: {result['bandsintown_found']}")
    print(f"Claude candidates: {result['claude_candidates']}")
    print(f"Validated: {result['validated']}")
    print(f"Rejected: {result['rejected']}")
    print(f"Inserted into {SANDBOX_TABLE}: {result['inserted']}")
    print()
    for event in result["inserted_events"]:
        print(f"- {event.title} | {event.start_at} | {event.venue} | {event.category} | {event.price_range}")
        print(f"    source: {event.source}  |  url: {event.url}")
        print(f"    pitch: {event.pitch}")


if __name__ == "__main__":
    main()

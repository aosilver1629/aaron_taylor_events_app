"""Clears every row from events_sandbox. Run this before repopulating with
a fresh research pass, so old candidates from a previous prompt/source
tweak don't linger and skew what you're looking at.

Usage: python3 sandbox/reset_events_sandbox.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_client  # noqa: E402

SANDBOX_TABLE = "events_sandbox"
# PostgREST has no bare "delete everything" — .neq on an id no row will
# ever have is the standard way to express an unconditional delete.
_IMPOSSIBLE_ID = "00000000-0000-0000-0000-000000000000"


def main() -> None:
    client = get_client()
    before = client.table(SANDBOX_TABLE).select("id", count="exact").execute()
    count = before.count or 0
    if count == 0:
        print(f"{SANDBOX_TABLE} is already empty.")
        return
    client.table(SANDBOX_TABLE).delete().neq("id", _IMPOSSIBLE_ID).execute()
    print(f"Cleared {count} row(s) from {SANDBOX_TABLE}.")


if __name__ == "__main__":
    main()

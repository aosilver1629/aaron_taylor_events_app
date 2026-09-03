"""Curation layer, Phase 1 — source registry with health tracking.

`SOURCES` in app.research.deterministic_search moves into the database via
this module: `load_sources` returns dicts shaped exactly like today's
hardcoded list (label/url/kind/parser/extraction_rules), falling back to
the hardcoded `SOURCES` when the table is empty or unreachable, so nothing
breaks mid-migration. `record_source_run` is a plain callback the caller
wires in — deterministic_search.py never imports this module, to avoid a
circular import (this module already depends on deterministic_search for
the fallback list).

Status rules (computed after every run, from the last 8 runs) are
deliberately dumb — no ML, no tuning knobs, see module docstring in the
spec (docs/curation_layer_spec.md §1.3) for the rationale:
  - blocked  — the latest run's fetch saw one of _BLOCKED_MARKERS.
  - silent   — zero candidates for 2 consecutive runs (and not blocked).
  - degraded — latest candidates < 40% of the trailing median (median of
               the *prior* runs; needs at least 3 prior runs of history).
  - healthy  — otherwise.
"""
from __future__ import annotations

import logging

from app.research.deterministic_search import SOURCES

logger = logging.getLogger("source_registry")

RECENT_RUNS_WINDOW = 8
DEGRADED_RATIO = 0.4
MIN_HISTORY_FOR_DEGRADED = 3


def load_sources(repo, city: str = "sf") -> list[dict]:
    """Returns dicts shaped like deterministic_search.SOURCES entries
    (label/url/kind/[parser]/[extraction_rules]), plus an "id" key for
    DB-backed sources (used by record_source_run — its absence is how a
    hardcoded-fallback source is recognized downstream)."""
    try:
        rows = repo.get_sources(city)
    except Exception:
        logger.exception("source_registry_load_failed")
        rows = []

    if not rows:
        return list(SOURCES)

    sources = []
    for row in rows:
        entry = {
            "id": row["id"],
            "label": row["label"],
            "url": row["url"],
            "kind": row["kind"],
            "status": row.get("status"),
        }
        if row.get("parser_id"):
            entry["parser"] = row["parser_id"]
        if row.get("extraction_rules"):
            entry["extraction_rules"] = row["extraction_rules"]
        sources.append(entry)
    return sources


def _compute_status(recent_runs: list[dict]) -> tuple[str, str | None, int]:
    """recent_runs: most-recent-first, up to RECENT_RUNS_WINDOW rows, each
    with "candidates" (int) and "blocked_marker_seen" (bool). Returns
    (status, status_reason, consecutive_zero_runs)."""
    if not recent_runs:
        return "healthy", None, 0

    consecutive_zero = 0
    for run in recent_runs:
        if run["candidates"] == 0:
            consecutive_zero += 1
        else:
            break

    latest = recent_runs[0]
    if latest["blocked_marker_seen"]:
        return "blocked", "blocked marker seen on latest run", consecutive_zero

    if consecutive_zero >= 2:
        return "silent", "zero candidates for 2 consecutive runs", consecutive_zero

    prior = recent_runs[1:]
    if len(prior) >= MIN_HISTORY_FOR_DEGRADED:
        counts = sorted(r["candidates"] for r in prior)
        n = len(counts)
        median = counts[n // 2] if n % 2 == 1 else (counts[n // 2 - 1] + counts[n // 2]) / 2
        if median > 0 and latest["candidates"] < DEGRADED_RATIO * median:
            reason = f"latest candidates {latest['candidates']} < {DEGRADED_RATIO:.0%} of trailing median {median}"
            return "degraded", reason, consecutive_zero

    return "healthy", None, consecutive_zero


def record_source_run(
    repo,
    source: dict,
    fetch_method: str,
    extract_method: str,
    candidates: int,
    blocked_marker_seen: bool,
) -> str | None:
    """No-op for hardcoded-fallback sources (no "id" — no registry row to
    update against). Returns the new status, or None if skipped."""
    source_id = source.get("id")
    if not source_id:
        return None

    repo.insert_source_run(source_id, fetch_method, extract_method, candidates, blocked_marker_seen)
    recent = repo.get_recent_source_runs(source_id, limit=RECENT_RUNS_WINDOW)
    status, reason, consecutive_zero = _compute_status(recent)
    repo.update_source_status(source_id, status, reason, consecutive_zero)

    previous_status = source.get("status")
    if previous_status is not None and previous_status != status:
        logger.info(
            "source_status_changed",
            extra={
                "job_fields": {
                    "source": source.get("label"),
                    "from": previous_status,
                    "to": status,
                    "reason": reason,
                }
            },
        )
    return status


def get_source_health(repo, city: str = "sf") -> list[dict]:
    """Backing data for GET /ops/sources: per source, label / tier-in-use
    (from the latest run's methods) / status / status_reason / the last
    RECENT_RUNS_WINDOW candidate counts (most-recent-first)."""
    rows = repo.get_sources(city)
    health = []
    for row in rows:
        runs = repo.get_recent_source_runs(row["id"], limit=RECENT_RUNS_WINDOW)
        tier_in_use = f"{runs[0]['fetch_method']}/{runs[0]['extract_method']}" if runs else None
        health.append(
            {
                "label": row["label"],
                "tier_in_use": tier_in_use,
                "status": row.get("status"),
                "status_reason": row.get("status_reason"),
                "recent_candidates": [r["candidates"] for r in runs],
            }
        )
    return health

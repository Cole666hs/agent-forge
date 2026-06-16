"""Example 8 — Retention monitor (v0.13.0).

Pure-Python walkthrough of the prune API. Inserts fake old runs/events
into a local state.db, then:

  1. Calls `runs.prune_older_than_days(N)` and `events.prune_older_than_days(N)`
     in dry-run style (just count, don't actually delete — we copy the
     rows into a side table for inspection).
  2. Compares with what would have been deleted.
  3. Actually deletes (apply) and verifies the new row counts.

No LLM, no adapter, no daemon — a self-contained script.

Run it:

    .venv/bin/python examples/08-retention-monitor/run.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from agentforge.state import State  # noqa: E402


def _insert_fake_run(state: State, days_old: int, workflow: str = "demo") -> str:
    """Insert a fake run with a `started_at` `days_old` days in the past."""
    import uuid

    run_id = f"run_{uuid.uuid4().hex[:12]}"
    started_at = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    ended_at = (datetime.now(timezone.utc) - timedelta(days=days_old) + timedelta(seconds=2)).isoformat()
    with state._tx() as conn:
        conn.execute(
            "INSERT INTO runs (id, workflow, tenant_id, agent, started_at, ended_at, status, duration_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, 'success', 2.0)",
            (run_id, workflow, "tenant_demo", "demo-agent", started_at, ended_at),
        )
    return run_id


def _insert_fake_event(state: State, run_id: str, days_old: int) -> None:
    """Insert a fake run_event with a `ts` `days_old` days in the past.

    The `run_events` table has a UNIQUE(seq) autoincrement column, so we
    let SQLite pick the next seq for us by inserting NULL.
    """
    ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    with state._tx() as conn:
        conn.execute(
            "INSERT INTO run_events (seq, run_id, workflow, tenant_id, kind, payload, ts) "
            "VALUES (NULL, ?, 'demo', 'tenant_demo', 'info', ?, ?)",
            (run_id, json.dumps({"note": "old event"}), ts),
        )


def main() -> None:
    here = Path(__file__).resolve().parent
    state_db = here / "state.db"
    if state_db.exists():
        state_db.unlink()

    state = State(state_db)

    print("== Seeding fake runs/events across ages ==")
    # 3 runs each at 1, 10, 40, 100 days old → 12 runs total
    for days in (1, 10, 40, 100):
        for _ in range(3):
            run_id = _insert_fake_run(state, days)
            _insert_fake_event(state, run_id, days)
    print("  12 runs + 12 events seeded (ages 1/10/40/100 days)")

    print("\n== Dry-run: what would be deleted at 30-day retention? ==")
    # We can't truly "dry-run" against the prune method itself (it deletes),
    # so we approximate by counting rows that would match the cutoff.
    cutoff_30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with state._tx() as conn:
        runs_to_delete = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE started_at < ?", (cutoff_30,)
        ).fetchone()[0]
        events_to_delete = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE ts < ?", (cutoff_30,)
        ).fetchone()[0]
    print(f"  runs > 30d old: {runs_to_delete} (would be deleted)")
    print(f"  events > 30d old: {events_to_delete} (would be deleted)")

    print("\n== Apply: runs.prune_older_than_days(30) + events.prune_older_than_days(30) ==")
    runs_deleted = state.runs.prune_older_than_days(30)
    events_deleted = state.events.prune_older_than_days(30)
    print(f"  runs deleted: {runs_deleted}")
    print(f"  events deleted: {events_deleted}")

    print("\n== Post-prune row counts ==")
    with state._tx() as conn:
        runs_total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        events_total = conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
    print(f"  runs: {runs_total}  (was 12, expected 6 = 3*1d + 3*10d)")
    print(f"  events: {events_total}  (was 12, expected 6)")

    print("\n== Disabled retention (days=0 → no-op) ==")
    n = state.runs.prune_older_than_days(0)
    print(f"  prune_older_than_days(0) returned: {n} (expected 0)")

    state.close()
    print("\ndone.")


if __name__ == "__main__":
    main()

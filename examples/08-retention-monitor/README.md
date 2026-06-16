# Example 8 — Retention monitor

Demonstrates the **v0.13.0** retention API. Self-contained — seeds fake
runs/events into a local `state.db`, dry-runs the prune math, applies it,
and verifies the post-prune row counts.

## What it shows

| Step | API call | Purpose |
|---|---|---|
| 1 | (seed) | Insert 12 fake runs + 12 fake events at 1/10/40/100 days old |
| 2 | (count) | Dry-run: count what `prune_older_than_days(30)` would delete |
| 3 | `state.runs.prune_older_than_days(30)` | Actually delete old runs |
| 4 | `state.events.prune_older_than_days(30)` | Actually delete old events |
| 5 | `state.runs.prune_older_than_days(0)` | `0` = "disabled" sentinel (no-op) |

## Run it

```bash
.venv/bin/python examples/08-retention-monitor/run.py
```

Expected output: a short report showing the seed, the dry-run count, the
apply counts, the post-prune verification, and the disabled-sentinel
test. Side effects: a local `state.db` (gitignored).

## When to use this in real life

- **Operations dashboard:** wire this into a cron and report
  `prune_older_than_days(N)` return values to your monitoring. A non-zero
  number means retention is actually pruning; a constant zero means
  retention is disabled (env var set to `0`).
- **CLI trigger:** `agentforge runs prune --apply --older-than 90` is the
  manual one-shot equivalent — useful before a backup or a DB migration.
- **Production default:** `AGENTFORGE_RETENTION_RUNS_DAYS=90` and
  `AGENTFORGE_RETENTION_EVENTS_DAYS=30` are the recommended starting
  point. Tune by storage pressure and your audit-trail requirements.

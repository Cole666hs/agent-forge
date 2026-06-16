# Migration guide

How to upgrade between major agentforge versions. Most minor releases
do NOT require migration — the [stability policy](./STABILITY.md) says
minor versions are backwards-compatible.

## v0.18.0 → v0.19.0 (or v1.0.0)

**No code changes required.** This release fixed a version-drift bug
where `agentforge.__version__` and `FastAPI(version=...)` were stuck
at older values; both now read from a single source of truth. If you
were checking the version via the HTTP `/openapi.json` endpoint, you
will see the corrected value (0.18.0 instead of 0.13.0). Otherwise
nothing changes.

## v0.17.0 → v0.18.0

**No code changes required.** Adds `STABILITY.md`, a version-consistency
regression test, and a single import (`from agentforge import __version__`)
in `serve.py`. No public API changed.

## v0.16.0 → v0.17.0

**No code changes required.** New top-level `Dockerfile`,
`docker-compose.yml`, `.env.example`, `contrib/systemd/agentforge.service`,
and `DEPLOY.md`. If you were using `examples/06-docker-deploy/` for
production, the files there still work; the new root files are
identical content.

## v0.15.0 → v0.16.0 (security)

**No code changes required,** but verify your deploy:

- If you have any workflows named with dots or slashes (e.g. `..secret`),
  they will now return 404. Rename them to use only `[A-Za-z0-9_-]`.
- If you POST bodies larger than 1 MiB, set
  `AGENTFORGE_MAX_BODY_BYTES` higher or you'll get 413.
- Review `SECURITY.md` for the new operator checklist.

## v0.14.0 → v0.15.0

**No code changes required.** New examples (`07-workflow-versioning`,
`08-retention-monitor`) and a CI smoke test that runs them. The
`tests/unit/test_examples_smoke.py` runner is added to the suite;
if you have a custom CI step, include it in the test command.

## v0.13.0 → v0.14.0 (schema bump)

**Schema migration is automatic**, but verify:

- Pre-v0.14.0 state.db files open transparently (migration adds the
  `workflow_versions` table). No data loss.
- The `agentforge workflows versions` CLI subcommands and the
  `state.workflows.*` Python API are new. Old code that doesn't use
  them is unaffected.

## v0.12.0 → v0.13.0 (schema bump)

**Schema migration is automatic.**

- SCHEMA bumped from 1 → 2. Pre-v0.13.0 state.db files get the
  `run_events` table added.
- The retention background task is opt-in via env vars; it does not
  run by default unless `AGENTFORGE_RETENTION_RUNS_DAYS > 0` (default
  is 90, so it IS on by default — set to 0 to disable).
- The `agentforge runs prune` CLI is new.

## v0.11.0 → v0.12.0

**No code changes required.**

- New `GET /v1/runs/{id}/logs?follow=true` SSE endpoint
- New `agentforge runs logs <run_id>` CLI
- New `app.state.runs`, `app.state.events`, `app.state.active_runs`
  exposed on the FastAPI app (handy for tests; no public consumer
  changes)

## v0.10.0 → v0.11.0

**No code changes required,** but verify:

- The MCP server is opt-in: install with `pip install 'agentforge[mcp]'`
  and run `agentforge serve --mcp`. If you don't install the extra,
  nothing changes.
- New `GET /v1/workflows` and `GET /v1/workflows/{name}` HTTP routes
  for listing workflow files.

## v0.9.0 → v0.10.0

**No code changes required.**

- New `agentforge runs cancel <run_id>` CLI subcommand
- Cancel is HTTP-based: it POSTs to the daemon's cancel endpoint.
  The CLI is a thin caller; the actual cancel logic is in the daemon.

## v0.8.0 → v0.9.0

**No code changes required.**

- New `/dashboard/runs/{run_id}` detail page
- New `agentforge runs {list,show}` CLI subcommand
- New `/dashboard/metrics` page

## v0.7.0 → v0.8.0

**No code changes required.**

- Run cancellation: `POST /v1/workflows/{name}/runs/{run_id}/cancel`
- WebSocket stream `/v1/runs/{id}/events` now uses the same channel
  as the dashboard

## v0.6.x → v0.7.0

**No code changes required.** WebSocket-based dashboard updates replace
the 5-second polling loop.

## v0.5.x → v0.6.0

Tenants + usage + runs storage moves from JSON files to SQLite:

- `tenants.json` → `state.db` (table `tenants`)
- `usage.json` → `state.db` (table `usage`)
- `runs.json` → `state.db` (table `runs`)

**Migration is automatic on first open of the new version.** Pre-0.6.0
data is preserved; the new version writes a backup
`*.pre-v0.6.0.bak` next to each JSON file before migration.

If you have custom code reading the JSON files directly, switch to
the SQLite handles:
- `TenantRegistry.path` → `state.tenants`
- `UsageStore.path` → `state.usage`
- `RunStore.path` → `state.runs`

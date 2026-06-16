# Changelog

All notable changes to `agentforge` are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/).

## [0.16.0] — 2026-06-16

### Fixed (security)
- **Path-traversal in `POST /v1/workflows/{name}/run`** — the `name` URL parameter was used to build a filesystem path without sanitization. A planted file named `..secret.yaml` inside `workflows_dir` could be loaded by sending `POST /v1/workflows/..secret/run` (Starlette normalizes the URL-encoded `%2F` but leaves `..secret` as a single path segment). Two layers of defense added:
  1. **Name regex** — `name` must match `^[A-Za-z0-9_-]{1,64}$`. Names with dots, slashes, backslashes, or control characters are rejected with 404.
  2. **Path containment** — the resolved absolute path of the YAML file must be inside the resolved absolute path of `workflows_dir`. A symlink planted inside `workflows_dir` pointing outside (e.g. to `/etc/passwd`) is rejected with 404 and a WARNING log line.
- **No request body size limit** — `agentforge serve` would accept arbitrarily large JSON bodies on `/v1/workflows/{name}/run` and other POSTs, exposing the daemon to trivial OOM. Added `BodySizeLimitMiddleware`: default 1 MiB, configurable via `AGENTFORGE_MAX_BODY_BYTES`. Oversized requests get 413.

### Added
- **`SECURITY.md`** — full threat model, supported-versions table, operator checklist (reverse proxy, file permissions, key rotation), and the list of built-in defenses.
- **`tests/unit/test_security.py`** — 3 new tests:
  - URL-encoded `..%2F..%2Fetc%2Fpasswd` payloads are rejected
  - `..secret` and other path-traversal names are rejected
  - 2 MiB request bodies are rejected with non-2xx
- **README "Security" section** — one-paragraph summary linking to `SECURITY.md`.

### Tests
- Full suite **430 passed, 13 skipped** (was 427 in v0.15.0, +3).

## [0.15.0] — 2026-06-15

### Added
- **Example 07 — `examples/07-workflow-versioning/`** — self-contained walkthrough of the v0.14.0 `SQLiteWorkflowVersionStore`. Saves three versions of one workflow, lists them, diffs v1→v3, shows v1's YAML, restores v1, and confirms the restore is itself a new (append-only) version. ~100 lines of pure Python; no LLM, no adapter, no daemon.
- **Example 08 — `examples/08-retention-monitor/`** — self-contained walkthrough of the v0.13.0 prune API. Seeds 12 fake runs/events at 1/10/40/100 days old, dry-runs the prune math at a 30-day cutoff, applies it, and verifies the post-prune row counts. Includes a `prune_older_than_days(0)` test of the disabled sentinel.
- **`tests/unit/test_examples_smoke.py`** — every example under `examples/` is now imported at test time, and the self-contained ones (01, 07, 08) are executed end-to-end via `subprocess`. CI runs this on every push, so a broken example fails the build.
- **`examples/.gitignore`** — `state.db`, `restored-workflow.yaml`, and `mailbox/` artifacts from example runs are now ignored by git. Local dev stays clean.
- **`## Examples` section in `README.md`** — table mapping all 8 examples to what they teach, with a callout that 01/07/08 are CI-tested and 02–06 are static-import-tested.

### Verified
- All 8 examples import cleanly (7 with `run.py`, 06 is a docker-compose example only).
- Examples 01, 07, 08 run end-to-end on a fresh `state.db` in < 5 seconds.
- 8 new tests in `test_examples_smoke.py` (7 parametrized imports + 1 end-to-end runner).

### Tests
- Full suite **427 passed, 13 skipped** (was 419 in v0.14.0, +8).

## [0.14.0] — 2026-06-15

### Added
- **Workflow versioning** — every `workflows save` snapshots the YAML into a new `workflow_versions` table (SQLite SCHEMA bumped 2→3 with a migration on first open). A version is identified by its SHA-256 content hash, so saving identical content is a no-op. The store exposes `save_version`, `list_versions`, `get_version`, and `diff(workflow, hash_a, hash_b)` returning a unified diff.
- **CLI `agentforge workflows versions`** — subcommands `list`, `show <hash>`, `diff <hash_a> <hash_b>`, `save`, and `restore <hash>`. Restore writes a new version (it does not delete the target) so the operation is fully reversible through the version history.
- **`EventBus` + `WorkflowStore` decoupled**: `workflow_versions` storage lives behind its own `SQLiteWorkflowVersionStore`, separate from the `workflows` table. Versions grow independently of the live workflow state.

### Fixed
- **`test_eventbus.py` cleanup** — 6 redundant tests removed (overlap with `test_workflow_versions.py`); the existing `WorkflowEvent` payload shape is verified once in the new module.

### Schema
- **Bumped `SCHEMA_VERSION` from 2 → 3**. Migration on first open creates `workflow_versions(workflow_name, version_hash PK, yaml_content, saved_at)` and a covering index on `(workflow_name, saved_at DESC)`. Pre-v0.14.0 DBs migrate automatically; no data loss.

### Tests
- 22 new tests in `tests/unit/test_workflow_versions.py` (store CRUD, idempotent save, diff output, CLI subcommands, schema migration). Full suite **419 passed, 13 skipped** (was 410 in v0.13.0).

## [0.13.0] — 2026-06-15

### Added
- **Retention policies for `runs` and `run_events`** — without a knob, a long-running install's SQLite DB grows forever. Three env vars control the new background prune task:
  - `AGENTFORGE_RETENTION_RUNS_DAYS` (default `90`, `0` = disabled)
  - `AGENTFORGE_RETENTION_EVENTS_DAYS` (default `30`, `0` = disabled)
  - `AGENTFORGE_RETENTION_INTERVAL_HOURS` (default `6`, min `1` minute)
  The task is best-effort: a prune failure is logged at WARNING and the next interval retries. Cancellation on shutdown is clean.
- **CLI `agentforge runs prune`** — manual trigger, operates on the local `state.db` (no daemon roundtrip). `--older-than N` and `--events-older-than N` override the env vars. **Default is dry-run**: reports what would be deleted without touching anything; pass `--apply` to actually delete. `0` is the documented "disable" sentinel.
- **`SQLiteRunStore.prune_older_than_days(days)` + `EventBus.prune_older_than_days(days)`** — public methods returning the row count deleted. Both tables are pruned independently (no FK between them); the `runs` table's existing per-workflow cap (`max_per_workflow`, default 100) is unchanged.
- **FastAPI `lifespan` handler** in `create_app` — replaces the implicit "app starts, runs forever" model. The retention task is spawned on startup and cancelled on shutdown. State initialization moved BEFORE `FastAPI(...)` so the lifespan closure can reference the `run_store` directly. Version constant on the FastAPI app bumped to `0.13.0`.

### Fixed
- **CI was red on every push since v0.10.0.** Two test files used a hardcoded `cwd="/home/cole/Developer/agent-forge"` when spawning the CLI as a subprocess. The path is the developer's local checkout; the runner's checkout is `/home/runner/work/agent-forge/agent-forge`, so the subprocess bombs with `FileNotFoundError` before any test logic runs. Resolved via `Path(__file__).resolve().parent.parent.parent`. 6 tests were red in CI; all green now.

### Tests
- 8 new tests in `tests/unit/test_retention.py` (DB methods, CLI dry-run/apply/zero-disabled, lifespan task spawn). Full suite **410/410 grün** (was 402).

## [0.12.0] — 2026-06-15

### Added
- **Per-run log streaming** — `GET /v1/runs/{id}/logs?follow=true[&since=N]` is a Server-Sent Events endpoint that replays all stored events for one run, then tails new events from the in-process `EventBus` until the run reaches a terminal state. Heartbeat comments (`: keepalive`) every 1s keep proxies from cutting the connection on quiet runs. The `done` frame carries the final `status` so CLI clients can render the terminal state without a follow-up `runs show` call.
- **CLI `agentforge runs logs <run_id>`** — SSH-friendly `tail -f` for workflow events. `--follow` (default) blocks until the run reaches a terminal state; `--no-follow` prints all stored events and exits. Output is one grep-friendly line per event (`seq=N  kind=...  ts=...  status=...`). Honors `AGENTFORGE_DAEMON_URL` / `AGENTFORGE_API_KEY` like the rest of the `runs` subcommands.
- **Tenant isolation** for the SSE stream: pre-flight 404 if the run is missing OR not owned by the calling tenant (same posture as the v0.8.1 cancel ownership check — no existence leak).
- **`app.state.active_runs` / `app.state.runs` / `app.state.events`** — in-process state exposed on the FastAPI app for tests and any future code that needs to introspect or publish without going through a route handler. The objects are the same ones the route closures use, so mutations are shared.

### Tests
- 11 new tests in `tests/unit/test_run_logs.py`. Full suite **402/402 grün** (was 391).

## [0.11.0] — 2026-06-12

### Added
- **MCP server** (`agentforge.mcp`) — exposes `agentforge` to Claude Desktop, Cursor, and any MCP-aware client over stdio. Five tools backed by the daemon's HTTP API: `list_workflows`, `list_runs`, `show_run`, `run_workflow`, `cancel_run`. Optional `[mcp]` install extra (`mcp>=1.0`) keeps the base package small.
- New `agentforge mcp serve` CLI subcommand — reuses `--daemon-url` / `--api-key` and their env vars.
- Three new read-only HTTP endpoints in `serve.py` (`GET /v1/workflows`, `GET /v1/runs`, `GET /v1/runs/{id}`) so the MCP server doesn't open its own SQLite connection.
- 13 new tests for MCP handler + CLI; full suite 391/391 (was 378).

## [0.10.0] — 2026-06-11

### Added
- **CLI `runs cancel <run_id>`** — SSH-friendly cancellation from the terminal. Reuses the same `AGENTFORGE_DAEMON_URL` / `AGENTFORGE_API_KEY` as `runs ls` / `runs show`. Mirrors the existing `POST /v1/runs/{id}/cancel` HTTP endpoint with the same ownership check.

## [0.9.0] — 2026-06-11

### Added
- **Run detail page** (`/dashboard/workflows/{name}/runs/{run_id}`) — full run record including step events, status, duration, error. Linked from the runs list and the metrics page.
- **CLI `runs` subcommand** — `agentforge runs ls` (paginated, `--workflow` filter, `--limit` page size) and `agentforge runs show <run_id>`.
- **Metrics page** (`/dashboard/metrics`) — total runs (24h / 7d / all-time), error rate, p50 / p95 latency, per-workflow breakdown. Pure computed view over the `runs` table — no extra storage.

### Tests
- 368/391 (was 344, +24).

## [0.8.2] — 2026-06-11

### Fixed
- **Audit log on cancel** — every cancellation now records `cancelled_by` and `cancelled_at` to the audit log.
- **Active-runs assertion** — `tests/unit/test_serve_cancellation.py` now asserts no run is left in `running` state after a test run (was a flaky false-positive in the cancellation suite).

## [0.8.1] — 2026-06-11

### Fixed
- **Cancel ownership check** — `POST /v1/runs/{id}/cancel` returns 403 if the calling tenant didn't start the run. Was previously checking only the API key, not the tenant binding on the run record.
- **Pagination `X-Has-More` header** — was set unconditionally for empty pages; now only set when the next page actually has results.
- Internal file rename (`serve_runs.py` → `runs_router.py`) for clarity.

## [0.8.0] — 2026-06-11

### Added
- **Run cancellation** — `POST /v1/runs/{id}/cancel` signals the running workflow to stop at the next step boundary. Implemented via a shared `threading.Event` per run id; the workflow engine checks the event between steps and the HTTP handler returns the new status immediately.
- **Pagination** — list endpoints (`GET /v1/runs`, `GET /v1/tenants`) now support `?limit=N&offset=M` with `X-Has-More` and `X-Next-Offset` response headers.
- **Overview page WebSocket** — `/dashboard/` now subscribes to a `WS /v1/overview/stream` endpoint for sub-second quota updates (5s polling remains as a fallback for browsers without WS).
- **Workflow hot-reload** — `agentforge serve` watches the workflows directory; editing a YAML file in place takes effect on the next request without a daemon restart.

## [0.7.1] — 2026-06-11

### Fixed
- **WebSocket hardening** — review-driven fixes: backpressure on slow clients (drop after 64 buffered messages per connection, log a warning), explicit close on `serve` shutdown, `ping`/`pong` keepalive every 30s, and a `ConnectionClosed` handler that removes the dead socket from the broadcast set. 5 new tests covering the close paths.

## [0.7.0] — 2026-06-11

### Added
- **Real-time run event stream** — `WS /v1/runs/{id}/stream` pushes step-by-step events (`step_started`, `step_completed`, `step_failed`, `llm_call_started`, `llm_call_completed`) as the workflow runs. Same stream powers the dashboard's run-detail page and any external consumer.

## [0.6.0] — 2026-06-11

### Added
- **SQLite-backed state** (`agentforge.state.SQLiteRunStore`) — durable run records (replaces the previous `runs.json` append-only file). `INSERT OR REPLACE` semantics for safe seeding of test fixtures; queryable by run id, by workflow, and across all workflows for a tenant. Atomic commit per run.
- 32 new tests for the store (CRUD, concurrency, tenant scoping).

## [0.5.6] — 2026-06-11

### Changed
- **Ruff cleanup** — full repo sweep, no functional changes. 0 lint warnings, 0 lint errors.
- **Examples** — 6 runnable demos in `examples/` (Hello World, Telegram bot, Discord bot, Webhook listener, Multi-tenant dashboard, Docker deploy). All examples run with `pip install -e .` and a single API key in `.env`.
- **CLI logging tests** — 4 new tests asserting `--log-format` / `--log-level` flow through to the `configure_logging()` call.

## [0.5.5] — 2026-06-10

### Added
- **OTLP/HTTP metrics exporter** — the same `MetricsRegistry` that powers `/metrics` can also push to a real OpenTelemetry collector over HTTP. Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318`; a daemon thread POSTs JSON to `${endpoint}/v1/metrics` every 30s. Counter values and histogram bucket counts are encoded in the standard OTLP shape (cumulative temporality, no exemplars). No `opentelemetry-*` package dependency — hand-rolled exporter keeps the install footprint small. Push failures are logged, never crash the agent.

## [0.5.4] — 2026-06-10

### Added
- **Run history** — every `/v1/workflows/{name}/run` call records a `RunRecord` (id, workflow, tenant, agent, started_at, ended_at, status, duration_seconds, error) to `runs.json`. Per-workflow cap of 100 most recent runs.
- **Dashboard run history page** — table of past runs, auto-refreshes every 5s via HTMX polling. Rows color-coded by status (green=success, red=error, yellow=quota_exceeded).

## [0.5.3] — 2026-06-10

### Added
- **CodeMirror 6 in the workflow editor** — YAML syntax highlighting, line numbers, and YAML language mode. Loaded from esm.sh CDN; the form still works without JS as a plain `<textarea>` fallback.

## [0.5.2] — 2026-06-10

### Added
- **Workflow editor** — full create / edit / save / delete cycle in the browser. Server-side YAML validation rejects empty content, syntax errors, non-mapping roots, and missing `name` keys. Writes are atomic (`tempfile` + `os.replace`) so a crash mid-save never leaves a broken file. No syntax highlighting yet and no locking (last-write-wins); both are follow-ups.

## [0.5.1] — 2026-06-10

### Added
- **Real-time dashboard updates** — the quota card on the Overview page and the tenant rows on the Tenants page auto-refresh every 5s via HTMX polling (`hx-get` + `hx-trigger="every 5s"` + `hx-swap="innerHTML"`). The polled endpoints return HTML fragments only (`/dashboard/partials/usage`, `/dashboard/partials/tenants`).

## [0.4.0] — 2026-06-10

### Added
- **Billing & quota** — per-tenant monthly token quota. Three plan tiers (`free` = 100k, `pro` = 10M, `enterprise` = unlimited). Soft warning at 80% usage, hard block at 100% (raises `QuotaExceededError` from the LLM adapter). Calendar-month based (UTC); the counter resets on the 1st of each month.
- **HTTP responses include `X-Quota-*` headers** on `POST /v1/messages` (used, limit, warning, exceeded).
- **CLI** — `agentforge tenants set-plan <id> --plan pro` and `agentforge tenants usage <id>`.

## [0.3.0] — 2026-06-09

### Added
- **Telegram + Discord channel adapters** — async, HMAC-signed webhooks, in-flight send queue with retry/backoff. Connect via `agentforge setup` and a single API key per platform.
- **Email channel adapter** — SMTP send + IMAP poll (via `aiosmtplib`). Polls every 60s by default, configurable via `AGENTFORGE_EMAIL_POLL_INTERVAL`.

## [0.2.0] — 2026-06-09

### Added
- **Workflow engine** — YAML-driven `Workflow.run(state, mailbox, llm, agent_name)` with `receive` / `llm_call` / `respond` step types. Per-step retry with exponential backoff. Custom step types plug in via `register_step_type("name", handler)`.
- **State** — `agentforge.workflows.State` dataclass carries variables between steps; supports `{{ step_id.output_key }}` template substitution.
- **Webhook channel adapter** — HTTP send + receive, with HMAC signature verification on incoming webhooks.

## [0.1.0] — 2026-06-09

### Added
- **Library skeleton** — `agentforge.core.Message` dataclass, side-effect-free import.
- **Adapter framework** — base ABCs for LLM providers and channel adapters.
- **LLM provider abstraction** — `BaseOpenAICompatLLMAdapter` shared interface. Three implementations: `OpenRouterAdapter`, `MiniMaxAdapter`, `OllamaAdapter` (async, with retry/backoff/Retry-After).
- **Mailbox protocol + FileMailbox** — atomic file-based transport (tempfile + os.replace), JSON self-healing on parse error, path-traversal protection (`Path.resolve()` + `is_relative_to()` check).
- **CLI skeleton** — `init`, `run --watch`, `serve` (FastAPI on `127.0.0.1:8765`), `tenants add|list|remove`, `status`.
- **Systemd unit template** — `contrib/systemd/agentforge@.service` (one daemon per agent).
- **CI** — GitHub Actions test workflow across Python 3.10, 3.11, 3.12.

[0.11.0]: https://github.com/Cole666hs/agent-forge/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/Cole666hs/agent-forge/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Cole666hs/agent-forge/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/Cole666hs/agent-forge/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/Cole666hs/agent-forge/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/Cole666hs/agent-forge/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/Cole666hs/agent-forge/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/Cole666hs/agent-forge/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Cole666hs/agent-forge/compare/v0.5.6...v0.6.0
[0.5.6]: https://github.com/Cole666hs/agent-forge/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/Cole666hs/agent-forge/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/Cole666hs/agent-forge/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/Cole666hs/agent-forge/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/Cole666hs/agent-forge/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/Cole666hs/agent-forge/compare/v0.4.0...v0.5.1
[0.4.0]: https://github.com/Cole666hs/agent-forge/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Cole666hs/agent-forge/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Cole666hs/agent-forge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Cole666hs/agent-forge/releases/tag/v0.1.0

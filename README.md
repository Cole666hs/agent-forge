# agentforge

> **Self-hosted multi-agent orchestration library.**
> Refactored from the production-proven `mailbox-llm-bridge` codebase into a clean library/daemon split, ready to be packaged as a commercial SaaS.

[![tests](https://github.com/Cole666hs/agent-forge/actions/workflows/test.yml/badge.svg)](https://github.com/Cole666hs/agent-forge/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## What's in the box

- **Mailbox** (`agentforge.core.FileMailbox`) — atomic file-based transport, JSON self-healing, path-traversal protection, **multi-tenant** (`tenant_id` argument scopes all paths)
- **3 LLM providers** (`OpenRouter`, `MiniMax`, `Ollama`) via the `BaseOpenAICompatLLMAdapter` — async, with retry/backoff/Retry-After
- **4 channel adapters** (`Webhook`, `Telegram`, `Discord`, `Email`) — all async, HMAC-signed webhooks
- **YAML workflow engine** (`agentforge.workflows.Workflow`) — `receive` / `llm_call` / `respond` step types, SQLite state persistence with **tenant scoping**, per-step retry
- **SQLite-backed state** (`agentforge.state.SQLiteRunStore`) — durable run records, `INSERT OR REPLACE` for safe seeding, queryable by run id and workflow
- **Multi-tenant API server** (`agentforge serve`) — FastAPI on `127.0.0.1:8765`, `X-API-Key` auth, tenant-scoped mailbox + workflows, paginated list endpoints with `X-Has-More`
- **Run cancellation** (v0.8.0) — POST `/v1/runs/{id}/cancel` kills an active workflow mid-flight; ownership-checked (only the tenant that started the run can cancel it); audit-logged
- **Real-time event stream** (v0.7.0) — WebSocket `/v1/runs/{id}/stream` pushes step-by-step events as the workflow runs
- **Tenant registry** (`agentforge.tenants.TenantRegistry`) — JSON-backed, keys stored as SHA-256 hashes
- **Observability** (`agentforge.observability`) — structured JSON logging, Prometheus `/metrics`, `/readyz`, request-ID propagation, instrumented mailbox / workflow / LLM
- **MCP server** (v0.11.0, opt-in `[mcp]` extra) — exposes `agentforge` to Claude Desktop, Cursor, and any MCP-aware client over stdio. Five tools: `list_workflows`, `list_runs`, `show_run`, `run_workflow`, `cancel_run`
- **CLI** (`agentforge`) — `init` / `run --watch` / `serve` / `tenants add|list|remove` / `runs ls|show|cancel` / `mcp serve` / `status`
- **Hardened systemd unit** (`contrib/systemd/agentforge@.service`) — one daemon per agent

**391 tests grün** across 30+ commits. Library import is side-effect-free.

## Quick start

```bash
# Install
git clone https://github.com/Cole666hs/agent-forge.git
cd agent-forge
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"

# Scaffold a project
agentforge init mybot
cd mybot

# Configure env (fill in your API key)
cp .env.example .env
$EDITOR .env

# Run a workflow (one-shot)
agentforge run workflow.yaml --agent mybot

# LLM provider is auto-detected from env (OPENROUTER_API_KEY / MINIMAX_API_KEY
# win, ollama serve is the fallback). Force a specific provider with --llm:
agentforge run workflow.yaml --agent mybot --llm openrouter
agentforge run workflow.yaml --agent mybot --llm ollama

# Or run continuously (poll inbox every 5s, systemd-friendly)
agentforge run workflow.yaml --agent mybot --watch
```

## Library usage (programmatic)

```python
import agentforge
from agentforge.workflows import State

# Mailbox
mbox = agentforge.FileMailbox(root="/var/lib/agentforge/mailbox")
mbox.send(agentforge.Message(from_="alice", to="bob", content="hi"))

# LLM (async)
llm = agentforge.make_provider("ollama")
text = await llm.chat("you are helpful", "what is 2+2?")

# Workflow
wf = agentforge.Workflow.from_yaml("workflow.yaml")
state = await wf.run(state=State(), mailbox=mbox, llm=llm, agent_name="mybot")
```

## MCP server (v0.11.0)

Expose `agentforge` to Claude Desktop, Cursor, and any MCP-aware client over stdio. The MCP server is a thin transport wrapper — the daemon stays the single source of truth for active runs, audit log, and run history.

```bash
# Install with MCP support (opt-in extra keeps the base install small)
pip install -e ".[mcp]"

# Configure the daemon connection (env or flags)
export AGENTFORGE_DAEMON_URL=http://127.0.0.1:8765
export AGENTFORGE_API_KEY=afk_...

# Run the server
agentforge mcp serve
```

**Five tools, all backed by the daemon's HTTP API:**

| Tool | What it does |
|---|---|
| `list_workflows` | List YAML workflow files visible to the calling tenant |
| `list_runs` | Paginated run history (newest first) |
| `show_run` | Full run record including step-level events |
| `run_workflow` | Start a workflow; returns the run id |
| `cancel_run` | Stop an in-flight workflow (ownership-checked) |

The server reads `AGENTFORGE_DAEMON_URL` and `AGENTFORGE_API_KEY` (or `--daemon-url` / `--api-key`) and proxies each tool call to the corresponding `/v1/...` endpoint. No second SQLite connection, no duplicate state.

**Claude Desktop / Cursor config:**

```json
{
  "mcpServers": {
    "agentforge": {
      "command": "agentforge",
      "args": ["mcp", "serve"],
      "env": {
        "AGENTFORGE_DAEMON_URL": "http://127.0.0.1:8765",
        "AGENTFORGE_API_KEY": "afk_..."
      }
    }
  }
}
```

## Runs & cancellation (v0.8.0 – v0.10.0)

Every workflow invocation produces a `RunRecord` (id, workflow, tenant, agent, started_at, ended_at, status, duration_seconds, error) persisted in `agentforge.state.SQLiteRunStore`. Records are queryable by id, by workflow, and across all workflows for a tenant.

**Cancellation** (v0.8.0): `POST /v1/runs/{id}/cancel` marks the run as cancelled and signals the executing workflow to stop at the next step boundary. Ownership-checked (only the tenant that started the run can cancel it). The audit log records who cancelled and when.

**WebSocket event stream** (v0.7.0): `WS /v1/runs/{id}/stream` pushes step-by-step events as the workflow runs. The same stream powers the dashboard's run-detail page and any external consumer that wants real-time visibility.

**CLI** (v0.9.0 – v0.10.0):

```bash
# List recent runs (tenant-scoped via the same X-API-Key as the daemon)
agentforge runs ls
agentforge runs ls --workflow echo-bot --limit 20

# Show full detail for one run (status, duration, error, step events)
agentforge runs show run_2026-06-15_a1b2c3

# Cancel an in-flight run
agentforge runs cancel run_2026-06-15_a1b2c3
```

All three subcommands are SSH-friendly (no TTY required) and reuse the same `AGENTFORGE_DAEMON_URL` / `AGENTFORGE_API_KEY` config as the rest of the CLI.

**Pagination:** list endpoints return `X-Has-More: true` when more results are available, plus `X-Next-Offset` for the next page. The CLI passes `--limit` to control page size (default 50).

## Workflow format

```yaml
name: echo-bot
steps:
  - id: receive
    type: receive
  - id: think
    type: llm_call
    inputs:
      system: "You are a helpful assistant."
      user: "{{ receive.content }}"
      output_key: think
  - id: respond
    type: respond
    inputs:
      to: "{{ receive.from }}"
      content: "{{ think }}"
```

Custom step types plug in via `register_step_type("name", handler)`.

## Architecture

```
src/agentforge/
  core/            — FileMailbox, Message (pure data + atomic IO)
  adapters/        — base ABCs + 3 LLMs + 4 channels
  workflows/       — YAML engine + State + step registry
  state/           — SQLite-backed run store (tenants, usage, runs)
  observability/   — logging, metrics, middleware, instrumentation
  tenants/         — TenantRegistry
  billing/         — plans + quota enforcement
  dashboard/       — Jinja2 + HTMX self-hosted UI
  serve.py         — FastAPI HTTP server (REST + WebSocket)
  mcp.py           — MCP stdio server exposing agentforge as tools
  cli.py           — Click CLI
contrib/
  systemd/         — hardened per-agent service unit
docs/
  plans/           — implementation plans (9 phases shipped, see CHANGELOG)
```

The library deliberately avoids greenfield decisions: every component
is in production already (3+ months on HAMILLER, refactored into a
clean shape rather than reinvented).

## Observability

### Structured JSON logging

`agentforge` emits structured JSON logs when `AGENTFORGE_LOG_FORMAT=json`:

```bash
AGENTFORGE_LOG_FORMAT=json agentforge serve
# {"ts":"2026-06-10T13:45:01+00:00","level":"INFO","logger":"agentforge.serve","msg":"agentforge serving on http://127.0.0.1:8765","request_id":"req_a1b2c3"}
```

The request ID is automatically attached from the `X-Request-Id` request header (or generated as `req_<12hex>` if absent) and echoed on the response. All log lines emitted during the request share that `request_id` — pipe the JSON output into Loki, Datadog, or a plain `jq` filter to trace a request through the whole stack.

CLI flags take precedence over env vars:

```bash
agentforge --log-format json --log-level DEBUG serve
agentforge --log-format text run workflow.yaml --agent mybot
```

### Metrics

`GET /metrics` returns Prometheus text format, no auth (same as `/health`):

```bash
$ curl http://127.0.0.1:8765/metrics
# HELP agentforge_mailbox_messages_total Total messages written/read from mailbox
# TYPE agentforge_mailbox_messages_total counter
agentforge_mailbox_messages_total{tenant="acme",direction="sent"} 42.0
# HELP agentforge_llm_call_duration_seconds LLM call latency in seconds
# TYPE agentforge_llm_call_duration_seconds histogram
agentforge_llm_call_duration_seconds_bucket{provider="OpenRouterAdapter",le="0.5"} 3.0
agentforge_llm_call_duration_seconds_bucket{provider="OpenRouterAdapter",le="+Inf"} 3.0
agentforge_llm_call_duration_seconds_sum{provider="OpenRouterAdapter"} 1.42
agentforge_llm_call_duration_seconds_count{provider="OpenRouterAdapter"} 3.0
# ...
```

Metrics currently exported (all with appropriate labels):

- `agentforge_mailbox_messages_total{tenant, direction}` — counter (sent|received)
- `agentforge_mailbox_send_duration_seconds{tenant}` — histogram
- `agentforge_mailbox_list_duration_seconds{tenant}` — histogram
- `agentforge_workflow_runs_total{workflow, outcome}` — counter (success|error)
- `agentforge_workflow_run_duration_seconds{workflow}` — histogram
- `agentforge_llm_calls_total{provider, outcome}` — counter
- `agentforge_llm_call_duration_seconds{provider}` — histogram
- `agentforge_llm_tokens_total{provider, direction}` — counter (in|out)

Metrics are in-memory only — they reset on process restart. Use Prometheus to scrape every 15-30s and store the history. For multi-process deployments, switch to `prometheus_client` with multiproc-dir mode (not needed in the current single-process serve model).

### Health checks

- `GET /health` — liveness (200 if process is up, no auth)
- `GET /readyz` — readiness (200 if mailbox-root is writable AND tenants.json is readable, 503 otherwise with reasons in JSON body)

Use `/health` for "is the process alive" and `/readyz` for "should we route traffic here":

```bash
$ curl http://127.0.0.1:8765/readyz
{"status":"ready"}

$ curl http://127.0.0.1:8765/readyz
{"status":"not_ready","reasons":["mailbox root missing: /var/lib/agentforge/mailbox"]}
```

### Configuration

| Env var | Default | CLI flag | Purpose |
|---|---|---|---|
| `AGENTFORGE_LOG_FORMAT` | `text` | `--log-format` | `json` or `text` |
| `AGENTFORGE_LOG_LEVEL` | `INFO` | `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

### Programmatic usage

```python
from agentforge.observability.logging import configure_logging
from agentforge.observability.metrics import get_registry
from agentforge.observability.instrumentation import (
    instrument_mailbox, instrument_workflow, instrument_llm,
)

# Configure JSON logging
configure_logging(fmt="json", level="INFO")

# Register a custom counter for your app
registry = get_registry()
my_counter = registry.counter("my_app_events_total", "Custom events")
my_counter.inc()

# Render for /metrics
print(registry.render())
```

## Dashboard

Self-hosted web UI for managing tenants, workflows, and usage. FastAPI + Jinja2 + HTMX. No JavaScript framework, no SPA, no build step. Open <http://localhost:8765/dashboard/> after starting `agentforge serve`.

**Login:** paste an API key (the one shown by `agentforge tenants add <id>`). The server sets a `HttpOnly` cookie for the session; the API key never leaves the server except in the request header.

**Pages:**

- **Overview** (`/dashboard/`) — tenant ID, plan badge, quota bar (color-coded: green / yellow at 80%+ / red at 100%+), workflow count
- **Tenants** (`/dashboard/tenants`) — list with plan + usage; create new tenant; delete (with confirm)
- **Tenant detail** (`/dashboard/tenants/{id}`) — full quota detail + plan switcher (free / pro / enterprise) + one-time API key display on creation
- **Workflows** (`/dashboard/workflows`) — list of `.yaml` files in the workflows dir
- **Workflow detail** (`/dashboard/workflows/{name}`) — raw YAML view + "Run via API" form
- **New workflow** (`/dashboard/workflows/new`) — form with name + YAML textarea, pre-filled with a starter template
- **Edit workflow** (`/dashboard/workflows/{name}/edit`) — same form, pre-filled with the current YAML; save overwrites
- **Run history** (`/dashboard/workflows/{name}/runs`) — table of past runs (id, agent, status, duration, started, error) auto-refreshes every 5s via HTMX polling

**Workflow editor (v0.5.2):** full create / edit / save / delete cycle in the browser. Server-side YAML validation rejects empty content, syntax errors, non-mapping roots, and missing `name` keys — invalid saves return `400` with the parser error rendered in the page (no half-written files). Writes are atomic (`tempfile` + `os.replace`) so a crash mid-save never leaves a broken file. No syntax highlighting yet (plain `<textarea>`) and no locking (last-write-wins); both are follow-ups.

**CodeMirror editor (v0.5.3):** the YAML textarea is enhanced with CodeMirror 6 (loaded from esm.sh CDN) — syntax highlighting, line numbers, and YAML language mode. The form still works without JS (plain textarea fallback). Real-browser verification needed for the actual highlighting.

**Run history (v0.5.4):** every `/v1/workflows/{name}/run` call records a `RunRecord` (id, workflow, tenant, agent, started_at, ended_at, status, duration_seconds, error) to `runs.json`. Per-workflow cap of 100 most recent runs. Dashboard page polls `/partials/runs/{name}` every 5s; rows are color-coded by status (green=success, red=error, yellow=quota_exceeded). Out of scope: per-run log streaming, span/trace correlation, retention policies.

**OpenTelemetry OTLP/HTTP export (v0.5.5):** the same `MetricsRegistry` that powers `/metrics` can also push to a real OTLP/HTTP collector. Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318` (standard OTel env var) before starting `agentforge serve`; a daemon thread then POSTs JSON to `${endpoint}/v1/metrics` every 30 seconds. Counter values and histogram bucket counts are encoded in the standard OTLP shape (cumulative temporality, no exemplars). No `opentelemetry-*` package dependency — hand-rolled exporter keeps the install footprint small. Push failures are logged and never crash the agent. Out of scope: OTLP traces/logs, push intervals <30s without code change, exemplars, delta-temporality.

**Tech:** Jinja2 templates render server-side; HTMX is loaded from a CDN for the few interactions (mostly just `<form>` posts — the dashboard is functional even with JS disabled). CSS is self-contained (`src/agentforge/dashboard/static/dashboard.css`), no Tailwind, no preprocessor.

**Real-time updates (v0.5.1):** the quota card on the Overview page and the tenant rows on the Tenants page auto-refresh every 5 seconds via HTMX polling (`hx-get` + `hx-trigger="every 5s"` + `hx-swap="innerHTML"`). The polled endpoints return HTML fragments only (`/dashboard/partials/usage`, `/dashboard/partials/tenants`) — no layout, no `<html>` wrapper, just the bit that changed. No WebSocket infrastructure needed.

**Self-hosted scope (v0.5.1):** polling is 5-second resolution (faster would mean more requests, slower feels stale); no per-row updates (the whole `<tbody>` re-renders); no push notifications (browser tab must be open); no WebSocket streaming (planned for a future version if 5s polling proves insufficient).

## Billing & quota

Per-tenant monthly token quota. Three plan tiers; soft warning at 80% usage; hard block at 100% (raises `QuotaExceededError` from the LLM adapter).

| Plan        | Monthly tokens | Use case                          |
|-------------|---------------:|-----------------------------------|
| `free`      |        100,000 | local dev, small experiments      |
| `pro`       |     10,000,000 | production agents                 |
| `enterprise`|         unlimited | paying customers, custom SLAs   |

**Limits are calendar-month based (UTC).** The counter resets on the 1st of each month; rolled-over entries are detected lazily on read.

**Set a plan** (CLI):

```bash
agentforge tenants set-plan acme --plan pro
```

**Check usage** (CLI):

```bash
agentforge tenants usage acme
# tenant:    acme
# plan:      pro
# used:      42,000 tokens
# limit:     10,000,000 tokens
# remaining: 9,958,000 tokens
# percent:   0.4%
```

**Check usage** (API, requires `X-API-Key`):

```bash
curl -H "X-API-Key: $KEY" http://localhost:8765/v1/tenants/acme/usage
# {"tenant_id":"acme","plan":"pro","used":42000,"limit":10000000,
#  "remaining":9958000,"pct":0.0042,"warning":false,"exceeded":false}
```

**HTTP responses** to `POST /v1/messages` include informational quota headers:

```
X-Quota-Used: 0
X-Quota-Limit: 100000
X-Quota-Warning: false
X-Quota-Exceeded: false
```

**Wiring in the CLI**: when you run a workflow with `--tenant <id>`, the LLM provider is instrumented with `enforce_quota()`. The first call that would push the tenant over their limit raises `QuotaExceededError`, which the CLI surfaces as a clean error.

**Self-hosted tier scope (v0.4.0)**: no payment provider, no email notifications, no per-tenant custom limits, no usage history beyond the current month. These are out of scope for the self-hosted edition — the cloud tier is a separate plan.

## Roadmap (next milestones)

**Since the last cut:** WebSocket streaming (v0.7.0), run cancellation (v0.8.0), pagination, metrics page, CLI `runs` subcommand (v0.9.0), `runs cancel` (v0.10.0), and the MCP server (v0.11.0) all shipped.

**Open items:**

- **Log shipping** (Loki / Datadog / Vector) — structured JSON logging is already there (see Observability), just needs a sink config
- **Multi-process metrics** — switch to `prometheus_client` multiproc-dir mode for setups where multiple `agentforge serve` processes share a host
- **Stripe integration for cloud tier** — per-tenant subscription state, webhook for plan upgrades/downgrades, dunning emails
- **Workflow versioning + diff view** — store every saved workflow with a hash; show diffs in the editor before save
- **Per-run log streaming** — `/v1/runs/{id}/logs` HTTP endpoint, tail-style SSE for long-running workflows
- **Retention policies** — bound `runs.json` / `runs.db` size, auto-prune after N days
- **Dark mode** — dashboard CSS variable toggle
- **Mobile-first responsive UI** — overview/tenants/workflows pages currently target ≥1024px width

These were identified by the HAMILLER and NEMESIS cross-reviews and the v0.5–v0.11 review pass. Each is a multi-day project; not in this MVP cut.

## License

MIT

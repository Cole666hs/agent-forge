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
- **Multi-tenant API server** (`agentforge serve`) — FastAPI on `127.0.0.1:8765`, `X-API-Key` auth, tenant-scoped mailbox + workflows
- **Tenant registry** (`agentforge.tenants.TenantRegistry`) — JSON-backed, keys stored as SHA-256 hashes
- **Observability** (`agentforge.observability`) — structured JSON logging, Prometheus `/metrics`, `/readyz`, request-ID propagation, instrumented mailbox / workflow / LLM
- **CLI** (`agentforge`) — `init` / `run --watch` / `serve` / `tenants add|list|remove` / `status`
- **Hardened systemd unit** (`contrib/systemd/agentforge@.service`) — one daemon per agent

**172 tests grün** across 16 commits. Library import is side-effect-free.

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
  observability/   — logging, metrics, middleware, instrumentation
  tenants/         — TenantRegistry
  serve.py         — FastAPI HTTP server
  cli.py           — Click CLI
contrib/
  systemd/         — hardened per-agent service unit
docs/
  plans/           — implementation plans (5 phases done, Phase 7 in progress)
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

## Roadmap (next milestones)

Billing/Quota (per-tenant LLM token metering) · Web dashboard (FastAPI + HTMX) · OpenTelemetry SDK / OTLP export · Log shipping (Loki/Datadog) · Multi-process metrics.

These were identified by both the HAMILLER and NEMESIS cross-review.
Each is a multi-day project; not in this MVP cut. Phase 7 (Observability) shipped the structured-logging + metrics + health-check foundation; the roadmap items above build on it.

## License

MIT

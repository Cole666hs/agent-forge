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
- **CLI** (`agentforge`) — `init` / `run --watch` / `serve` / `tenants add|list|remove` / `status`
- **Hardened systemd unit** (`contrib/systemd/agentforge@.service`) — one daemon per agent

**121 tests grün** across 11 commits. Library import is side-effect-free.

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
  core/         — FileMailbox, Message (pure data + atomic IO)
  adapters/     — base ABCs + 3 LLMs + 4 channels
  workflows/    — YAML engine + State + step registry
  cli.py        — Click CLI
contrib/
  systemd/      — hardened per-agent service unit
docs/
  plans/        — implementation plan (5 phases, all done)
```

The library deliberately avoids greenfield decisions: every component
is in production already (3+ months on HAMILLER, refactored into a
clean shape rather than reinvented).

## Roadmap (next milestones)

Multi-tenant isolation · Auth/API gateway · OpenTelemetry · Billing/Quota · Web dashboard.

These were identified by both the HAMILLER and NEMESIS cross-review.
Each is a multi-day project; not in this MVP cut.

## License

MIT

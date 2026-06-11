# Example 4 — Webhook Listener

Runs an **HTTP webhook server** that any external service (GitHub, Stripe,
Zapier, your own app) can POST to. The body becomes a `Message` in the
agent's mailbox, the workflow processes it, and the response goes back as
JSON.

## What you'll learn

- Starting a `WebhookChannelAdapter` on a chosen port
- HMAC verification of incoming requests (`X-Signature` header)
- Triggering a workflow from an external HTTP call

## Setup

```bash
export WEBHOOK_PORT=8080
export WEBHOOK_SECRET="generate-a-random-string"
```

The secret is used to compute `HMAC-SHA256(secret, body)` and emit the result
as `X-Signature: sha256=<hex>`. Incoming requests with a wrong signature are
rejected with HTTP 401.

## Run it

```bash
.venv/bin/python examples/04-webhook-listener/run.py
```

Server listens on `http://127.0.0.1:8080/inbox/agent-forge`.

## Smoke test

```bash
# Compute signature
SECRET=test
BODY='{"text":"hello"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -X POST http://127.0.0.1:8080/inbox/agent-forge \
  -H "Content-Type: application/json" \
  -H "X-Signature: sha256=$SIG" \
  -d "$BODY"
```

You'll get back the workflow's response as JSON.

## Files

- `workflow.yaml` — single receive step (workflow runs against the body)
- `run.py` — starts the aiohttp webhook server
- `mailbox/` — created on first run

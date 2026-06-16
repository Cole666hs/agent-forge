# Example 6 — Docker deploy (with optional nginx)

The root-level `Dockerfile` and `docker-compose.yml` (v0.17.0) cover the
basic single-service case. This example adds the **optional nginx
reverse-proxy layer** for TLS termination and rate-limiting. Use this
when you need:

- HTTPS in front of the API
- `client_max_body_size` larger than the 1 MiB default (e.g. for big
  webhook payloads)
- IP-based rate limits

## Files in this example

| File | What it adds over the root `docker-compose.yml` |
|---|---|
| `nginx.conf` | Reverse proxy with WebSocket/SSE support, 16 MB body limit, 300s read timeout |
| `Dockerfile` | Identical to the root one; kept here for self-contained reference |
| `docker-compose.yml` | Adds the `nginx` service (commented out by default) |
| `env.example` | More verbose env-var documentation |

## Quick start (nginx disabled)

```bash
# Use the root-level compose (simpler, no nginx)
cd ../..
cp .env.example .env && $EDITOR .env
docker compose up -d
```

## Quick start (with nginx)

```bash
cd examples/06-docker-deploy
cp env.example .env && $EDITOR .env
# 1. Uncomment the nginx service in docker-compose.yml
# 2. Mount your TLS certs in /etc/nginx/ssl/ (or use Caddy instead)
docker compose up -d
```

The reverse proxy listens on `:80` (and `:443` once you mount certs).
The agentforge service is no longer published on `:8766`; it talks to
nginx over the compose network only.

## Configuration

Edit `.env` (in this example, or in the repo root) to set:

- `AGENTFORGE_ADMIN_TOKEN` — dashboard login (default: `admin`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` — OTLP collector for metrics (optional)
- `AGENTFORGE_LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR

## Files

- `Dockerfile` — slim image based on python:3.11-slim
- `docker-compose.yml` — agentforge + optional nginx + a tiny OTLP collector
- `env.example` — environment variables template
- `nginx.conf` — reverse proxy with WebSocket upgrade and basic TLS
- `data/` — created on first run (bind mount)

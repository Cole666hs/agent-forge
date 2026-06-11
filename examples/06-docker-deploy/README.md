# Example 6 — Docker Deploy

Full stack in Docker: the API server + dashboard, with persistent
volumes for mailbox/state/tenants/usage, and an optional nginx reverse
proxy in front. Use this as a starting point for production.

## What you'll get

- `agentforge` daemon on `http://127.0.0.1:8766`
- Dashboard at `http://127.0.0.1:8766/dashboard`
- Persistent state across restarts (`./data/`)
- Optional nginx proxy on `http://127.0.0.1:80` (uncomment in compose)

## Run it

```bash
cd examples/06-docker-deploy
docker compose up -d

# Verify
docker compose logs -f agentforge

# Create a tenant
docker compose exec agentforge agentforge tenants add acme

# Open the dashboard
xdg-open http://127.0.0.1:8766/dashboard/login
# Default admin token: "admin" (set AGENTFORGE_ADMIN_TOKEN in .env)
```

## Configuration

Edit `env.example` to set:

- `AGENTFORGE_ADMIN_TOKEN` — dashboard login (default: `admin`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` — OTLP collector for metrics (optional)
- `AGENTFORGE_LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR

## Files

- `Dockerfile` — slim image based on python:3.11-slim
- `docker-compose.yml` — agentforge + optional nginx + a tiny OTLP collector
- `env.example` — environment variables template
- `nginx.conf` — reverse proxy with WebSocket upgrade and basic TLS
- `data/` — created on first run (bind mount)

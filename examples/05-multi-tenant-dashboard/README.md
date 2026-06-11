# Example 5 — Multi-Tenant Dashboard

Spin up the FastAPI server with a tenant registry, run a workflow, and
look at the dashboard. Shows how to:

- Create tenants
- Track per-tenant usage against plan limits
- Set a tenant's plan (free / pro / enterprise)
- Inspect quota state via the dashboard UI

## What you'll learn

- `TenantRegistry` + `UsageStore` wiring
- Plan-based quotas (soft warning @80%, hard block @100%)
- The full `agentforge serve` daemon and dashboard

## Run it

```bash
# 1. Start the server
.venv/bin/agentforge serve --port 8766 --log-level INFO &

# 2. Create two tenants
.venv/bin/agentforge tenants add acme
.venv/bin/agentforge tenants add tinyco

# 3. Set the plan
.venv/bin/agentforge tenants set-plan acme pro
.venv/bin/agentforge tenants set-plan tinyco free

# 4. Open the dashboard
xdg-open http://127.0.0.1:8766/dashboard/login
# Default admin token: "admin" (set AGENTFORGE_ADMIN_TOKEN in env to change)

# 5. From another shell, run a workflow against a tenant
.venv/bin/agentforge run examples/05-multi-tenant-dashboard/workflow.yaml \
  --tenant acme --agent demo --mailbox ./mailbox
```

The dashboard's **Usage** card for `acme` will start ticking up. Switch to
`tinyco` (free plan, 1k tokens/month) and after enough calls you'll see the
**X-Quota-Warning** header in API responses, then HTTP 429 once exhausted.

## Files

- `workflow.yaml` — single LLM call against the tenant's quota
- `tenants.json` — created on first tenant add
- `usage.json` — created on first LLM call (per-tenant, per-month)
- `mailbox/` — created on first run

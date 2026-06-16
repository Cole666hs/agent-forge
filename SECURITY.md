# Security

## Reporting a vulnerability

If you find a security issue in `agentforge`, please report it privately:

- **Email:** security@agentforge.local (private; not monitored by a bot)
- **What to include:** description, reproducer (YAML / curl), impact estimate
- **Response time:** we aim to acknowledge within 3 business days

Please do **not** file public GitHub issues for security bugs. We coordinate
fix-and-disclose on a per-case basis.

## Supported versions

Only the latest minor version receives security patches. As of this writing:

| Version | Supported          |
|---------|--------------------|
| v0.16.x | ✅ active          |
| v0.15.x | ❌ end-of-life     |
| < v0.15 | ❌ unsupported     |

Older releases may have known issues that are fixed in newer versions.
Please upgrade before filing a report.

## Threat model

`agentforge` is a self-hosted library + daemon. The threat model assumes:

- **Trusted operator** runs `agentforge serve` on a host they control
- **Tenants** are external callers who authenticate with an API key
- **Workflows** are YAML files on disk that the operator authors or reviews
- **The network** is untrusted (any tenant can reach the daemon's port)

Out of scope for the core product:

- Multi-tenant isolation beyond the API-key boundary (no per-tenant rate
  limit, no per-tenant resource quota in the self-hosted tier)
- Sandbox isolation of arbitrary workflow steps — step handlers execute
  with the daemon's full Python privileges. Review workflow YAML before
  loading.
- Secret management. API keys live in the daemon's process env and the
  tenant registry JSON file. Restrict filesystem permissions accordingly.

## Built-in defenses (v0.16.0)

| Defense | Where | What it does |
|---|---|---|
| API key auth | `require_tenant` dep | Every `/v1/*` endpoint requires `X-API-Key` |
| Tenant isolation | `RunStore.cancel`, `runs list` | Cross-tenant access returns 404 (no existence leak) |
| HMAC signing | `WebhookChannelAdapter` | `X-Signature: sha256=...` on all incoming/outgoing webhook calls |
| Path-traversal guard | `POST /v1/workflows/{name}/run` | `name` must match `^[A-Za-z0-9_-]{1,64}$`; resolved path must stay inside `workflows_dir` |
| Body size limit | `BodySizeLimitMiddleware` | Default 1 MiB; configurable via `AGENTFORGE_MAX_BODY_BYTES` |
| YAML safe-load | `Workflow.from_yaml_text` | `yaml.safe_load` only — no arbitrary Python object construction |
| Audit log | structured JSON | All cancel attempts, cross-tenant rejections, and webhook signature failures are logged at WARNING+ |

## Operator checklist

Before exposing `agentforge serve` to any untrusted network:

1. **Set a strong API key per tenant.** Keys are opaque to the daemon; the
   only constraint is length and randomness. Use the
   `agentforge tenants add` CLI or write them by hand.
2. **Put the daemon behind a reverse proxy** (nginx, Caddy, Traefik) that
   terminates TLS. The daemon itself is HTTP, not HTTPS.
3. **Restrict `workflows_dir` permissions.** Workflow files are loaded
   on every API call; the daemon process needs at least read access.
4. **Restrict `state.db` and `mailbox/` permissions.** These contain
   tenant data including message contents.
5. **Set `AGENTFORGE_MAX_BODY_BYTES`** if your workflows take unusually
   large inputs (or unusually small ones).
6. **Subscribe to releases.** Watch this repo for security tags; we publish
   patches as `v0.X.Y` and reference CVEs in the CHANGELOG.

## Acknowledgments

We credit reporters in the release notes unless they ask to remain anonymous.

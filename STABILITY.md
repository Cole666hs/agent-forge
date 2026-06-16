# Stability & API guarantees

`agentforge` is approaching v1.0.0. This document describes the
stability tiers, the deprecation policy, and what users can rely on
between minor versions.

## Stability tiers

| Tier | What it means | Examples |
|---|---|---|
| **Stable** | Backwards-compatible within the 1.x line. Breaking changes require a 2.0 release and at least one prior minor deprecation. | `agentforge.run()`, the `Workflow.from_yaml()` API, the `POST /v1/workflows/{name}/run` HTTP route, the `state.db` schema |
| **Experimental** | May change in any minor version. Pin your dependency. | `agentforge serve` MCP server, the dashboard UI, OTLP exporter config |
| **Internal** | No guarantees. Importing from these modules is at your own risk. | Anything under `agentforge._*`, dashboard router internals, observability middleware |

## Deprecation policy

When a stable API needs to change:

1. **Announce** in the next minor release: add a `DeprecationWarning`,
   keep the old behavior working, and document in `CHANGELOG.md`.
2. **Deprecate for at least one full minor version.** The deprecation
   must appear in the release notes of the minor that introduced it AND
   the one after.
3. **Remove in a major version bump** (e.g. 1.x → 2.0). Removal in a
   minor is a bug — please open an issue if you see one.

## Public API surface (v0.18.0)

### Stable (v1.0.0 contract)

- `agentforge.Workflow`, `agentforge.State`, `agentforge.Step`
- `agentforge.register_step_type(name, handler)`
- `agentforge.FileMailbox`, `agentforge.Message`, `agentforge.Mailbox`
- `agentforge.TenantRegistry`
- The `X-API-Key` auth header on `/v1/*` routes
- The `state.db` schema (current: SCHEMA_VERSION=3)
- The `workflows/{name}.yaml` on-disk format
- All public CLI commands (`agentforge run`, `serve`, `tenants`, `runs`, `workflows`)
- All `AGENTFORGE_*` env vars (the full list is in `.env.example`)

### Experimental (may change)

- **MCP server** (`agentforge serve --mcp`) — the tool list, the
  parameter shapes, the transport. v1.0.0 will freeze these.
- **Dashboard UI** — the URL routes are stable, but the visual design
  and JS interactions are not.
- **OTLP exporter** — the env-var config is stable; the wire format
  follows OpenTelemetry's own deprecation policy.

### Internal (no contract)

- `agentforge._*` modules
- `agentforge.dashboard.router` internals
- `agentforge.observability.middleware`
- Anything reached via `from agentforge.X import Y` where Y is NOT
  re-exported from `agentforge/__init__.py`

## v1.0.0 promises

When v1.0.0 ships, the following become **stable**:

- Every re-export in `agentforge/__init__.py:__all__`
- Every public CLI subcommand
- Every HTTP route documented in the OpenAPI spec
- The `state.db` schema (with migration path to the next version)
- The `workflows/*.yaml` on-disk format
- The 1.x line of releases will not break these without the deprecation
  policy above

What's **not** in the v1.0.0 contract:

- The dashboard's visual design
- The set of bundled LLM providers (we may add or drop adapters)
- The bundled step types (`record`, `llm_call`, `respond`, etc.) — these
  are the engine's vocabulary; new types will be additive, not removals

## Versioning

`agentforge` follows [SemVer 2.0.0](https://semver.org/):

- **MAJOR** (1.x → 2.x) — incompatible API changes
- **MINOR** (1.0 → 1.1) — backwards-compatible features
- **PATCH** (1.0.0 → 1.0.1) — backwards-compatible bug fixes

`agentforge.__version__` is the single source of truth. `pyproject.toml`,
`agentforge serve`'s OpenAPI document, and the OTel `service_version`
all read from it. A regression test (`tests/unit/test_version_consistency.py`)
fails the build if any of these drift apart.

## When in doubt

- Open a GitHub Discussion before depending on something not listed
  under **Stable**
- Watch the CHANGELOG.md diff at upgrade time
- Pin your `agentforge` version (`agentforge==0.18.0` in `requirements.txt`)

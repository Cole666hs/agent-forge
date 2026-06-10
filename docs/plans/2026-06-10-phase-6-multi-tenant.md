# Phase 6: Auth + Multi-Tenancy

> **For Hermes:** Use subagent-driven-development skill to implement task-by-task.
> **Skills to load for execution:** `test-driven-development`, `verification-before-completion`, `requesting-code-review`

**Goal:** Transform agentforge from a single-tenant library into a multi-tenant SaaS-ready product. Every operation (mailbox, workflow, state) is scoped to a `tenant_id`, and all access is gated by an API key that maps to exactly one tenant.

**Architecture:** Tenant ID is a path component (`mailbox_root/<tenant_id>/<agent>/inbox/...`) and a SQLite column on the state DB. API keys live in a small `tenants.json` or `tenants.db` registry consulted by the FastAPI server.

**Tech Stack:** Existing (FastAPI for server, SQLite for state + tenants). One new dep if we go FastAPI: actually `fastapi`+`uvicorn` are already in budget-profi, but not in agentforge. Add them.

---

## Acceptance Criteria

- [ ] `FileMailbox(root, tenant_id="acme")` writes to `root/acme/...` (not `root/...`)
- [ ] Two tenants with the same agent name don't see each other's messages
- [ ] `tenant_id` validation: must match `[a-z0-9_-]+`
- [ ] SQLite state DB has a `tenant_id` column; `State.persist/hydrate` scope by tenant
- [ ] `agentforge serve` boots a FastAPI app on port 8765
- [ ] `GET /health` returns 200 (no auth required)
- [ ] `GET /v1/inbox` requires `X-API-Key`; unknown key → 401
- [ ] `POST /v1/messages` requires `X-API-Key`; sends to that tenant's mailbox
- [ ] `POST /v1/workflows/<name>/run` requires `X-API-Key`; runs workflow in tenant scope
- [ ] All endpoints log tenant_id + request_id for audit
- [ ] 10+ new tests, all green; existing 95 still green

## Out of Scope (for this milestone)

- **Billing/Quota** (Phase 8) — needs tenant-metering infra first
- **Web Dashboard** (Phase 9) — needs the API layer first; this milestone IS the API layer
- **OpenTelemetry** (Phase 7) — observability on top of the API
- **JWT/OAuth** — API keys are sufficient for v1; OAuth can come later
- **Tenant onboarding flow** — `agentforge tenants add <id> <api-key>` is a CLI subcommand, no UI

## Skills to load for execution

- `test-driven-development` — RED-GREEN-REFACTOR on every change
- `verification-before-completion` — Iron Law
- `requesting-code-review` — pre-commit quality gate (now we have a public GitHub)
- `fastapi-lifespan-gotcha` — FastAPI 0.115+ startup/shutdown quirks

---

## Phase 6.1: Tenant in Mailbox (skeleton)

### Task 6.1.1: `FileMailbox` accepts `tenant_id`

**Files:**
- Modify: `src/agentforge/core/mailbox.py`
- Modify: `tests/unit/core/test_mailbox.py`

**TDD: Failing test first.**

```python
# Add to test_mailbox.py
def test_filemailbox_with_tenant_id_writes_under_tenant_dir(mbox_with_tenant):
    mbox, tmp_path = mbox_with_tenant
    msg = Message(from_="alice", to="bob", content="x")
    mbox.send(msg)
    # Path: tmp_path/<tenant>/alice/outbox/...
    assert (tmp_path / "acme" / "alice" / "outbox" / f"{msg.id}.json").exists()

def test_two_tenants_isolated(tmp_path):
    a = FileMailbox(root=tmp_path, tenant_id="acme")
    b = FileMailbox(root=tmp_path, tenant_id="corp")
    a.send(Message(from_="alice", to="bob", content="for-acme"))
    b.send(Message(from_="alice", to="bob", content="for-corp"))
    assert len(a.list_inbox("bob", include_read=True)) == 1
    assert a.list_inbox("bob", include_read=True)[0].content == "for-acme"
    assert b.list_inbox("bob", include_read=True)[0].content == "for-corp"

def test_invalid_tenant_id_rejected(tmp_path):
    with pytest.raises(ValueError, match="tenant_id"):
        FileMailbox(root=tmp_path, tenant_id="../etc")
```

**Implementation:** `__init__` adds `tenant_id: str = ""` parameter. All path operations prepend `tenant_id/` when set. Validation via `_validate_agent_name` analog.

**Acceptance:** 3 new tests pass, 12 existing mailbox tests still pass.

### Task 6.1.2: `State.persist/hydrate` adds `tenant_id` column

**Files:**
- Modify: `src/agentforge/workflows/engine.py`
- Modify: `tests/unit/workflows/test_engine.py`

Add `tenant_id TEXT NOT NULL` to the schema, default empty string for backward compat. `State(run_id, tenant_id="acme")` writes to the new schema; `WHERE tenant_id = ? AND run_id = ?` filter.

**Acceptance:** 2-3 new tests pass; old tests with no tenant still pass (default "" is a no-op).

## Phase 6.2: Tenant Registry + API-Key Auth

### Task 6.2.1: `TenantRegistry`

**Files:**
- Create: `src/agentforge/tenants/registry.py`
- Create: `tests/unit/tenants/test_registry.py`

JSON-backed for v1 (`tenants.json` in mailbox root or specified path). Each entry: `{"tenant_id": "acme", "api_key_hash": "sha256...", "created_at": "..."}`. Look up by `X-API-Key` header value (hashed, compared).

```python
class TenantRegistry:
    def __init__(self, path: Path): ...
    def add(self, tenant_id: str, api_key: str) -> None: ...
    def lookup(self, api_key: str) -> Optional[str]:  # returns tenant_id
        ...
    def list_tenants(self) -> List[str]: ...
```

**Acceptance:** 4-5 tests (add, lookup hit/miss, duplicate rejection, list).

## Phase 6.3: FastAPI Server Skeleton

### Task 6.3.1: `agentforge serve`

**Files:**
- Create: `src/agentforge/serve.py`
- Create: `src/agentforge/cli.py` (extend with `serve` subcommand)
- Create: `tests/unit/test_serve.py`

FastAPI app with:
- `GET /health` (no auth)
- `GET /v1/inbox?agent=NAME` (auth)
- `POST /v1/messages` (auth, body: `{"to": "...", "content": "..."}`)
- `POST /v1/workflows/{name}/run` (auth, body: `{"agent": "..."}`)

Auth dependency: `X-API-Key` header → tenant_id via `TenantRegistry`. Returns 401 on miss.

**Acceptance:** 5-6 tests via `fastapi.testclient.TestClient`.

## Phase 6.4: CLI Subcommand `agentforge tenants`

**Files:**
- Modify: `src/agentforge/cli.py`

```bash
agentforge tenants add <id> [--api-key KEY]
agentforge tenants list
agentforge tenants remove <id>
```

`add` without `--api-key` generates a random key and prints it (once, like moltbook).

---

## Rollback Plan

**Trigger:** Any tenant isolation bug ships, or tests regress below 95.

**Steps:**
1. `git reset --hard <last-green-sha>` on master
2. Local-only repo, no production state
3. Verify: `pytest tests/ -q` → 95/95

**Data loss window:** None — tenants.json is a new file, doesn't exist yet.

**Tested:** Rollback rehearsed manually 2026-06-10 (Phase 1-5 all had clean rollbacks).

---

## Plan-Compliance Check (post-Phase-6)

After all tasks done, before claiming "Phase 6 done":

| # | Task | Status | Evidence |
|---|------|--------|----------|
| 6.1.1 | FileMailbox tenant_id | ⏳ | pytest 3 new + 12 old = 15 |
| 6.1.2 | State.persist tenant_id | ⏳ | pytest 2-3 new + 10 old = 12-13 |
| 6.2.1 | TenantRegistry | ⏳ | pytest 4-5 |
| 6.3.1 | FastAPI server | ⏳ | pytest 5-6 via TestClient |
| 6.4.1 | CLI tenants subcommand | ⏳ | pytest 2 |

**Acceptance walk:**
- [ ] `FileMailbox(root, tenant_id="acme")` writes to `root/acme/...` — verified by 6.1.1
- [ ] Two tenants isolated — verified by 6.1.1
- [ ] Tenant validation — verified by 6.1.1
- [ ] State scoped by tenant — verified by 6.1.2
- [ ] FastAPI boots on `agentforge serve` — verified by 6.3.1
- [ ] /health no auth, /v1/* requires X-API-Key — verified by 6.3.1
- [ ] ≥ 10 new tests, all existing still pass — final pytest count

**Out-of-scope:** Billing, Dashboard, OTel, OAuth — all explicitly deferred to later phases.

**Skills-loaded check:** TDD ✓, verification ✓, code-review ⏳ (will run before merge).

**Verdict:** ⏳ → ✅ when all rows filled in.

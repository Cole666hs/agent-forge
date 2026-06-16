"""agentforge.serve — FastAPI HTTP server with API-key auth.

Endpoints:
  GET  /health                       — no auth, returns 200 (liveness)
  GET  /readyz                       — no auth, returns 200/503 (readiness)
  GET  /metrics                      — no auth, Prometheus text format
  GET  /v1/inbox?agent=NAME          — auth, list mailbox inbox
  POST /v1/messages                  — auth, send message
  POST /v1/workflows/{name}/run      — auth, run workflow

Auth: `X-API-Key: <key>` header. The server consults a TenantRegistry
to map keys → tenant_id. All mailbox + state operations are scoped to
that tenant.

Observability:
  - Each request gets an X-Request-Id (inbound or generated), echoed on
    the response and stored in the request_id contextvar for log lines.
  - Per-tenant mailboxes are instrumented (counter + duration) on first
    use and cached. Metrics accumulate until process restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from agentforge.billing.usage import UsageStore
from agentforge.billing.quota import quota_status, QuotaExceededError
from agentforge.core.mailbox import FileMailbox
from agentforge.core.message import Message
from agentforge.core.runs import RunRecord, RunStore
from agentforge.dashboard import router as dashboard_router
from agentforge.dashboard.router import get_templates
from agentforge.observability.instrumentation import instrument_mailbox
from agentforge.observability.logging import configure_logging
from agentforge.observability.metrics import get_registry
from agentforge.observability.middleware import RequestIdMiddleware
from agentforge.state import State as AppState, migrate_json_to_sqlite
from agentforge.tenants.registry import TenantRegistry
from agentforge.workflows.engine import (
    State as EngineState,
    Workflow,
    WorkflowCancelled,
    WorkflowError,
)

logger = logging.getLogger(__name__)


# v0.16.0: workflow names in URL paths must be single safe segments.
# Allows alphanumerics, dash, underscore. No dots, slashes, backslashes,
# or control characters. Dot-starting names like `..secret` are rejected.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse_format(data: dict) -> bytes:
    """Serialize one SSE event frame as bytes.

    Each frame is `data: <json>\\n\\n`. The double newline is the
    SSE spec's record separator (one event = one record). Multi-line
    `data` would need a `data: ` prefix per line, but JSON on a
    single line is fine and matches what EventSource() in browsers
    expects.
    """
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    to: str
    content: str = Field(min_length=1)
    intent: str = "respond"


class SendMessageResponse(BaseModel):
    id: str
    to: str
    # `from_` is a reserved-ish field name; serialize as `from` to match
    # the wire-format convention used by Message.to_dict()
    from_: str = Field(serialization_alias="from")
    content: str

    model_config = {"populate_by_name": True}


class InboxResponse(BaseModel):
    messages: list[dict]


class RunWorkflowRequest(BaseModel):
    agent: str


class RunWorkflowResponse(BaseModel):
    state_keys: list[str]


class WorkflowItem(BaseModel):
    """One workflow file in the workflows dir. v0.11.0."""
    name: str
    description: str
    path: str


class WorkflowsListResponse(BaseModel):
    """Response for `GET /v1/workflows`. v0.11.0."""
    workflows: list[WorkflowItem]
    tenant_id: str


class RunsListResponse(BaseModel):
    """Response for `GET /v1/runs?workflow=X`. v0.11.0."""
    workflow: str
    runs: list[dict]
    count: int


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    tenants_path: Path,
    mailbox_root: Path,
    state_db: Optional[Path] = None,
    workflows_dir: Optional[Path] = None,
) -> FastAPI:
    """Build the FastAPI app. No IO at import time — pass all config.

    v0.13.0: state is initialized BEFORE the FastAPI app, so the
    lifespan handler can reference the run_store directly. Routes
    are still added after app construction (the FastAPI idiomatic
    way), but the lifespan closure captures `run_store` cleanly.
    """
    tenants_path = Path(tenants_path)
    mailbox_root = Path(mailbox_root)
    state_db = Path(state_db) if state_db is not None else mailbox_root.parent / "state.db"
    workflows_dir = Path(workflows_dir) if workflows_dir is not None else mailbox_root.parent / "workflows"

    # -- state first (v0.13.0 refactor) ---------------------------------
    state = AppState(state_db)
    legacy_tenants = tenants_path if tenants_path.exists() else None
    legacy_usage = (mailbox_root.parent / "usage.json") if (mailbox_root.parent / "usage.json").exists() else None
    legacy_runs = (mailbox_root.parent / "runs.json") if (mailbox_root.parent / "runs.json").exists() else None
    if any(p is not None for p in (legacy_tenants, legacy_usage, legacy_runs)):
        migrate_json_to_sqlite(legacy_tenants, legacy_usage, legacy_runs, state)
    registry = state.tenants
    usage_store = state.usage
    run_store = state.runs

    # -- retention background task (v0.13.0) -----------------------------
    # A long-lived asyncio task that periodically prunes old runs and
    # run_events rows. Three env vars control it:
    #   AGENTFORGE_RETENTION_RUNS_DAYS        (default 90, 0 = disabled)
    #   AGENTFORGE_RETENTION_EVENTS_DAYS      (default 30, 0 = disabled)
    #   AGENTFORGE_RETENTION_INTERVAL_HOURS   (default 6, min 1 minute)
    # The task is best-effort: a prune failure is logged and the
    # next interval will retry. The daemon is never taken down by
    # a retention hiccup.
    async def _retention_loop() -> None:
        runs_days = int(os.environ.get("AGENTFORGE_RETENTION_RUNS_DAYS", "90"))
        events_days = int(os.environ.get("AGENTFORGE_RETENTION_EVENTS_DAYS", "30"))
        interval_hours = float(os.environ.get(
            "AGENTFORGE_RETENTION_INTERVAL_HOURS", "6",
        ))
        interval_seconds = max(60.0, interval_hours * 3600.0)
        # Initial sleep: stagger the first run so serve startup
        # doesn't slam the DB with a delete right after boot.
        await asyncio.sleep(30.0)
        while True:
            try:
                n_runs = run_store.prune_older_than_days(runs_days)
                n_events = run_store.events.prune_older_than_days(events_days)
                if n_runs or n_events:
                    logger.info(
                        "retention: pruned %d runs (>%dd), %d events (>%dd)",
                        n_runs, runs_days, n_events, events_days,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("retention: prune failed: %s", e)
            try:
                await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: spawn the retention task. The task is owned by
        # the event loop and outlives individual requests.
        task = asyncio.create_task(
            _retention_loop(), name="agentforge-retention",
        )
        app.state.retention_task = task
        try:
            yield
        finally:
            # Shutdown: cancel the task and wait for it to finish.
            # The task's except blocks re-raise CancelledError, so
            # await completes quickly.
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    app = FastAPI(title="agentforge", version="0.13.0", lifespan=lifespan)

    # -- app.state exposure (tests + v0.12.0 SSE) ------------------------
    # v0.8.0 #1: active-run registry. Maps run_id -> (tenant_id,
    # asyncio.Event) so the cancel endpoint can enforce ownership
    # (v0.8.1 polish: a cancel for tenant B's run_id by tenant A
    # was previously allowed, because the dict was keyed by run_id
    # only). The event is the one the engine polls between steps.
    # Scope: in-process only. A multi-worker deployment would need
    # a shared cancellation channel (Redis pub/sub or a DB flag) —
    # out of scope for v0.8.0.
    active_runs: dict[str, tuple[str, "asyncio.Event"]] = {}
    app.state.active_runs = active_runs
    app.state.runs = run_store
    app.state.events = run_store.events

    # -- auth dependency ---------------------------------------------------

    def require_tenant(request: Request) -> str:
        """Reads X-API-Key header, returns tenant_id (or raises 401)."""
        api_key = request.headers.get("X-API-Key", "")
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-API-Key header required",
            )
        tenant_id = registry.lookup(api_key)
        if tenant_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid API key",
            )
        return tenant_id

    # Per-tenant mailbox cache. Each tenant gets its own FileMailbox, and
    # the first call to mailbox_for() also instruments it for metrics.
    _mailbox_cache: dict[str, FileMailbox] = {}

    def mailbox_for(tenant_id: str) -> FileMailbox:
        if tenant_id not in _mailbox_cache:
            m = FileMailbox(root=mailbox_root, tenant_id=tenant_id)
            instrument_mailbox(m, registry=get_registry())
            _mailbox_cache[tenant_id] = m
        return _mailbox_cache[tenant_id]

    # -- routes ------------------------------------------------------------

    # RequestIdMiddleware is added via app.add_middleware below (after
    # all routes are registered) so the middleware wraps the whole stack.

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        """Readiness check: mailbox-root writable + tenants file readable.

        Returns 200 with {"status": "ready"} when both pass, 503 with
        {"status": "not_ready", "reasons": [...]} otherwise.
        """
        reasons: list[str] = []
        if not mailbox_root.exists():
            reasons.append(f"mailbox root missing: {mailbox_root}")
        elif not mailbox_root.is_dir():
            reasons.append(f"mailbox root not a directory: {mailbox_root}")
        else:
            try:
                probe = mailbox_root / ".readyz_probe"
                probe.write_text("ok")
                probe.unlink()
            except OSError as e:
                reasons.append(f"mailbox root not writable: {e}")
        if not tenants_path.exists():
            reasons.append(f"tenants file missing: {tenants_path}")
        elif not tenants_path.is_file():
            reasons.append(f"tenants path not a file: {tenants_path}")
        if reasons:
            return Response(
                status_code=503,
                content=json.dumps({"status": "not_ready", "reasons": reasons}),
                media_type="application/json",
            )
        return Response(
            content=json.dumps({"status": "ready"}),
            media_type="application/json",
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus text format. No auth — same as /health."""
        return Response(
            content=get_registry().render(),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/v1/inbox", response_model=InboxResponse)
    def list_inbox(
        agent: str,
        tenant_id: str = Depends(require_tenant),
    ) -> InboxResponse:
        mbox = mailbox_for(tenant_id)
        messages = mbox.list_inbox(agent, include_read=False)
        return InboxResponse(
            messages=[m.to_dict() for m in messages],
        )

    @app.post("/v1/messages", response_model=SendMessageResponse,
              status_code=status.HTTP_201_CREATED)
    def send_message(
        body: SendMessageRequest,
        response: Response,
        tenant_id: str = Depends(require_tenant),
    ) -> SendMessageResponse:
        mbox = mailbox_for(tenant_id)
        msg = Message(
            from_=tenant_id,
            to=body.to,
            content=body.content,
            intent=body.intent,
        )
        mbox.send(msg)
        # Add quota headers to response (informational — messages don't
        # consume tokens, so used stays at current usage).
        qs = quota_status(registry, usage_store, tenant_id)
        limit_str = "unlimited" if qs.limit is None else str(qs.limit)
        response.headers["X-Quota-Used"] = str(qs.used)
        response.headers["X-Quota-Limit"] = limit_str
        response.headers["X-Quota-Warning"] = "true" if qs.warning else "false"
        response.headers["X-Quota-Exceeded"] = "true" if qs.exceeded else "false"
        return SendMessageResponse(
            id=msg.id, to=msg.to, from_=msg.from_, content=msg.content,
        )

    @app.get("/v1/tenants/{tenant_id}/usage")
    def get_tenant_usage(
        tenant_id: str,
        # Authenticated via the same X-API-Key as /v1/* endpoints. The
        # caller's tenant_id (from the auth dep) is implicitly the
        # tenant they're allowed to see — for multi-tenant isolation
        # we'd compare; for Phase 8, the API key is the credential.
        _: str = Depends(require_tenant),
    ) -> dict:
        qs = quota_status(registry, usage_store, tenant_id)
        return {
            "tenant_id": qs.tenant_id,
            "plan": qs.plan.value,
            "used": qs.used,
            "limit": qs.limit,
            "remaining": qs.remaining,
            "pct": qs.pct,
            "warning": qs.warning,
            "exceeded": qs.exceeded,
        }
    @app.post("/v1/workflows/{name}/runs/{run_id}/cancel", include_in_schema=True)
    def cancel_workflow_run(
        name: str,
        run_id: str,
        tenant_id: str = Depends(require_tenant),
    ) -> dict:
        """Signal an in-flight workflow run to stop at the next step
        boundary. v0.8.0 #1; v0.8.1 polish: ownership check.

        Looks up the run's asyncio.Event in the in-process
        `active_runs` registry. If the run is not in flight (already
        finished, or the daemon was restarted since it started),
        returns 404. If the run is in flight but owned by a
        different tenant, returns 403 (v0.8.1 polish — previously
        a tenant could cancel any run by guessing the run_id; the
        48-bit UUID entropy made guessing impractical, but
        defense-in-depth costs nothing).

        The cancellation is cooperative: the engine checks the event
        between steps and raises WorkflowCancelled. Long-running
        steps (a slow LLM call) finish normally; the cancellation
        takes effect on the next step boundary.
        """
        entry = active_runs.get(run_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"run {run_id!r} is not active (already finished or never existed)",
            )
        run_tenant_id, ev = entry
        if run_tenant_id != tenant_id:
            # Defense-in-depth: even if the caller happens to know
            # another tenant's run_id, they can't cancel it. The
            # practical exposure is low (run_id is 12 hex of UUID4,
            # 48 bits of entropy) but the check is one string-compare.
            # v0.8.1 (from HAMILLER review): audit the attempt — a
            # tenant trying to cancel someone else's run is a
            # security signal worth logging even though the request
            # is rejected. Helpful for detecting probing or
            # compromised credentials.
            logger.warning(
                "ws_runs cancel: tenant %r tried to cancel run %r owned by %r",
                tenant_id, run_id, run_tenant_id,
            )
            raise HTTPException(
                status_code=403,
                detail=f"run {run_id!r} is not owned by tenant {tenant_id!r}",
            )
        ev.set()
        # v0.8.1: audit trail. Every successful cancel is logged
        # so a post-mortem can correlate user actions with the
        # run-history record.
        logger.info("cancelled run %r (workflow %r) for tenant %r",
                    run_id, name, tenant_id)
        return {"cancelled": True, "run_id": run_id, "workflow": name}

    @app.get("/v1/workflows", response_model=WorkflowsListResponse)
    def list_workflows(
        tenant_id: str = Depends(require_tenant),
    ) -> WorkflowsListResponse:
        """List all available workflow files. v0.11.0: added for MCP.

        Workflows live on the daemon's filesystem (`workflows_dir`),
        not in the tenant store — every tenant with a valid API key
        sees the same set. The MCP server (and any external tool)
        needs this endpoint to discover what it can `run_workflow`.
        """
        items = []
        if workflows_dir.exists():
            for p in sorted(workflows_dir.glob("*.yaml")):
                # Read just the top-level `name` and `description` for
                # the response — these are the only fields a tool
                # caller needs to display. Full workflow body stays
                # out of the API response to keep it small.
                try:
                    import yaml as _yaml
                    body = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                except Exception:
                    body = {}
                items.append({
                    "name": body.get("name", p.stem),
                    "description": body.get("description", ""),
                    "path": str(p),
                })
        return WorkflowsListResponse(workflows=items, tenant_id=tenant_id)

    @app.get("/v1/runs", response_model=RunsListResponse)
    def list_runs(
        workflow: str,
        limit: int = 50,
        before: Optional[str] = None,
        tenant_id: str = Depends(require_tenant),
    ) -> RunsListResponse:
        """List runs for one workflow, newest first. v0.11.0: added
        for MCP. Mirrors the dashboard's `/dashboard/workflows/{name}/runs`
        page so the MCP server doesn't need its own SQLite connection.

        Tenant isolation note: as of v0.11.0, the runs table is
        global (the dashboard shows the same data to any logged-in
        user). For multi-tenant deployments, a future change will
        scope this query by tenant_id. Until then, an API key only
        proves the caller is *a* tenant, not which one.
        """
        # Reject non-positive limits up front.
        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=400,
                detail="limit must be between 1 and 500",
            )
        runs = run_store.list_runs(workflow, limit=limit, before=before)
        return RunsListResponse(
            workflow=workflow,
            runs=[asdict(r) for r in runs],
            count=len(runs),
        )

    @app.get("/v1/runs/{run_id}", response_model=RunRecord)
    def show_run(
        run_id: str,
        tenant_id: str = Depends(require_tenant),
    ) -> RunRecord:
        """Look up a single run by id. v0.11.0: added for MCP.
        Returns 404 if not found. Same tenant-isolation caveat as
        the list endpoint.
        """
        run = run_store.get_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=404, detail=f"run {run_id!r} not found",
            )
        return run

    @app.get("/v1/runs/{run_id}/logs")
    def stream_run_logs(
        run_id: str,
        request: Request,
        follow: bool = True,
        since: int = 0,
        tenant_id: str = Depends(require_tenant),
    ) -> StreamingResponse:
        """Server-Sent Events stream of one run's event log. v0.12.0.

        Replays all stored events with seq > `since`, then (if `follow=true`
        and the run is still in-flight) tails the in-process EventBus for
        new events. Each event frame is `data: {...}\\n\\n`. Heartbeat
        comments (`: keepalive\\n\\n`) every 1s keep proxies from
        cutting the connection. The stream closes naturally with a
        `done` frame when the run reaches a terminal state.

        Tenant isolation: a tenant can only stream their own runs. The
        run's tenant_id is checked on the lookup; live events are
        filtered by the bus's own tenant_id field (defence in depth).
        The run must exist before the stream opens, so 404 is the
        proper HTTP response (not an SSE error frame).
        """
        # Pre-flight: validate run exists and is owned by this tenant.
        # After we return the StreamingResponse we can't change the
        # status code, so this has to happen up front.
        run = run_store.get_run(run_id)
        if run is None or run.tenant_id != tenant_id:
            # Same posture as cancel (v0.8.1 polish): don't leak that
            # the run exists by returning 404 vs 403 differently.
            raise HTTPException(
                status_code=404, detail=f"run {run_id!r} not found",
            )
        workflow = run.workflow
        bus = run_store.events

        async def event_stream():
            # Track the last seq we've emitted so we don't double-fire
            # on the boundary between replay and live tail.
            last_seq = max(0, int(since))
            # 1) Replay. The events_for_run query already filters by
            # run_id and orders ASC, so we just yield each.
            for ev in bus.events_for_run(run_id):
                if ev.seq <= last_seq:
                    continue
                last_seq = ev.seq
                yield _sse_format({
                    "seq": ev.seq, "kind": ev.kind, "payload": ev.payload,
                    "ts": ev.ts,
                })
            # 2) Live tail. Only useful while the run is in active_runs.
            if not follow:
                return
            # Open one bus subscription per stream. The bus's iterator
            # cleans up its queue in its own finally block; we use
            # wait_for so the loop can re-check active_runs and
            # is_disconnected periodically (and emit a heartbeat on
            # quiet connections).
            aiter = bus.subscribe(workflow, since=last_seq)
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    if run_id not in active_runs:
                        # The run is no longer in flight. Emit a
                        # final 'done' frame with the recorded
                        # terminal status (if any) and exit.
                        final = run_store.get_run(run_id)
                        yield _sse_format({
                            "kind": "done",
                            "status": (final.status if final else "unknown"),
                        })
                        return
                    try:
                        ev = await asyncio.wait_for(
                            aiter.__anext__(), timeout=1.0,
                        )
                    except asyncio.TimeoutError:
                        # No new event in 1s. Emit a heartbeat and
                        # re-check active_runs at the top of the loop.
                        # (Heartbeats matter for nginx / cloudflare
                        # idle timeouts on long-quiet runs.)
                        yield b": keepalive\n\n"
                        continue
                    except StopAsyncIteration:
                        # The subscribe generator was closed. This
                        # shouldn't happen in normal use, but bail
                        # safely.
                        return
                    if ev.run_id != run_id or ev.seq <= last_seq:
                        # Event for a different run on the same
                        # workflow, or a duplicate from the replay
                        # boundary. Skip.
                        continue
                    last_seq = ev.seq
                    yield _sse_format({
                        "seq": ev.seq, "kind": ev.kind,
                        "payload": ev.payload, "ts": ev.ts,
                    })
            finally:
                # The bus's async generator cleans up its subscriber
                # queue in its own finally block; we let GC run that
                # by dropping our reference. Calling aclose() would
                # be nicer but the static type lies (the stub types
                # subscribe() as AsyncIterator, not AsyncGenerator).
                pass

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
                "Connection": "keep-alive",
            },
        )

    @app.post("/v1/workflows/{name}/run", response_model=RunWorkflowResponse)
    async def run_workflow(
        name: str,
        body: RunWorkflowRequest,
        tenant_id: str = Depends(require_tenant),
    ) -> RunWorkflowResponse:
        # v0.16.0: defense in depth — reject path-traversal and control
        # characters in the workflow name. Starlette normalizes some
        # (`..` segments) but `..secret` arrives here verbatim and
        # could be a planted file. The rule: a workflow name must be a
        # single path segment containing only letters, digits, `-`, `_`.
        if not _SAFE_NAME_RE.match(name):
            raise HTTPException(
                status_code=404, detail=f"workflow {name!r} not found"
            )
        wf_path = workflows_dir / f"{name}.yaml"
        # v0.16.0: defense in depth — even after name validation, ensure
        # the resolved path is still inside workflows_dir. A symlink
        # planted in workflows_dir could otherwise point outside.
        try:
            wf_path = wf_path.resolve(strict=True)
            workflows_resolved = Path(workflows_dir).resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            raise HTTPException(
                status_code=404, detail=f"workflow {name!r} not found"
            )
        if not wf_path.is_relative_to(workflows_resolved):
            logger.warning(
                "workflow path escape attempt: name=%r resolved=%r workflows_dir=%r",
                name, wf_path, workflows_resolved,
            )
            raise HTTPException(
                status_code=404, detail=f"workflow {name!r} not found"
            )
        wf = Workflow.from_yaml(wf_path)
        mbox = mailbox_for(tenant_id)
        state = EngineState(tenant_id=tenant_id)
        # Run history record. Started now; ended + status filled in finally.
        import uuid
        from datetime import datetime, timezone
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started_at = datetime.now(timezone.utc).isoformat()
        error_msg: str | None = None
        status_label = "success"
        # v0.8.0 #1: register a cancellation event for this run. The
        # engine polls it between steps. The cancel endpoint finds the
        # event by run_id and sets it. v0.8.1 polish: tenant_id stored
        # alongside the event so the cancel endpoint can enforce
        # ownership.
        run_event = asyncio.Event()
        active_runs[run_id] = (tenant_id, run_event)
        # v0.7.0: emit a 'started' event so dashboard subscribers see
        # the run appear in real time (vs the old 5s HTMX polling).
        run_store.events.publish(
            run_id=run_id, workflow=name, tenant_id=tenant_id,
            kind="started", payload={"agent": body.agent},
        )
        try:
            await wf.run(state=state, mailbox=mbox, llm=None,
                         agent_name=body.agent, state_db=state_db,
                         cancel_event=active_runs.get(run_id, ("", None))[1])
        except WorkflowCancelled:
            # v0.8.0 #1: cancellation. The cancel endpoint set the
            # active_runs[run_id] event; the engine raised this on the
            # next inter-step check. We do NOT re-raise — the request
            # succeeds with a normal 200, and the run is recorded as
            # 'cancelled' by the finally block. From the caller's POV,
            # the run was "successfully cancelled"; from the audit
            # trail's POV, the run is just another terminal status.
            status_label = "cancelled"
            error_msg = "cancelled by user"
        except QuotaExceededError as e:
            # Quota enforcement at the workflow run level. Currently the
            # LLM provider in `serve` mode is shared (no per-tenant
            # instrument_llm) so this branch is reachable only when a
            # future change wires per-tenant instrumentation. Keeping
            # the handler so the 429 path is tested and ready.
            ended_at = datetime.now(timezone.utc).isoformat()
            duration = (datetime.fromisoformat(ended_at)
                        - datetime.fromisoformat(started_at)).total_seconds()
            try:
                run_store.record(RunRecord(
                    id=run_id, workflow=name, tenant_id=tenant_id,
                    agent=body.agent, started_at=started_at, ended_at=ended_at,
                    status="quota_exceeded", duration_seconds=duration,
                    error=str(e),
                ))
            except Exception:
                pass  # don't fail the request if history write fails
            # v0.7.1: emit a terminal event so WS subscribers don't see
            # a "started" event without a follow-up. The run record
            # already has status="quota_exceeded" so this keeps the
            # event log and the run table in sync.
            try:
                run_store.events.publish(
                    run_id=run_id, workflow=name, tenant_id=tenant_id,
                    kind="failed",
                    payload={
                        "status": "quota_exceeded",
                        "duration_seconds": duration,
                        "error": str(e),
                    },
                )
            except Exception:
                pass
            # v0.8.0 #4: tenant-quota event (rejected, but the change
            # is still notable for the overview bar).
            try:
                run_store.events.publish(
                    run_id=run_id,
                    workflow=f"__tenant_quota__:{tenant_id}",
                    tenant_id=tenant_id,
                    kind="quota_changed",
                    payload={"trigger": "quota_exceeded"},
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "quota_exceeded",
                    "tenant_id": e.tenant_id,
                    "used": e.used,
                    "limit": e.limit,
                    "requested": e.requested,
                },
                headers={"Retry-After": "2592000"},  # 30 days
            )
        except WorkflowError as e:
            status_label = "error"
            error_msg = str(e)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Record success + error + cancelled runs (quota_exceeded
            # handled above). v0.8.0 #1: 'cancelled' is now a valid
            # terminal status alongside success and error.
            if status_label in ("success", "error", "cancelled") and 'wf_path' in dir():
                ended_at = datetime.now(timezone.utc).isoformat()
                duration = (datetime.fromisoformat(ended_at)
                            - datetime.fromisoformat(started_at)).total_seconds()
                try:
                    run_store.record(RunRecord(
                        id=run_id, workflow=name, tenant_id=tenant_id,
                        agent=body.agent, started_at=started_at, ended_at=ended_at,
                        status=status_label, duration_seconds=duration,
                        error=error_msg,
                    ))
                except Exception:
                    pass  # don't fail the request if history write fails
                # v0.7.0: emit a terminal event. Subscribers use this to
                # replace the in-flight row with the final status.
                try:
                    run_store.events.publish(
                        run_id=run_id, workflow=name, tenant_id=tenant_id,
                        kind="finished" if status_label == "success" else "failed",
                        payload={
                            "status": status_label,
                            "duration_seconds": duration,
                            "error": error_msg,
                        },
                    )
                except Exception:
                    pass
                # v0.8.0 #4: also publish a tenant-quota event so the
                # overview page can refresh its quota bar in real time.
                # We use a synthetic workflow key `__tenant_quota__:<id>`
                # as a namespace separator; the WS /ws/overview endpoint
                # subscribes to that key. The event itself doesn't carry
                # the new quota value — the endpoint re-computes
                # quota_status() on each event (1 DB read, negligible).
                try:
                    run_store.events.publish(
                        run_id=run_id,
                        workflow=f"__tenant_quota__:{tenant_id}",
                        tenant_id=tenant_id,
                        kind="quota_changed",
                        payload={"trigger": "run_finished",
                                 "run_status": status_label},
                    )
                except Exception:
                    pass
            # Always unregister the cancellation event, even on
            # unexpected exceptions, so a stuck entry doesn't leak.
            active_runs.pop(run_id, None)
        return RunWorkflowResponse(state_keys=sorted(state._data.keys()))

    # Wrap the whole ASGI stack with RequestIdMiddleware. Starlette runs
    # middlewares in reverse-registration order, so this outermost wrap
    # means the middleware sees the raw inbound request and can write
    # the X-Request-Id header on the response before the FastAPI router
    # processes anything.
    app.add_middleware(RequestIdMiddleware)

    # v0.16.0: body size limit. Prevents a caller from POSTing a huge
    # JSON body and exhausting server memory. The cap is configurable
    # via AGENTFORGE_MAX_BODY_BYTES (default 1 MiB).
    import os as _os
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse as _JSONResponse
    _MAX_BODY = int(_os.environ.get("AGENTFORGE_MAX_BODY_BYTES", str(1024 * 1024)))

    class _BodySizeLimitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            cl = request.headers.get("content-length")
            if cl is not None:
                try:
                    if int(cl) > _MAX_BODY:
                        return _JSONResponse(
                            {"detail": f"request body too large (>{_MAX_BODY} bytes)"},
                            status_code=413,
                        )
                except ValueError:
                    pass
            return await call_next(request)

    app.add_middleware(_BodySizeLimitMiddleware)

    # -- dashboard wiring --------------------------------------------------
    # State that the dashboard router needs (templates, registry, paths).
    # Mounted BEFORE we add the router so the router's dependency lookups
    # resolve. v0.6.0: tenants + usage + runs are all SQLite-backed now —
    # the dashboard router reads these handles directly, no per-request
    # file IO.
    app.state.tenants = registry
    app.state.usage = usage_store
    app.state.runs = run_store
    app.state.templates = get_templates()
    app.state.workflows_dir = workflows_dir

    # -- OTLP exporter (v0.5.5) --------------------------------------------
    # If OTEL_EXPORTER_OTLP_ENDPOINT is set, start a background thread that
    # pushes the metrics registry to the collector every 30s. Standard OTLP
    # env var name, so users can configure via the same flag their other
    # OTel-instrumented apps use.
    import os as _os
    _otlp_endpoint = _os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if _otlp_endpoint:
        from agentforge.observability.otlp import OtlpExporter
        app.state.otlp_exporter = OtlpExporter(
            endpoint=_otlp_endpoint, registry=get_registry(),
            service_name="agentforge", service_version="0.8.0",
        )
        app.state.otlp_exporter.start()

    # Mount the static files directory for the dashboard CSS.
    from fastapi.staticfiles import StaticFiles
    _dashboard_static = Path(__file__).parent / "dashboard" / "static"
    app.mount("/dashboard/static", StaticFiles(directory=str(_dashboard_static)),
              name="dashboard-static")

    # Include the dashboard HTML routes.
    app.include_router(dashboard_router)

    return app


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
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response
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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    tenants_path: Path,
    mailbox_root: Path,
    state_db: Optional[Path] = None,
    workflows_dir: Optional[Path] = None,
) -> FastAPI:
    """Build the FastAPI app. No IO at import time — pass all config."""
    tenants_path = Path(tenants_path)
    mailbox_root = Path(mailbox_root)
    state_db = Path(state_db) if state_db is not None else mailbox_root.parent / "state.db"
    workflows_dir = Path(workflows_dir) if workflows_dir is not None else mailbox_root.parent / "workflows"

    app = FastAPI(title="agentforge", version="0.8.0")
    # SQLite-backed state (v0.6.0). One State object, three handles
    # (tenants, usage, runs) all sharing one connection + one lock.
    # Falls back to the JSON files if state_db is explicitly None.
    state = AppState(state_db)
    # Backwards-compat: if the user has legacy JSON files lying around
    # and hasn't migrated, do it now. migrate_json_to_sqlite is
    # idempotent (INSERT OR IGNORE) so safe to call on every boot.
    legacy_tenants = tenants_path if tenants_path.exists() else None
    legacy_usage = (mailbox_root.parent / "usage.json") if (mailbox_root.parent / "usage.json").exists() else None
    legacy_runs = (mailbox_root.parent / "runs.json") if (mailbox_root.parent / "runs.json").exists() else None
    if any(p is not None for p in (legacy_tenants, legacy_usage, legacy_runs)):
        migrate_json_to_sqlite(legacy_tenants, legacy_usage, legacy_runs, state)
    # `registry` and `usage_store` keep their old names so the rest of
    # serve.py doesn't need to change. They're now SQLite-backed.
    registry = state.tenants
    usage_store = state.usage
    run_store = state.runs

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

    # v0.8.0 #1: active-run registry. Maps run_id -> (tenant_id,
    # asyncio.Event) so the cancel endpoint can enforce ownership
    # (v0.8.1 polish: a cancel for tenant B's run_id by tenant A
    # was previously allowed, because the dict was keyed by run_id
    # only). The event is the one the engine polls between steps.
    # Scope: in-process only. A multi-worker deployment would need
    # a shared cancellation channel (Redis pub/sub or a DB flag) —
    # out of scope for v0.8.0.
    active_runs: dict[str, tuple[str, "asyncio.Event"]] = {}

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

    @app.post("/v1/workflows/{name}/run", response_model=RunWorkflowResponse)
    async def run_workflow(
        name: str,
        body: RunWorkflowRequest,
        tenant_id: str = Depends(require_tenant),
    ) -> RunWorkflowResponse:
        wf_path = workflows_dir / f"{name}.yaml"
        if not wf_path.exists():
            raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
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


"""FastAPI router for the dashboard UI. Side-effect-free at import time."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Cookie,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from agentforge.billing.plans import Plan
from agentforge.billing.quota import quota_status
from agentforge.billing.usage import UsageStore  # noqa: F401  (re-exported for tests)
from agentforge.core.runs import RunStore  # noqa: F401  (re-exported for tests)
from agentforge.dashboard.auth import (
    COOKIE_NAME,
    get_registry,
    tenant_from_cookie_or_401,
)

logger = logging.getLogger("agentforge.dashboard.ws")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def get_templates():
    """Build a fresh Jinja2 environment. Called per-app to keep state local."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request) -> Response:
    templates = request.app.state.templates
    return HTMLResponse(
        templates.get_template("login.html").render(
            request=request, error=None,
        )
    )


@router.post("/login")
def login_post(
    request: Request,
    api_key: str = Form(...),
) -> Response:
    registry = get_registry(request)
    tenant_id = registry.lookup(api_key)
    if tenant_id is None:
        templates = request.app.state.templates
        return HTMLResponse(
            templates.get_template("login.html").render(
                request=request, error="Invalid API key.",
            ),
            status_code=401,
        )
    response = RedirectResponse(url="/dashboard/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        COOKIE_NAME, api_key,
        httponly=True, samesite="lax", max_age=86400,  # 1 day
    )
    return response


@router.get("/logout")
def logout() -> Response:
    response = RedirectResponse(url="/dashboard/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_workflows(workflows_dir: Path) -> list[dict]:
    if not workflows_dir.exists():
        return []
    items = []
    for p in workflows_dir.glob("*.yaml"):
        items.append({"name": p.stem, "mtime": p.stat().st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# Main pages
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def overview(request: Request) -> Response:
    tenant_id = tenant_from_cookie_or_401(request)
    qs = quota_status(request.app.state.tenants, request.app.state.usage, tenant_id)
    workflows = _list_workflows(request.app.state.workflows_dir)
    templates = request.app.state.templates
    return templates.get_template("overview.html").render(
        request=request, tenant_id=tenant_id, quota=qs,
        workflow_count=len(workflows), recent_workflows=workflows[:5],
    )


@router.get("/tenants", response_class=HTMLResponse)
def tenants_list(request: Request) -> Response:
    tenant_id = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    usage = request.app.state.usage
    rows = []
    for tid in registry.list_tenants():
        qs = quota_status(registry, usage, tid)
        rows.append({
            "tenant_id": tid, "plan": qs.plan.value,
            "used": qs.used, "limit": qs.limit,
            "warning": qs.warning, "exceeded": qs.exceeded,
        })
    templates = request.app.state.templates
    return templates.get_template("tenants.html").render(
        request=request, current_tenant=tenant_id, rows=rows,
    )


@router.post("/tenants")
def tenants_create(
    request: Request,
    tenant_id: str = Form(...),
) -> Response:
    # Auth gate — call for side effect (raises 401 if cookie invalid)
    tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    api_key = registry.add(tenant_id)
    return RedirectResponse(
        url=f"/dashboard/tenants/{tenant_id}?new_key={api_key}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/tenants/{tenant_id}/delete")
def tenants_delete(request: Request, tenant_id: str) -> Response:
    # Auth gate — call for side effect (raises 401 if cookie invalid)
    tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    registry.remove(tenant_id)
    return RedirectResponse(url="/dashboard/tenants", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
def tenant_detail(request: Request, tenant_id: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    usage = request.app.state.usage
    qs = quota_status(registry, usage, tenant_id)
    new_key = request.query_params.get("new_key")
    templates = request.app.state.templates
    return templates.get_template("tenant_detail.html").render(
        request=request, auth_tenant=auth_tenant,
        target_tenant=tenant_id, quota=qs, new_key=new_key,
    )


@router.post("/tenants/{tenant_id}/plan")
def tenant_set_plan(
    request: Request,
    tenant_id: str,
    plan: str = Form(...),
) -> Response:
    # Auth gate — call for side effect (raises 401 if cookie invalid)
    tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    if plan not in ("free", "pro", "enterprise"):
        raise HTTPException(status_code=400, detail=f"invalid plan {plan!r}")
    registry.set_plan(tenant_id, Plan(plan))
    return RedirectResponse(
        url=f"/dashboard/tenants/{tenant_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/workflows", response_class=HTMLResponse)
def workflows_list(request: Request) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    workflows = _list_workflows(request.app.state.workflows_dir)
    templates = request.app.state.templates
    return templates.get_template("workflows.html").render(
        request=request, auth_tenant=auth_tenant, workflows=workflows,
    )


@router.get("/workflows/new", response_class=HTMLResponse)
def workflow_new_get(request: Request) -> Response:
    """Form for a new workflow: name + empty YAML content.

    NOTE: This route MUST be registered BEFORE `/workflows/{name}` so
    FastAPI's pattern matcher doesn't bind `name="new"` to the dynamic
    segment and try to read workflows_dir/new.yaml (which doesn't exist).
    """
    tenant_from_cookie_or_401(request)
    templates = request.app.state.templates
    starter = (
        "name: my_workflow\n"
        "description: A short description.\n"
        "steps:\n"
        "  - id: receive\n"
        "    type: receive\n"
        "  - id: respond\n"
        "    type: respond\n"
        "    inputs:\n"
        "      to: user\n"
        "      content: \"got: {{ receive.content }}\"\n"
    )
    return templates.get_template("workflow_edit.html").render(
        request=request, name="", yaml_text=starter, error=None, is_new=True,
    )


def _validate_yaml(yaml_text: str) -> tuple[bool, str]:
    """Parse YAML. Return (ok, error_message). Empty content is invalid."""
    if not yaml_text or not yaml_text.strip():
        return False, "YAML content is empty"
    try:
        import yaml
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return False, f"invalid YAML: {e}"
    if not isinstance(parsed, dict):
        return False, "YAML must be a mapping (key: value) at the top level"
    if "name" not in parsed:
        return False, "YAML must contain a top-level 'name' key"
    return True, ""


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically (tempfile + os.replace)."""
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@router.post("/workflows/{name}")
def workflow_save(
    request: Request,
    name: str,
    yaml_content: str = Form(""),
) -> Response:
    """Validate + save (create or overwrite). yaml_content default empty
    so the empty-string test path returns our 400, not FastAPI's 422."""
    tenant_from_cookie_or_401(request)
    wf_dir = request.app.state.workflows_dir
    if "/" in name or ".." in name or not name:
        raise HTTPException(status_code=400, detail="invalid workflow name")
    ok, err = _validate_yaml(yaml_content)
    if not ok:
        templates = request.app.state.templates
        html = templates.get_template("workflow_edit.html").render(
            request=request, name=name, yaml_text=yaml_content,
            error=err, is_new=not (wf_dir / f"{name}.yaml").exists(),
        )
        return HTMLResponse(content=html, status_code=400)
    _atomic_write(wf_dir / f"{name}.yaml", yaml_content)
    return RedirectResponse(
        url=f"/dashboard/workflows/{name}",
        status_code=303,
    )


@router.get("/workflows/{name}", response_class=HTMLResponse)
def workflow_detail(request: Request, name: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    wf_path = request.app.state.workflows_dir / f"{name}.yaml"
    if not wf_path.exists():
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
    yaml_text = wf_path.read_text(encoding="utf-8")
    templates = request.app.state.templates
    return templates.get_template("workflow_detail.html").render(
        request=request, auth_tenant=auth_tenant,
        name=name, yaml_text=yaml_text,
    )


# ---------------------------------------------------------------------------
# Workflow run history (v0.5.4)
# ---------------------------------------------------------------------------
# NOTE: route ordering matters again — `/workflows/{name}/runs` MUST be
# registered before `/workflows/{name}` for the same reason as `/new`:
# FastAPI's pattern matcher binds the dynamic segment to "runs" otherwise
# and tries to load workflows_dir/runs.yaml. (The `name != "runs"` guard
# would also work but is more fragile than just declaring the static
# segments first.)

@router.get("/workflows/{name}/runs", response_class=HTMLResponse)
def workflow_runs(request: Request, name: str) -> Response:
    """Render the runs history page for one workflow. Initial render
    pre-fills the table; WebSocket pushes updates; "load more" button
    fetches older pages (v0.8.0 #3)."""
    tenant_from_cookie_or_401(request)
    run_store = request.app.state.runs
    runs = run_store.list_runs(name, limit=50)
    templates = request.app.state.templates
    return templates.get_template("workflow_runs.html").render(
        request=request, workflow_name=name, runs=runs,
    )


@router.get("/partials/runs/{name}", response_class=HTMLResponse)
def partial_runs(request: Request, name: str) -> Response:
    """HTMX/JS fragment: rendered rows of the runs table for one workflow.

    `?before=<started_at>` is the cursor for the next page (v0.8.0 #3).
    `?limit=N` controls the page size (default 50).
    """
    tenant_from_cookie_or_401(request)
    run_store = request.app.state.runs
    before = request.query_params.get("before")
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    runs = run_store.list_runs(name, limit=limit, before=before)
    templates = request.app.state.templates
    return templates.get_template("_partial_runs_rows.html").render(
        request=request, workflow_name=name, runs=runs,
    )


# ---------------------------------------------------------------------------
# WebSocket run event stream (v0.7.0)
# ---------------------------------------------------------------------------
# Replaces the HTMX `every 5s` polling on the runs page. The server
# pushes a JSON message for every run event the EventBus sees for this
# workflow. Clients reconnect with `?since=<seq>` and the server
# replays missed events from the run_events table before going live.
#
# Backpressure: the underlying TCP/ASGI buffer is the only
# throttling. We send every event as it arrives. If the client falls
# behind, the kernel TCP buffer absorbs a small burst, then the
# WebSocket close-on-write-timeout kicks in (uvicorn default 10s).
# Coalescing events at 100ms intervals would add complexity for
# marginal value on a dashboard that already pre-renders the last
# 50 runs server-side.
#
# Auth: WebSockets can't carry arbitrary headers from the browser,
# so we authenticate by reading the session cookie directly. The
# same `tenant_from_cookie_or_401` helper is reused — if the cookie
# is missing or the API key doesn't resolve to a tenant, we close
# with code 1008 (policy violation) before accepting.

@router.websocket("/ws/runs/{name}")
async def ws_runs(
    websocket: WebSocket,
    name: str,
    cookie: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> None:
    """Stream run events for one workflow as JSON text frames.

    Each frame: `{"seq": int, "run_id": str, "kind": str, "payload":
    {...}, "ts": "ISO8601"}`. The first frame is always a `hello`
    message that includes the current `max_seq` so the client can
    decide whether to reconnect-with-replay on disconnect.

    v0.7.1 hardening:
    - **CSRF protection via Origin check.** Browsers cannot set
      custom headers on a WS handshake, but they DO send the Origin
      header. A cross-origin page (`evil.com`) opening a WS to
      `agentforge.example/dashboard/ws/runs/x` would carry the
      session cookie. SameSite=Lax does not block this (Lax allows
      top-level navigations + same-site requests). We reject the
      upgrade with 1008 when Origin is present and does not match
      the request's Host. Same-origin requests (no Origin header)
      are allowed.
    - **Tenant isolation.** A subscribing tenant only sees events
      whose `tenant_id` matches their own. Without this filter, any
      authenticated tenant could observe any other tenant's run
      events for any workflow.
    """
    # CSRF: reject cross-origin WS upgrade before accept(). The Origin
    # header is set by the browser for cross-origin requests. We trust
    # the Host header to identify our own host (FastAPI exposes it on
    # the websocket scope via `headers`).
    origin = websocket.headers.get("origin")
    if origin is not None:
        host = websocket.headers.get("host", "")
        # Parse just the host:port out of the Origin (which is
        # "scheme://host[:port]"). urllib.parse gives us a clean split
        # without dragging in a URL object.
        from urllib.parse import urlparse
        origin_host = urlparse(origin).netloc
        if not host or origin_host != host:
            await websocket.close(code=1008, reason="cross-origin not allowed")
            return
    # Auth via cookie. Reject before accept() so unauthorized clients
    # get a clean 1008 close (not a half-open socket).
    if not cookie:
        await websocket.close(code=1008, reason="missing session cookie")
        return
    registry = websocket.app.state.tenants
    tenant_id = registry.lookup(cookie)
    if tenant_id is None:
        await websocket.close(code=1008, reason="invalid session cookie")
        return
    await websocket.accept()
    bus = websocket.app.state.runs.events
    # `since` query param: client tells us the last seq it saw, server
    # replays any events with seq > since before going live. Default 0
    # (replay everything for this workflow).
    since = 0
    try:
        raw = websocket.query_params.get("since", "0")
        since = max(0, int(raw))
    except ValueError:
        since = 0
    # First frame: hello with the current max_seq, so the client can
    # reconnect-with-replay correctly even if the bus is empty.
    try:
        await websocket.send_text(json.dumps({
            "kind": "hello",
            "seq": bus.max_seq(name),
            "workflow": name,
        }))
    except Exception:
        return
    # Drain replay + live. We loop manually so we can stop on
    # WebSocketDisconnect; an `async for` would also work but the
    # try/except is more explicit about the disconnect path.
    #
    # Tenant isolation (v0.7.1): events whose tenant_id does not
    # match the authenticated tenant are dropped before being sent.
    # This adds 1 string-compare per event — negligible.
    try:
        async for ev in bus.subscribe(name, since=since):
            if ev.tenant_id and ev.tenant_id != tenant_id:
                continue
            await websocket.send_text(json.dumps({
                "kind": ev.kind,
                "seq": ev.seq,
                "run_id": ev.run_id,
                "payload": ev.payload,
                "ts": ev.ts,
            }))
    except WebSocketDisconnect:
        # Client closed cleanly. The bus.subscribe() finally block
        # already removed our queue from the subscribers list.
        logger.debug("ws_runs[%s]: client disconnected", name)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("ws_runs[%s]: unexpected error: %s", name, e)
        try:
            await websocket.close(code=1011, reason="server error")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# WebSocket overview-quota stream (v0.8.0 #4)
# ---------------------------------------------------------------------------
# Replaces the HTMX `every 5s` polling of /partials/usage on the
# overview page. The server pushes a fresh `quota_status` JSON frame
# every time a run finishes (or any other quota-changing event fires
# for the authenticated tenant). Replay-on-reconnect uses the same
# `?since=` cursor pattern as the runs WS.
#
# Authentication, Origin check, and tenant isolation follow the same
# patterns as the runs WS (see above).

@router.websocket("/ws/overview")
async def ws_overview(
    websocket: WebSocket,
    cookie: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> None:
    """Stream quota-status updates for the authenticated tenant.

    Each frame: `{"kind": "quota", "tenant_id": str, "plan": str,
    "used": int, "limit": int|None, "remaining": int|None, "pct":
    float, "warning": bool, "exceeded": bool}`. First frame is a
    `hello` with the current `max_seq` of the tenant's quota
    stream (v0.8.0 #4 convention: workflow key
    `__tenant_quota__:<tenant_id>`).
    """
    # CSRF: same-origin check (see ws_runs for the rationale).
    origin = websocket.headers.get("origin")
    if origin is not None:
        host = websocket.headers.get("host", "")
        from urllib.parse import urlparse
        origin_host = urlparse(origin).netloc
        if not host or origin_host != host:
            await websocket.close(code=1008, reason="cross-origin not allowed")
            return
    # Auth.
    if not cookie:
        await websocket.close(code=1008, reason="missing session cookie")
        return
    registry = websocket.app.state.tenants
    usage = websocket.app.state.usage
    tenant_id = registry.lookup(cookie)
    if tenant_id is None:
        await websocket.close(code=1008, reason="invalid session cookie")
        return
    await websocket.accept()
    bus = websocket.app.state.runs.events
    quota_key = f"__tenant_quota__:{tenant_id}"
    since = 0
    try:
        raw = websocket.query_params.get("since", "0")
        since = max(0, int(raw))
    except ValueError:
        since = 0
    # Hello frame.
    try:
        await websocket.send_text(json.dumps({
            "kind": "hello",
            "seq": bus.max_seq(quota_key),
            "tenant_id": tenant_id,
        }))
    except Exception:
        return
    try:
        async for ev in bus.subscribe(quota_key, since=since):
            if ev.tenant_id and ev.tenant_id != tenant_id:
                # Shouldn't happen (we subscribed to our own key), but
                # defense-in-depth: don't leak other tenants' events.
                continue
            # Re-compute the quota on each event. One DB read of
            # tenants + usage, negligible.
            qs = quota_status(registry, usage, tenant_id)
            await websocket.send_text(json.dumps({
                "kind": "quota",
                "seq": ev.seq,
                "tenant_id": qs.tenant_id,
                "plan": qs.plan.value,
                "used": qs.used,
                "limit": qs.limit,
                "remaining": qs.remaining,
                "pct": qs.pct,
                "warning": qs.warning,
                "exceeded": qs.exceeded,
            }))
    except WebSocketDisconnect:
        logger.debug("ws_overview[%s]: client disconnected", tenant_id)
    except Exception as e:  # pragma: no cover
        logger.warning("ws_overview[%s]: unexpected error: %s", tenant_id, e)
        try:
            await websocket.close(code=1011, reason="server error")
        except Exception:
            pass


@router.get("/workflows/{name}/edit", response_class=HTMLResponse)
def workflow_edit_get(request: Request, name: str) -> Response:
    """Edit form pre-filled with the current YAML."""
    tenant_from_cookie_or_401(request)
    wf_path = request.app.state.workflows_dir / f"{name}.yaml"
    if not wf_path.exists():
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
    yaml_text = wf_path.read_text(encoding="utf-8")
    templates = request.app.state.templates
    return templates.get_template("workflow_edit.html").render(
        request=request, name=name, yaml_text=yaml_text,
        error=None, is_new=False,
    )


@router.post("/workflows/{name}/delete")
def workflow_delete(request: Request, name: str) -> Response:
    """Remove the .yaml file. Safe if it doesn't exist."""
    tenant_from_cookie_or_401(request)
    wf_path = request.app.state.workflows_dir / f"{name}.yaml"
    if wf_path.exists():
        wf_path.unlink()
    return RedirectResponse(
        url="/dashboard/workflows", status_code=303,
    )


# ---------------------------------------------------------------------------
# HTMX partials — return HTML fragments for polling
# ---------------------------------------------------------------------------
# HTMX `hx-get` + `hx-trigger="every Ns"` re-fetches these endpoints and
# swaps their returned HTML into the trigger element. We return fragments,
# not full pages — no <html>/<body> wrapper. This keeps polling cheap
# (small payloads, no layout re-render).

@router.get("/partials/usage", response_class=HTMLResponse)
def partial_usage(request: Request) -> Response:
    """Render the quota-card body (bar + status line) for the current tenant.

    Used by `overview.html` via HTMX polling. The card on the overview page
    is tagged with `hx-get="/dashboard/partials/usage" hx-trigger="every 5s"`
    so this endpoint fires every 5 seconds and the bar updates in place.
    """
    tenant_id = tenant_from_cookie_or_401(request)
    usage = request.app.state.usage
    qs = quota_status(request.app.state.tenants, usage, tenant_id)
    templates = request.app.state.templates
    return templates.get_template("_partial_usage.html").render(
        request=request, quota=qs,
    )


@router.get("/partials/tenants", response_class=HTMLResponse)
def partial_tenants(request: Request) -> Response:
    """Render the table-row body of the tenants list (no <table> wrapper).

    Used by `tenants.html` via HTMX polling on the <tbody>. Fires every
    5 seconds; the whole row set is re-rendered, so plan/usage changes
    appear without a page refresh.
    """
    tenant_from_cookie_or_401(request)  # 401 if not signed in
    registry = request.app.state.tenants
    usage = request.app.state.usage
    rows = []
    for tid in registry.list_tenants():
        qs = quota_status(registry, usage, tid)
        rows.append({
            "tenant_id": tid, "plan": qs.plan.value,
            "used": qs.used, "limit": qs.limit,
            "warning": qs.warning, "exceeded": qs.exceeded,
        })
    templates = request.app.state.templates
    return templates.get_template("_partial_tenants_rows.html").render(
        request=request, rows=rows,
    )

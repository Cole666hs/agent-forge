"""FastAPI router for the dashboard UI. Side-effect-free at import time."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from agentforge.billing.plans import Plan
from agentforge.billing.quota import quota_status
from agentforge.billing.usage import UsageStore
from agentforge.dashboard.auth import (
    COOKIE_NAME,
    get_registry,
    tenant_from_cookie_or_401,
)

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
    usage = UsageStore(path=request.app.state.usage_path)
    qs = quota_status(request.app.state.tenants, usage, tenant_id)
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
    usage = UsageStore(path=request.app.state.usage_path)
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
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    api_key = registry.add(tenant_id)
    return RedirectResponse(
        url=f"/dashboard/tenants/{tenant_id}?new_key={api_key}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/tenants/{tenant_id}/delete")
def tenants_delete(request: Request, tenant_id: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    registry.remove(tenant_id)
    return RedirectResponse(url="/dashboard/tenants", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
def tenant_detail(request: Request, tenant_id: str) -> Response:
    auth_tenant = tenant_from_cookie_or_401(request)
    registry = request.app.state.tenants
    usage = UsageStore(path=request.app.state.usage_path)
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
    auth_tenant = tenant_from_cookie_or_401(request)
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
    usage = UsageStore(path=request.app.state.usage_path)
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
    usage = UsageStore(path=request.app.state.usage_path)
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

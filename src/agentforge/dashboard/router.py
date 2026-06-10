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

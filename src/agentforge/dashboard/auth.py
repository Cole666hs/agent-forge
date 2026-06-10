"""Cookie-based auth for the dashboard. The API key IS the credential."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

COOKIE_NAME = "agentforge_api_key"


def get_registry(request: Request):
    """Access the app's TenantRegistry instance (attached in create_app)."""
    return request.app.state.tenants


def tenant_from_cookie_or_401(request: Request) -> str:
    """Read the API key from the cookie, look up the tenant, return tenant_id.
    Raises 401 if missing or invalid.
    """
    api_key = request.cookies.get(COOKIE_NAME, "")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. POST /dashboard/login with your API key.",
        )
    registry = get_registry(request)
    tenant_id = registry.lookup(api_key)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )
    return tenant_id

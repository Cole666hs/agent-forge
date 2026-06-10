"""agentforge.dashboard — server-rendered web UI (FastAPI + Jinja2 + HTMX)."""
from agentforge.dashboard.router import router, get_templates

__all__ = ["router", "get_templates"]

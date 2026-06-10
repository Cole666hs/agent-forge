"""FastAPI router for the dashboard UI. Side-effect-free at import time."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from jinja2 import Environment, FileSystemLoader, select_autoescape

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Templates live alongside this file at .../dashboard/templates/
_TEMPLATES_DIR = Path(__file__).parent / "templates"


def get_templates() -> Environment:
    """Build a fresh Jinja2 environment. Called per-app to keep state local."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

"""Tests for agentforge.dashboard — package skeleton + Jinja2 environment."""
import agentforge.dashboard
from agentforge.dashboard.router import get_templates


def test_dashboard_package_imports():
    assert hasattr(agentforge.dashboard, "router")


def test_get_templates_returns_jinja2_environment():
    env = get_templates()
    assert hasattr(env, "get_template")
    assert hasattr(env, "from_string")
    # Smoke: a simple string template renders
    t = env.from_string("hello {{ name }}")
    assert t.render(name="world") == "hello world"

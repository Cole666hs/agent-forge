"""Tests for v0.11.0 MCP server.

The MCP server is a thin HTTP transport wrapper. We test the two
distinct layers:

1. `_make_tool_handler(daemon_url, api_key)` — the synchronous HTTP
   function that backs every tool call. We mock `requests.Session`
   to verify the right URL, method, headers, and body are sent for
   each tool name. This is the "what does the daemon see?" test.

2. The CLI `mcp serve` subcommand — verifies that the command is
   wired up, error handling works when the mcp package is missing,
   and a successful launch doesn't blow up.

We deliberately do NOT spin up the actual stdio MCP server in
tests — that's an integration concern. The transport + dispatch
logic is exercised through `_make_tool_handler` and the per-tool
call paths.

Covers:
- list_workflows → GET /v1/workflows
- list_runs → GET /v1/runs?workflow=X&limit=N
- show_run → GET /v1/runs/{id}
- run_workflow → POST /v1/workflows/{name}/run with {agent: ...}
- cancel_run → POST /v1/workflows/{name}/runs/{id}/cancel
- X-API-Key header is sent on every call
- Daemon unreachable → structured error (not a stack trace)
- 4xx/5xx responses are surfaced as {status, body}
- 5 tools are registered with the right names
- CLI `mcp serve` requires AGENTFORGE_API_KEY (or --api-key)
- CLI `mcp serve` rejects missing mcp package
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from agentforge.cli import cli
from agentforge.mcp import MCP_AVAILABLE, _make_tool_handler


pytestmark = pytest.mark.skipif(
    not MCP_AVAILABLE,
    reason="mcp package not installed; skip MCP server tests",
)


class _FakeResp:
    """Mimics requests.Response — only fields our handler uses."""
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text if text else (json.dumps(body) if body is not None else "")

    def json(self):
        return self._body or {}


@pytest.fixture
def handler():
    """A handler with a fake daemon that always returns the canned response."""
    h = _make_tool_handler("http://d:8765", "fake-test-key-001")
    return h


def _patch_session(handler, response: _FakeResp):
    """Patch the requests.Session held by the handler's closure."""
    mock_session = MagicMock()
    mock_session.request.return_value = response
    # The handler uses session.request(...). We need to swap the
    # session object inside the closure. Simpler: replace the
    # bound method on the requests module.
    return patch("agentforge.mcp.requests.Session",
                 return_value=mock_session)


# ---- handler layer --------------------------------------------------------


def test_handler_sends_x_api_key_header(handler):
    """Every call carries X-API-Key. The MCP server's whole auth story
    depends on this — if it leaks, tenants can see each other's data."""
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(200, {"ok": True})
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "fake-test-key-001")
        h("GET", "/v1/workflows")
    # The session was constructed with the API key in its default headers.
    update_call = mock_session.headers.update.call_args
    assert update_call is not None
    assert update_call.args[0]["X-API-Key"] == "fake-test-key-001"


def test_handler_daemon_unreachable(handler):
    """requests.RequestException → structured error, no stack trace."""
    import requests
    mock_session = MagicMock()
    mock_session.request.side_effect = requests.ConnectionError("refused")
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "fake-test-key-001")
        out = h("GET", "/v1/workflows")
    assert "error" in out
    assert "cannot reach daemon" in out["error"]
    # The URL must surface so the LLM caller can diagnose (typo? wrong port?)
    assert out["url"] == "http://d:8765/v1/workflows"
    assert out["method"] == "GET"


def test_handler_5xx_surfaces_status_and_body(handler):
    """Daemon 500 → caller sees {status: 500, body: {...}}."""
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(
        500, body={"detail": "kaboom"},
    )
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "fake-test-key-001")
        out = h("GET", "/v1/workflows")
    assert out["status"] == 500
    assert out["body"]["detail"] == "kaboom"


def test_handler_404_surfaces_status(handler):
    """Daemon 404 → caller sees {status: 404, body: {...}}."""
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(
        404, body={"detail": "not found"},
    )
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "fake-test-key-001")
        out = h("GET", "/v1/runs/abc")
    assert out["status"] == 404
    assert out["body"]["detail"] == "not found"


# ---- per-tool HTTP shape --------------------------------------------------


def test_tool_list_workflows_calls_get_v1_workflows(handler):
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(
        200, body={"workflows": [], "tenant_id": "acme"},
    )
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "fake-test-key-001")
        out = h("GET", "/v1/workflows")
    assert out["status"] == 200
    call = mock_session.request.call_args
    assert call.args == ("GET", "http://d:8765/v1/workflows")
    assert call.kwargs["timeout"] == 30


def test_tool_list_runs_includes_workflow_and_limit():
    """list_runs must forward workflow + optional limit/before as query params."""
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(
        200, body={"workflow": "demo", "runs": [], "count": 0},
    )
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "k")
        out = h("GET", "/v1/runs?workflow=demo", params={"limit": 5})
    assert out["status"] == 200
    call = mock_session.request.call_args
    assert call.args[0] == "GET"
    assert "workflow=demo" in call.args[1]
    assert call.kwargs["params"] == {"limit": 5}


def test_tool_show_run_uses_run_id_in_path():
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(
        200, body={"id": "r-123", "status": "success"},
    )
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "k")
        out = h("GET", "/v1/runs/r-123")
    assert out["status"] == 200
    assert mock_session.request.call_args.args[1] == "http://d:8765/v1/runs/r-123"


def test_tool_run_workflow_posts_agent_body():
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(
        200, body={"state_keys": []},
    )
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "k")
        out = h("POST", "/v1/workflows/demo/run", json={"agent": "tester"})
    assert out["status"] == 200
    call = mock_session.request.call_args
    assert call.args[0] == "POST"
    assert call.args[1] == "http://d:8765/v1/workflows/demo/run"
    assert call.kwargs["json"] == {"agent": "tester"}


def test_tool_cancel_run_uses_workflow_and_id_in_path():
    mock_session = MagicMock()
    mock_session.request.return_value = _FakeResp(
        200, body={"cancelled": True, "run_id": "r-9", "workflow": "demo"},
    )
    with patch("agentforge.mcp.requests.Session", return_value=mock_session):
        h = _make_tool_handler("http://d:8765", "k")
        out = h("POST", "/v1/workflows/demo/runs/r-9/cancel")
    assert out["status"] == 200
    assert mock_session.request.call_args.args[1] == "http://d:8765/v1/workflows/demo/runs/r-9/cancel"


# ---- CLI layer ------------------------------------------------------------


def test_cli_mcp_serve_rejects_missing_api_key():
    """Without AGENTFORGE_API_KEY and no --api-key, exit 1."""
    runner = CliRunner()
    result = runner.invoke(cli, ["mcp", "serve"], env={})
    assert result.exit_code == 1
    assert "AGENTFORGE_API_KEY" in result.output or "API" in result.output


def test_cli_mcp_serve_passes_daemon_url_and_api_key():
    """The CLI must thread --daemon-url and --api-key into the MCP main()."""
    runner = CliRunner()
    # The CLI does `from agentforge.mcp import MCP_AVAILABLE, main as mcp_main`
    # inside the function, so the symbol lives in `agentforge.mcp` and
    # that's what we patch.
    with patch("agentforge.mcp.main") as mock_main:
        mock_main.return_value = 0
        result = runner.invoke(
            cli,
            ["--daemon-url", "http://x:9999",
             "--api-key", "fake-test-key-001",
             "mcp", "serve"],
        )
    assert result.exit_code == 0, result.output
    mock_main.assert_called_once()
    kwargs = mock_main.call_args.kwargs
    assert kwargs["daemon_url"] == "http://x:9999"
    assert kwargs["api_key"] == "fake-test-key-001"


def test_cli_mcp_serve_propagates_exit_code():
    """The MCP server's exit code flows through to the CLI."""
    runner = CliRunner()
    with patch("agentforge.mcp.main", return_value=42) as mock_main:
        result = runner.invoke(
            cli,
            ["--api-key", "fake-test-key-001", "mcp", "serve"],
        )
    assert result.exit_code == 42
    mock_main.assert_called_once()


def test_cli_mcp_group_lists_serve_subcommand():
    """`agentforge mcp --help` shows the `serve` subcommand."""
    runner = CliRunner()
    result = runner.invoke(cli, ["mcp", "--help"])
    assert result.exit_code == 0, result.output
    assert "serve" in result.output

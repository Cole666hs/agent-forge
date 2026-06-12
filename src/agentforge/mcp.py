"""agentforge.mcp — MCP server that exposes agentforge operations as tools.

v0.11.0 design:

The MCP server is a thin transport wrapper. It does NOT touch the
state.db or run workflows itself. It speaks to a running `agentforge
serve` daemon over HTTP and exposes each HTTP endpoint as an MCP tool.

Why HTTP, not direct DB access?
- Single source of truth: the daemon owns active_runs, audit log,
  and run history. The MCP server is stateless and can be restarted
  freely without losing data.
- Multi-tenant safety: the daemon's `X-API-Key` auth is the boundary.
  Every tool call goes through it, so an MCP client that knows one
  tenant's key can't impersonate another.
- The cancel audit log, run history, and quota events are all
  written by the daemon. The MCP server is a thin caller, just like
  the dashboard and the CLI.

Tools exposed:
  list_workflows()                  — discover what's runnable
  list_runs(workflow, limit, before) — inspect run history
  show_run(run_id)                  — get one run's full record
  run_workflow(name, agent)         — trigger a workflow run
  cancel_run(run_id, workflow)      — cancel an in-flight run

Transport: stdio. The CLI command `agentforge mcp serve` launches
this as a subprocess; the parent process (Claude Desktop, Cursor,
any MCP-aware tool) speaks JSON-RPC over stdin/stdout.

This is a server, not a client — Hermes Agent's own MCP client is
in `hermes_agent.tools.mcp_client`. They share the SDK but not code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import requests

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover — `mcp` is optional
    MCP_AVAILABLE = False
    # Type stubs so module-level imports don't fail before runtime
    # checks. These are only referenced after MCP_AVAILABLE is True.
    Server = None
    stdio_server = None
    Tool = None
    TextContent = None


logger = logging.getLogger("agentforge.mcp")


def _make_tool_handler(daemon_url: str, api_key: str):
    """Closure factory: returns the async handler for the MCP server.
    Holds the daemon URL + API key in a closure so the registered
    handler can stay a free function while still using them.
    """
    session = requests.Session()
    session.headers.update({"X-API-Key": api_key})

    def _do(method: str, path: str, **kw) -> dict[str, Any]:
        url = f"{daemon_url.rstrip('/')}{path}"
        try:
            resp = session.request(method, url, timeout=30, **kw)
        except requests.RequestException as e:
            return {"error": f"cannot reach daemon: {e}",
                    "url": url, "method": method}
        # Parse JSON body if present, else text. Always return the
        # status so the LLM caller can react to 4xx/5xx.
        try:
            body = resp.json() if resp.text else {}
        except ValueError:
            body = {"raw": resp.text[:500]}
        return {"status": resp.status_code, "body": body}

    return _do


async def _serve(daemon_url: str, api_key: str) -> None:
    """Main entry point. Registers tools and runs the stdio server.

    Args:
        daemon_url: Base URL of the running daemon (e.g. http://127.0.0.1:8765).
        api_key: X-API-Key for the daemon (tenant-bound).

    The `mcp` package must be installed (`pip install mcp`). If it
    isn't, this logs a clear error and exits 1 — the CLI's
    `mcp serve` subcommand checks MCP_AVAILABLE before calling.
    """
    if not MCP_AVAILABLE:
        raise RuntimeError(
            "mcp package is not installed. Run: uv pip install mcp"
        )
    do = _make_tool_handler(daemon_url, api_key)
    server = Server("agentforge")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_workflows",
                description=(
                    "List all workflow files available on the daemon. "
                    "Each entry has a `name` (use this in `run_workflow`), "
                    "a one-line `description`, and the `path` on disk. "
                    "Call this first to discover what's runnable."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="list_runs",
                description=(
                    "List runs for one workflow, newest first. "
                    "`workflow` is required; `limit` (1-500, default 50) "
                    "and `before` (ISO 8601 timestamp cursor) are optional. "
                    "Returns the run records with id, status, agent, "
                    "started_at, ended_at, duration_seconds, and error."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "workflow": {
                            "type": "string",
                            "description": "Workflow name (matches a file in the daemon's workflows dir).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max runs to return. 1-500, default 50.",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 500,
                        },
                        "before": {
                            "type": "string",
                            "description": "Cursor: only return runs with started_at < this ISO 8601 timestamp.",
                        },
                    },
                    "required": ["workflow"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="show_run",
                description=(
                    "Look up a single run by id. Returns the full run "
                    "record (all fields) or 404 if the run id is unknown."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "Run id (the `id` field from list_runs or run_workflow output).",
                        },
                    },
                    "required": ["run_id"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="run_workflow",
                description=(
                    "Trigger a workflow run. `name` is a workflow file "
                    "(use list_workflows to find one). `agent` is the "
                    "agent name that owns the inbox this workflow reads "
                    "from — the run executes that workflow's steps once "
                    "and returns the run id. The run is asynchronous "
                    "from the caller's POV: the response returns the "
                    "run_id immediately; poll list_runs / show_run to "
                    "see when it finishes."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Workflow name (e.g. 'support-bot').",
                        },
                        "agent": {
                            "type": "string",
                            "description": "Agent name that owns the inbox.",
                        },
                    },
                    "required": ["name", "agent"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="cancel_run",
                description=(
                    "Cancel a running workflow. The cancellation is "
                    "cooperative — the run stops at the next step "
                    "boundary, not mid-step. Returns 200 on success, "
                    "404 if the run is already finished, 403 if the run "
                    "is owned by a different tenant (defense-in-depth; "
                    "the API key is tenant-scoped, so this should be "
                    "rare)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "Run id to cancel.",
                        },
                        "workflow": {
                            "type": "string",
                            "description": "Workflow name the run belongs to (needed for the URL path).",
                        },
                    },
                    "required": ["run_id", "workflow"],
                    "additionalProperties": False,
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list:
        # Map tool name → HTTP call. Keep this dispatch table close to
        # the tool schemas above — if you add a tool, add a row here.
        if name == "list_workflows":
            data = do("GET", "/v1/workflows")
        elif name == "list_runs":
            wf = arguments.get("workflow", "")
            if not wf:
                return [TextContent(type="text",
                                    text=json.dumps({"error": "workflow is required"}))]
            params = {}
            if "limit" in arguments:
                params["limit"] = arguments["limit"]
            if "before" in arguments:
                params["before"] = arguments["before"]
            data = do("GET", f"/v1/runs?workflow={wf}", params=params)
        elif name == "show_run":
            run_id = arguments.get("run_id", "")
            data = do("GET", f"/v1/runs/{run_id}")
        elif name == "run_workflow":
            body = {
                "agent": arguments.get("agent", ""),
            }
            data = do("POST", f"/v1/workflows/{arguments['name']}/run", json=body)
        elif name == "cancel_run":
            data = do("POST", f"/v1/workflows/{arguments['workflow']}/runs/{arguments['run_id']}/cancel")
        else:
            data = {"error": f"unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(data, indent=2))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def main(daemon_url: str, api_key: Optional[str] = None) -> int:
    """Synchronous entry point for `agentforge mcp serve`.

    Reads AGENTFORGE_API_KEY from the environment if `api_key` is
    not given — most callers wire the key via env to avoid putting
    secrets in process listings.
    """
    key = api_key or os.environ.get("AGENTFORGE_API_KEY")
    if not key:
        print("error: AGENTFORGE_API_KEY not set (or pass --api-key)",
              file=__import__("sys").stderr)
        return 1
    if not MCP_AVAILABLE:
        print("error: mcp package not installed. uv pip install mcp",
              file=__import__("sys").stderr)
        return 1
    asyncio.run(_serve(daemon_url, key))
    return 0

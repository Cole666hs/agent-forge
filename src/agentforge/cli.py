"""agentforge.cli — Click-based command-line interface.

Subcommands:
  init <name>       Scaffold a new workflow project (workflow.yaml, .env.example)
  run <workflow>    Execute a workflow once (or with --watch, poll continuously)
  status            Show mailbox health + per-agent message counts

The CLI is the "deploy" surface of agentforge — every workflow that
runs in production is launched via `agentforge run`. The library code
(agentforge.core, agentforge.workflows, agentforge.adapters) is the
"build" surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import click
import yaml

from agentforge import __version__
from agentforge.core.mailbox import FileMailbox
from agentforge.workflows.engine import State, Workflow, WorkflowError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="agentforge")
@click.option("--mailbox-root", default="./mailbox", show_default=True,
              help="Mailbox root directory (overrides the per-command default).")
@click.option("--tenants", default="./tenants.json", show_default=True,
              help="Path to the tenant registry JSON file.")
@click.pass_context
def cli(ctx: click.Context, mailbox_root: str, tenants: str) -> None:
    """agentforge — self-hosted multi-agent orchestration."""
    # Stash on context so subcommands can pick them up
    ctx.ensure_object(dict)
    ctx.obj["mailbox_root"] = Path(mailbox_root)
    ctx.obj["tenants_path"] = Path(tenants)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit(0)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("name")
def init(name: str) -> None:
    """Scaffold a new workflow project in ./<name>/."""
    target = Path(name)
    if target.exists():
        click.echo(f"error: directory {name!r} already exists", err=True)
        sys.exit(1)
    target.mkdir(parents=True)
    # workflow.yaml — minimal viable workflow
    (target / "workflow.yaml").write_text(yaml.safe_dump({
        "name": name,
        "description": f"TODO: describe what {name} does",
        "steps": [
            {"id": "receive", "type": "receive"},
            {
                "id": "think",
                "type": "llm_call",
                "inputs": {
                    "system": "You are a helpful assistant.",
                    "user": "{{ receive.content }}",
                    "output_key": "think",
                },
            },
            {
                "id": "respond",
                "type": "respond",
                "inputs": {
                    "to": "{{ receive.from }}",
                    "content": "{{ think }}",
                },
            },
        ],
    }, sort_keys=False), encoding="utf-8")
    # .env.example — required env vars
    (target / ".env.example").write_text(
        "# Required: where the mailbox lives (file-based)\n"
        "MAILBOX_ROOT=./mailbox\n"
        "\n"
        "# Required for OpenRouter: free + paid models\n"
        "# OPENROUTER_API_KEY=sk-or-...\n"
        "\n"
        "# Optional: direct MiniMax API\n"
        "# MINIMAX_API_KEY=...\n"
        "\n"
        "# For local Ollama, no key needed; just run ollama serve locally\n",
        encoding="utf-8",
    )
    # .gitignore — keep state out of git
    (target / ".gitignore").write_text(
        "mailbox/\nstate.db\n.env\n__pycache__/\n*.pyc\n",
        encoding="utf-8",
    )
    click.echo(f"created {name}/")
    click.echo(f"  workflow.yaml  - edit this to define your agent")
    click.echo(f"  .env.example   - copy to .env and fill in secrets")
    click.echo(f"  .gitignore     - keeps state out of git")
    click.echo(f"\nnext: cd {name} && agentforge run workflow.yaml --agent mybot")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("workflow", type=click.Path(exists=False, dir_okay=False))
@click.option("--mailbox", default="./mailbox", show_default=True,
              help="Mailbox root directory (file-based transport).")
@click.option("--agent", required=True,
              help="Agent name — owns the inbox this workflow reads from.")
@click.option("--watch", is_flag=True, default=False,
              help="Poll the inbox continuously instead of running once.")
@click.option("--watch-interval", default=5, show_default=True,
              help="Seconds between inbox polls when --watch is set.")
def run(
    workflow: str,
    mailbox: str,
    agent: str,
    watch: bool,
    watch_interval: int,
) -> None:
    """Execute a workflow file.

    Without --watch, the workflow runs once and exits.
    With --watch, the workflow runs in a loop, polling the agent's inbox
    and executing the workflow for each unread message. Designed for
    systemd-managed long-running deployments.
    """
    wf_path = Path(workflow)
    if not wf_path.exists():
        click.echo(f"error: workflow file not found: {workflow}", err=True)
        sys.exit(1)
    # Load env if .env exists (best-effort)
    _load_env(wf_path.parent / ".env")
    # Make the mailbox root absolute so relative CWD changes don't break it
    mb_root = Path(mailbox).resolve()
    mb_root.mkdir(parents=True, exist_ok=True)
    mbox = FileMailbox(root=mb_root)
    wf = Workflow.from_yaml(wf_path)
    click.echo(f"loaded workflow: {wf.name} ({len(wf.steps)} steps)")
    try:
        if watch:
            asyncio.run(_watch_loop(wf, mbox, agent, watch_interval))
        else:
            state = State()
            asyncio.run(wf.run(state=state, mailbox=mbox, llm=None, agent_name=agent))
            click.echo(f"workflow completed. state keys: {sorted(state._data.keys())}")
    except WorkflowError as e:
        click.echo(f"workflow failed: {e}", err=True)
        sys.exit(2)
    except KeyboardInterrupt:
        click.echo("\ninterrupted", err=True)
        sys.exit(130)


async def _watch_loop(
    wf: Workflow,
    mbox: FileMailbox,
    agent: str,
    interval: int,
) -> None:
    """Poll the inbox and run the workflow for each unread message.

    Catches asyncio.CancelledError cleanly so SIGTERM/SIGINT (which the
    CLI converts to a task cancellation) exits the loop without leaving
    an orphan workflow state. The current step is allowed to finish via
    CancelledError propagation — the engine is async and checkpoints
    after each step, so partial work is recoverable on next start.
    """
    click.echo(f"watching {agent!r}'s inbox every {interval}s (Ctrl-C to stop)")
    try:
        while True:
            unread = mbox.list_inbox(agent, include_read=False, limit=1)
            if unread:
                click.echo(f"→ running workflow for message {unread[0].id}")
                state = State()
                try:
                    await wf.run(state=state, mailbox=mbox, llm=None, agent_name=agent)
                except WorkflowError as e:
                    click.echo(f"  workflow error: {e}", err=True)
                click.echo(f"  state keys after run: {sorted(state._data.keys())}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        click.echo("interrupted — shutting down watch loop", err=True)
        raise


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--mailbox", default="./mailbox", show_default=True,
              help="Mailbox root directory to inspect.")
def status(mailbox: str) -> None:
    """Show mailbox health and per-agent message counts."""
    mb_root = Path(mailbox).resolve()
    if not mb_root.exists():
        click.echo(f"mailbox not found: {mb_root}")
        click.echo("  run `agentforge init <project>` and then `agentforge run ...` first")
        return
    mbox = FileMailbox(root=mb_root)
    click.echo(f"mailbox root: {mb_root}")
    click.echo(f"  writable: {os.access(mb_root, os.W_OK)}")
    # Per-agent counts
    agents = sorted(p.name for p in mb_root.iterdir() if p.is_dir() and not p.name.startswith("_"))
    if not agents:
        click.echo("  agents: (none yet — send a message first)")
        return
    click.echo(f"  agents ({len(agents)}):")
    for name in agents:
        inbox = mbox.count_unread(name)
        click.echo(f"    - {name}: {inbox} unread")


# ---------------------------------------------------------------------------
# tenants
# ---------------------------------------------------------------------------

@cli.group()
@click.pass_context
def tenants(ctx: click.Context) -> None:
    """Manage tenants and their API keys."""
    pass


@tenants.command(name="add")
@click.argument("tenant_id")
@click.option("--api-key", default=None,
              help="Provide an API key instead of generating one. "
                   "If omitted, a random key is printed ONCE.")
@click.pass_context
def tenants_add(ctx: click.Context, tenant_id: str, api_key: str | None) -> None:
    """Register a new tenant."""
    from agentforge.tenants.registry import TenantRegistry
    reg = TenantRegistry(path=ctx.obj["tenants_path"])
    try:
        key = reg.add(tenant_id, api_key=api_key)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        ctx.exit(1)
    click.echo(f"tenant {tenant_id!r} registered.")
    click.echo(f"API key: {key}")
    click.echo("(store this now — it will not be shown again)")


@tenants.command(name="list")
@click.pass_context
def tenants_list(ctx: click.Context) -> None:
    """List all registered tenants."""
    from agentforge.tenants.registry import TenantRegistry
    reg = TenantRegistry(path=ctx.obj["tenants_path"])
    names = reg.list_tenants()
    if not names:
        click.echo("(no tenants — add one with `agentforge tenants add <id>`)")
        return
    for name in names:
        click.echo(f"  {name}")


@tenants.command(name="remove")
@click.argument("tenant_id")
@click.pass_context
def tenants_remove(ctx: click.Context, tenant_id: str) -> None:
    """Remove a tenant and revoke its API key."""
    from agentforge.tenants.registry import TenantRegistry
    reg = TenantRegistry(path=ctx.obj["tenants_path"])
    if reg.remove(tenant_id):
        click.echo(f"removed {tenant_id!r}")
    else:
        click.echo(f"error: tenant {tenant_id!r} not found", err=True)
        ctx.exit(1)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--state-db", default=None,
              help="Path to the SQLite state DB (default: <mailbox-root>/../state.db).")
@click.option("--workflows-dir", default=None,
              help="Directory of *.yaml workflows (default: <mailbox-root>/../workflows).")
@click.pass_context
def serve(
    ctx: click.Context,
    host: str,
    port: int,
    state_db: str | None,
    workflows_dir: str | None,
) -> None:
    """Run the FastAPI server (HTTP API with multi-tenant auth)."""
    try:
        import uvicorn
    except ImportError:
        click.echo("error: uvicorn not installed. pip install agent-forge[serve]", err=True)
        ctx.exit(1)
    from agentforge.serve import create_app
    mailbox_root = ctx.obj["mailbox_root"]
    app = create_app(
        tenants_path=ctx.obj["tenants_path"],
        mailbox_root=mailbox_root,
        state_db=Path(state_db) if state_db else None,
        workflows_dir=Path(workflows_dir) if workflows_dir else None,
    )
    click.echo(f"agentforge serving on http://{host}:{port}")
    click.echo(f"  mailbox: {mailbox_root}")
    click.echo(f"  tenants: {ctx.obj['tenants_path']}")
    uvicorn.run(app, host=host, port=port, log_level="info")


# ---------------------------------------------------------------------------
# env loader
# ---------------------------------------------------------------------------

def _load_env(path: Path) -> None:
    """Best-effort .env loader — no error if file is missing."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Console-script entry point for pyproject.toml [project.scripts]."""
    cli()


if __name__ == "__main__":
    main()

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
@click.pass_context
def cli(ctx: click.Context) -> None:
    """agentforge — self-hosted multi-agent orchestration."""
    if ctx.invoked_subcommand is None:
        # No subcommand → show help
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
    """Poll the inbox and run the workflow for each unread message."""
    click.echo(f"watching {agent!r}'s inbox every {interval}s (Ctrl-C to stop)")
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

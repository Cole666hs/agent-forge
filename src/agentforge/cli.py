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
from agentforge.adapters.llm import LLMError, make_provider
from agentforge.adapters.base import BaseLLMAdapter
from agentforge.core.mailbox import FileMailbox
from agentforge.observability.logging import configure_logging
from agentforge.workflows.engine import State, Workflow, WorkflowError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="agentforge")
@click.option("--mailbox-root", default="./mailbox", show_default=True,
              help="Mailbox root directory (overrides the per-command default).")
@click.option("--state-db", "state_db_path", default="./state.db", show_default=True,
              help="Path to the SQLite state database (tenants, usage, runs).")
@click.option("--log-format", default=None, envvar="AGENTFORGE_LOG_FORMAT",
              help='Log format: "json" or "text" (default: text, or $AGENTFORGE_LOG_FORMAT).')
@click.option("--log-level", default=None, envvar="AGENTFORGE_LOG_LEVEL",
              help='Log level: "DEBUG"|"INFO"|"WARNING"|"ERROR" (default: INFO, or $AGENTFORGE_LOG_LEVEL).')
@click.option("--daemon-url", default="http://127.0.0.1:8765", show_default=True,
              envvar="AGENTFORGE_DAEMON_URL",
              help="Base URL of a running `agentforge serve` daemon. Used by "
                   "subcommands that talk to the daemon (e.g. `runs cancel`).")
@click.option("--api-key", default=None,
              envvar="AGENTFORGE_API_KEY",
              help="X-API-Key for the daemon. Used by subcommands that talk to the "
                   "daemon. Prefer the env var over passing on the command line.")
@click.pass_context
def cli(ctx: click.Context, mailbox_root: str, state_db_path: str,
        log_format: str | None, log_level: str | None,
        daemon_url: str, api_key: str | None) -> None:
    """agentforge — self-hosted multi-agent orchestration."""
    # Stash on context so subcommands can pick them up
    ctx.ensure_object(dict)
    ctx.obj["mailbox_root"] = Path(mailbox_root)
    ctx.obj["state_db"] = Path(state_db_path)
    ctx.obj["daemon_url"] = daemon_url.rstrip("/")
    ctx.obj["api_key"] = api_key
    # Configure logging once at process start. Idempotent.
    configure_logging(fmt=log_format, level=log_level)
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
    click.echo("  workflow.yaml  - edit this to define your agent")
    click.echo("  .env.example   - copy to .env and fill in secrets")
    click.echo("  .gitignore     - keeps state out of git")
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
@click.option("--llm", default="auto", show_default=True,
              help='LLM provider: "auto"|"openrouter"|"minimax"|"ollama"|"" (none).')
@click.option("--tenant", default=None,
              help="Tenant ID for billing/quota enforcement (optional, pairs with --tenants + --usage).")
def run(
    workflow: str,
    mailbox: str,
    agent: str,
    watch: bool,
    watch_interval: int,
    llm: str,
    tenant: str | None,
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
    llm_provider = _resolve_llm(llm)
    if llm_provider is not None:
        click.echo(f"  llm: {type(llm_provider).__name__}")
        # If --tenant was given, wire billing/quota enforcement.
        # v0.6.0: tenants + usage live in the same SQLite state.db.
        if tenant:
            from agentforge.observability.instrumentation import instrument_llm
            from agentforge.observability.metrics import get_registry
            from agentforge.state import State as AppState
            app_state = AppState(state_db)
            registry = app_state.tenants
            usage = app_state.usage
            instrument_llm(
                llm_provider, registry=get_registry(),
                tenants=registry, usage=usage, tenant_id=tenant,
            )
            click.echo(f"  billing: enforced for tenant {tenant!r}")
    try:
        if watch:
            asyncio.run(_watch_loop(wf, mbox, agent, watch_interval, llm_provider))
        else:
            state = State()
            asyncio.run(wf.run(state=state, mailbox=mbox, llm=llm_provider, agent_name=agent))
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
    llm: BaseLLMAdapter | None,
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
                    await wf.run(state=state, mailbox=mbox, llm=llm, agent_name=agent)
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
    from agentforge.state import State as AppState
    state = AppState(ctx.obj["state_db"])
    try:
        key = state.tenants.add(tenant_id, api_key=api_key)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        ctx.exit(1)
    finally:
        state.close()
    click.echo(f"tenant {tenant_id!r} registered.")
    click.echo(f"API key: {key}")
    click.echo("(store this now — it will not be shown again)")


@tenants.command(name="list")
@click.pass_context
def tenants_list(ctx: click.Context) -> None:
    """List all registered tenants."""
    from agentforge.state import State as AppState
    state = AppState(ctx.obj["state_db"])
    try:
        names = state.tenants.list_tenants()
    finally:
        state.close()
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
    from agentforge.state import State as AppState
    state = AppState(ctx.obj["state_db"])
    try:
        removed = state.tenants.remove(tenant_id)
    finally:
        state.close()
    if removed:
        click.echo(f"removed {tenant_id!r}")
    else:
        click.echo(f"error: tenant {tenant_id!r} not found", err=True)
        ctx.exit(1)


@tenants.command(name="set-plan")
@click.argument("tenant_id")
@click.option("--plan", required=True,
              type=click.Choice(["free", "pro", "enterprise"]),
              help="New plan tier.")
@click.pass_context
def tenants_set_plan(ctx: click.Context, tenant_id: str, plan: str) -> None:
    """Change a tenant's plan tier."""
    from agentforge.billing.plans import Plan
    from agentforge.state import State as AppState
    state = AppState(ctx.obj["state_db"])
    try:
        state.tenants.set_plan(tenant_id, Plan(plan))
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        ctx.exit(1)
        return
    finally:
        state.close()
    click.echo(f"tenant {tenant_id!r} plan set to {plan}")


@tenants.command(name="usage")
@click.argument("tenant_id")
@click.pass_context
def tenants_usage(ctx: click.Context, tenant_id: str) -> None:
    """Show current-month token usage and quota for a tenant."""
    from agentforge.billing.plans import PLAN_LIMITS
    from agentforge.billing.quota import quota_status
    from agentforge.state import State as AppState
    state = AppState(ctx.obj["state_db"])
    try:
        try:
            qs = quota_status(state.tenants, state.usage, tenant_id)
        except ValueError as e:
            click.echo(f"error: {e}", err=True)
            ctx.exit(1)
            return
        limit = PLAN_LIMITS[qs.plan]
        limit_str = "unlimited" if limit is None else f"{limit:,}"
        remaining_str = "unlimited" if qs.remaining is None else f"{qs.remaining:,}"
        pct_str = "n/a" if limit is None else f"{qs.pct * 100:.1f}%"
        warning_marker = " [WARNING]" if qs.warning else ""
        exceeded_marker = " [EXCEEDED]" if qs.exceeded else ""
        click.echo(
            f"tenant:    {qs.tenant_id}\n"
            f"plan:      {qs.plan.value}\n"
            f"used:      {qs.used:,} tokens\n"
            f"limit:     {limit_str} tokens\n"
            f"remaining: {remaining_str} tokens\n"
            f"percent:   {pct_str}{warning_marker}{exceeded_marker}"
        )
    finally:
        state.close()


# ---------------------------------------------------------------------------
# runs — inspect run history from the terminal (v0.9.0)
# ---------------------------------------------------------------------------
# Lets a user SSH into a server and look at what's been happening
# without opening a browser. Two subcommands:
#
#   agentforge runs ls <workflow> [--limit N]   ASCII table of recent runs
#   agentforge runs show <run_id>               Detailed run + event timeline
#
# `cancel` is NOT here in v0.9.0 — cancellation is in-process
# (active_runs lives in the daemon's memory), and the CLI is a
# separate process. A cross-process cancel would need a DB-backed
# cancellation queue polled by the daemon; that's v0.10.0 work.
# ---------------------------------------------------------------------------


@cli.group()
@click.pass_context
def runs(ctx: click.Context) -> None:
    """Inspect run history from the terminal."""
    pass


@runs.command(name="ls")
@click.argument("workflow")
@click.option("--limit", default=20, show_default=True,
              help="Max number of runs to show.")
@click.option("--status", default=None,
              type=click.Choice(["success", "error", "cancelled", "quota_exceeded"], case_sensitive=False),
              help="Filter by terminal status.")
@click.pass_context
def runs_ls(ctx: click.Context, workflow: str, limit: int, status: str | None) -> None:
    """List recent runs for one workflow (newest first)."""
    from agentforge.state import State as AppState
    state = AppState(ctx.obj["state_db"])
    try:
        rows = state.runs.list_runs(workflow, limit=limit * 4)  # over-fetch if filtering
    finally:
        state.close()
    if status is not None:
        rows = [r for r in rows if r.status == status][:limit]
    if not rows:
        click.echo(
            f"(no runs for workflow {workflow!r}"
            + (f" with status={status!r}" if status else "")
            + ")"
        )
        return
    # Compact fixed-width table for terminal use.
    click.echo(f"{'run_id':<18}  {'agent':<14}  {'status':<14}  {'duration':>10}  started")
    click.echo("-" * 80)
    for r in rows:
        click.echo(
            f"{r.id:<18}  {r.agent:<14}  {r.status:<14}  "
            f"{r.duration_seconds:>9.3f}s  {r.started_at}"
        )


@runs.command(name="show")
@click.argument("run_id")
@click.pass_context
def runs_show(ctx: click.Context, run_id: str) -> None:
    """Show a run record + the full event timeline (newest first)."""
    import json as _json
    from agentforge.state import State as AppState
    state = AppState(ctx.obj["state_db"])
    try:
        run = state.runs.get_run(run_id)
        if run is None:
            click.echo(f"error: run {run_id!r} not found", err=True)
            ctx.exit(1)
            return
        events = state.runs.events.events_for_run(run_id)
    finally:
        state.close()
    # Run header.
    error_str = f"\n  error:    {run.error[:200]}" if run.error else ""
    click.echo(
        f"run:      {run.id}\n"
        f"workflow: {run.workflow}\n"
        f"agent:    {run.agent}\n"
        f"status:   {run.status}\n"
        f"started:  {run.started_at}\n"
        f"ended:    {run.ended_at}\n"
        f"duration: {run.duration_seconds:.3f}s"
        f"{error_str}"
    )
    if not events:
        click.echo(
            "\n(no events recorded for this run — the event log was "
            "added in v0.7.0; older runs have no events)"
        )
        return
    click.echo(f"\nevent timeline ({len(events)} events):")
    for ev in events:
        # Compact one-line summary per event, payload as JSON on the next
        # line. Easy to scan in the terminal, easy to grep.
        click.echo(f"  #{ev.seq:>5}  {ev.ts}  {ev.kind}")
        if ev.payload:
            click.echo("           payload: " + _json.dumps(ev.payload, ensure_ascii=False))


@runs.command(name="logs")
@click.argument("run_id")
@click.option("--follow/--no-follow", default=True,
              help="Keep streaming until the run reaches a terminal state "
                   "(default: follow). --no-follow prints all stored events and exits.")
@click.option("--since", default=0, type=int,
              help="Skip events with seq <= N (useful for reconnect-resume).")
@click.pass_context
def runs_logs(ctx: click.Context, run_id: str, follow: bool, since: int) -> None:
    """Tail the event log of a run (Server-Sent Events stream).

    Connects to the daemon's /v1/runs/{id}/logs endpoint and prints
    each event as it arrives. With --follow (the default), blocks
    until the run reaches a terminal state and the server emits a
    `done` frame. With --no-follow, prints all stored events and
    exits — useful for post-mortem on completed runs.

    Output format (one line per event, tab-separated, easy to grep):

        seq=42  kind=started          ts=2026-06-15T10:00:01+00:00
        seq=43  kind=llm_call_started ts=2026-06-15T10:00:01+00:00
        seq=44  kind=llm_call_completed ts=2026-06-15T10:00:02+00:00
        seq=45  kind=finished         status=success
        seq=46  kind=done             status=success

    Exit codes:
      0  stream ended naturally (run reached terminal state or --no-follow)
      1  run not found in local state.db, or 404 from daemon
      2  daemon unreachable or other transport error
    """
    import requests as _req
    from agentforge.state import State as AppState
    # Local lookup first (mirrors runs_cancel). We use this to give
    # the right error before talking to the daemon, and to confirm
    # the run_id is at least known to the local state.
    state = AppState(ctx.obj["state_db"])
    try:
        run = state.runs.get_run(run_id)
    finally:
        state.close()
    if run is None:
        click.echo(f"error: run {run_id!r} not found in state.db", err=True)
        ctx.exit(1)
        return
    api_key = ctx.obj.get("api_key")
    if not api_key:
        click.echo(
            "error: no API key. Set $AGENTFORGE_API_KEY or pass --api-key.",
            err=True,
        )
        ctx.exit(1)
        return
    daemon_url = ctx.obj["daemon_url"]
    url = f"{daemon_url}/v1/runs/{run_id}/logs"
    params = {"follow": "true" if follow else "false", "since": since}
    try:
        # stream=True so we read line-by-line. timeout=None on the
        # read: the server sends heartbeats every 1s on quiet runs,
        # so a 30s timeout here would still catch a dead connection.
        resp = _req.get(
            url, headers={"X-API-Key": api_key}, params=params,
            stream=True, timeout=(5, 30),
        )
    except _req.RequestException as e:
        click.echo(f"error: cannot reach daemon at {daemon_url}: {e}", err=True)
        ctx.exit(2)
        return
    if resp.status_code == 404:
        click.echo(
            f"error: run {run_id!r} not found (or not owned by this tenant)",
            err=True,
        )
        ctx.exit(1)
        return
    if resp.status_code == 401:
        click.echo("error: API key rejected by daemon (401)", err=True)
        ctx.exit(1)
        return
    if resp.status_code != 200:
        click.echo(
            f"error: daemon returned {resp.status_code}: {resp.text[:300]}",
            err=True,
        )
        ctx.exit(2)
        return
    # Parse SSE: each event is `data: <json>\n\n` (or `: keepalive\n\n`).
    # We use iter_lines and accumulate lines until we see a blank
    # line (= end of one SSE record). Heartbeats (lines starting with
    # ':') are silently dropped.
    buf: list[str] = []
    seen_done = False
    try:
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            if raw == "":
                # End of one SSE record. Flush.
                if buf:
                    payload = "\n".join(buf).lstrip()
                    if payload.startswith("data:"):
                        json_str = payload[len("data:"):].strip()
                        try:
                            ev = json.loads(json_str)
                        except json.JSONDecodeError:
                            # Malformed event from the server. Print
                            # and continue; don't crash the stream.
                            click.echo(f"warn: malformed event: {json_str[:200]}", err=True)
                            buf = []
                            continue
                        if ev.get("kind") == "done":
                            # Terminal frame. Print + exit cleanly.
                            click.echo(
                                f"seq=-  kind=done  status={ev.get('status','unknown')}"
                            )
                            seen_done = True
                            buf = []
                            return
                        click.echo(_format_event_line(ev))
                    # ignore other prefixes (event:, id:, retry:, etc.)
                buf = []
            elif raw.startswith(":"):
                # SSE comment (heartbeat). Drop silently.
                continue
            else:
                buf.append(raw)
    except KeyboardInterrupt:
        # User pressed Ctrl-C. Close cleanly.
        click.echo("(interrupted)", err=True)
    finally:
        resp.close()
    if not seen_done and not follow:
        # --no-follow: we got all the events, but the server doesn't
        # emit a `done` frame because it returns immediately after
        # the replay. That's expected.
        return
    if not seen_done:
        # Stream ended without a `done` frame. Probably the server
        # closed the connection (shutdown, run cancelled remotely,
        # etc.). Exit 0 anyway — the data we have is consistent.
        return


def _format_event_line(ev: dict) -> str:
    """Render one SSE event dict as a single line for stdout.

    Stable column order (seq, kind, ts, payload) so it's grep-friendly.
    """
    seq = ev.get("seq", "-")
    kind = ev.get("kind", "?")
    ts = ev.get("ts", "")
    payload = ev.get("payload") or {}
    # Flatten a couple of common payload keys into the same line for
    # scanning; the rest stays in a compact JSON blob.
    extras = []
    for k in ("status", "duration_seconds", "agent", "step_id", "error"):
        if k in payload:
            extras.append(f"{k}={payload[k]}")
    if extras and len(payload) > len(extras):
        extras.append("payload=" + json.dumps(payload, ensure_ascii=False))
    elif extras:
        pass  # only known keys, no need for the full payload blob
    else:
        if payload:
            extras.append("payload=" + json.dumps(payload, ensure_ascii=False))
    extra_str = "  " + "  ".join(extras) if extras else ""
    return f"seq={seq:<5}  kind={kind:<22}  ts={ts}{extra_str}"


@runs.command(name="cancel")
@click.argument("run_id")
@click.pass_context
def runs_cancel(ctx: click.Context, run_id: str) -> None:
    """Cancel a running workflow by run_id.

    Looks up the run in the local SQLite state.db to find which workflow
    it belongs to, then POSTs to the daemon's cancel endpoint. The
    daemon's cancel handler (v0.8.0) does the actual work: it checks
    ownership (403 if the run belongs to another tenant), sets the
    in-process asyncio.Event, and audits the attempt (INFO on success,
    WARNING on cross-tenant rejection). The run is stopped at the next
    step boundary, not mid-step.

    Why HTTP, not direct DB writes? The cancel handler is in-process
    state in the daemon — it can't be replicated into the CLI without
    a shared queue (Redis/DB polling). HTTP keeps the audit log and
    ownership check in one place. The CLI is a thin caller.

    Exit codes:
      0  cancellation requested (200) or the run was already finished
         (404 — the desired state is reached either way)
      1  the run is owned by a different tenant (403)
      2  the daemon is unreachable or returned an unexpected status
    """
    import requests as _req
    from agentforge.state import State as AppState
    # Step 1: local lookup so we know which workflow to address.
    state = AppState(ctx.obj["state_db"])
    try:
        run = state.runs.get_run(run_id)
    finally:
        state.close()
    if run is None:
        click.echo(f"error: run {run_id!r} not found in state.db", err=True)
        ctx.exit(1)
        return
    api_key = ctx.obj.get("api_key")
    if not api_key:
        click.echo(
            "error: no API key. Set $AGENTFORGE_API_KEY or pass --api-key.",
            err=True,
        )
        ctx.exit(1)
        return
    daemon_url = ctx.obj["daemon_url"]
    url = f"{daemon_url}/v1/workflows/{run.workflow}/runs/{run_id}/cancel"
    try:
        resp = _req.post(url, headers={"X-API-Key": api_key}, timeout=10)
    except _req.RequestException as e:
        click.echo(f"error: cannot reach daemon at {daemon_url}: {e}", err=True)
        ctx.exit(2)
        return
    if resp.status_code == 200:
        click.echo(f"cancellation requested for run {run_id} (workflow {run.workflow!r})")
        return
    if resp.status_code == 404:
        # Run is not in flight — already finished or never existed in
        # this daemon's memory. Treat as success: the desired state
        # (run is no longer running) is reached.
        click.echo(f"run {run_id} is not active (already finished or never existed)")
        return
    if resp.status_code == 403:
        click.echo(
            f"error: run {run_id!r} is not owned by this tenant",
            err=True,
        )
        ctx.exit(1)
        return
    if resp.status_code == 401:
        click.echo("error: API key rejected by daemon (401)", err=True)
        ctx.exit(1)
        return
    # Anything else is unexpected — surface the body so the user can
    # report it without digging through server logs.
    click.echo(
        f"error: daemon returned {resp.status_code}: {resp.text[:300]}",
        err=True,
    )
    ctx.exit(2)


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
    # v0.6.0: `tenants_path` is the JSON file used only as the
    # source for a one-shot migration to the SQLite state.db.
    # The serve subcommand's --state-db flag wins; otherwise we
    # default to <mailbox-root>/../state.db.
    legacy_tenants = ctx.obj["state_db"].with_name("tenants.json")
    app = create_app(
        tenants_path=legacy_tenants,
        mailbox_root=mailbox_root,
        state_db=Path(state_db) if state_db else None,
        workflows_dir=Path(workflows_dir) if workflows_dir else None,
    )
    click.echo(f"agentforge serving on http://{host}:{port}")
    click.echo(f"  mailbox:     {mailbox_root}")
    click.echo(f"  state.db:    {state_db or mailbox_root.parent / 'state.db'}")
    click.echo(f"  legacy JSON: {legacy_tenants} (one-shot migration source)")
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


def _resolve_llm(spec: str | None) -> BaseLLMAdapter | None:
    """Construct an LLM provider from --llm spec, or auto-detect.

    Spec format: ``"openrouter"`` | ``"minimax"`` | ``"ollama"`` | ``"auto"``
    (default). ``"auto"`` picks a provider in priority order — providers
    whose env var is set win, and ollama (no env needed) is the last-resort
    fallback. Returns ``None`` only if the spec is explicitly empty —
    workflows without ``llm_call`` steps don't need a provider.
    """
    if spec is None or spec == "":
        return None
    if spec == "auto":
        from agentforge.adapters.llm_compat import BaseOpenAICompatLLMAdapter
        # Prefer providers with real env vars set (cheaper, deterministic).
        # Ollama is the fallback — it always works if ollama serve is up.
        for cls in BaseOpenAICompatLLMAdapter.__subclasses__():
            if cls.ENV_API_KEY and os.environ.get(cls.ENV_API_KEY):
                return cls()
        # No env-keyed provider found — try ollama
        try:
            return make_provider("ollama")
        except LLMError:
            pass
        raise click.UsageError(
            "no LLM provider auto-detected. Set OPENROUTER_API_KEY, "
            "MINIMAX_API_KEY, or start ollama serve."
        )
    try:
        return make_provider(spec)
    except LLMError as e:
        raise click.UsageError(str(e)) from None


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------
# v0.11.0: MCP server subcommand. Runs an stdio-based MCP server that
# exposes agentforge operations (list workflows, list/show runs, run,
# cancel) as tools for Claude Desktop, Cursor, or any MCP-aware tool.
# See agentforge/mcp.py for the implementation.


@cli.group()
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Run an MCP server exposing agentforge operations as tools."""
    pass


@mcp.command(name="serve")
@click.pass_context
def mcp_serve(ctx: click.Context) -> None:
    """Start the MCP server on stdio. Configure Claude Desktop / Cursor
    to launch this command and the agent-forge operations become tools.
    """
    from agentforge.mcp import MCP_AVAILABLE, main as mcp_main
    if not MCP_AVAILABLE:
        click.echo(
            "error: mcp package not installed. uv pip install mcp",
            err=True,
        )
        ctx.exit(1)
        return
    rc = mcp_main(daemon_url=ctx.obj["daemon_url"], api_key=ctx.obj.get("api_key"))
    ctx.exit(rc)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Console-script entry point for pyproject.toml [project.scripts]."""
    cli()


if __name__ == "__main__":
    main()

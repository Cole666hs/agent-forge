"""Unit tests for agentforge.cli — Click-based command-line interface.

Uses click.testing.CliRunner to invoke the CLI in-process. No subprocess,
no real filesystem pollution — CliRunner.isolated_filesystem() sandboxes
writes to a temp dir.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import yaml
from click.testing import CliRunner

from agentforge.cli import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Top-level help / no-args
# ---------------------------------------------------------------------------

def test_cli_no_args_shows_help(runner: CliRunner):
    result = runner.invoke(cli, [])
    assert result.exit_code == 0
    # Help text should mention the subcommands
    assert "init" in result.output
    assert "run" in result.output
    assert "status" in result.output


def test_cli_help_flag(runner: CliRunner):
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


# ---------------------------------------------------------------------------
# init — scaffold a project
# ---------------------------------------------------------------------------

def test_init_creates_project_files(runner: CliRunner):
    with runner.isolated_filesystem() as fs:
        result = runner.invoke(cli, ["init", "mybot"])
        assert result.exit_code == 0, result.output
        project = Path(fs) / "mybot"
        assert (project / "workflow.yaml").exists()
        assert (project / ".env.example").exists()
        # workflow.yaml is a valid YAML with a name field
        data = yaml.safe_load((project / "workflow.yaml").read_text())
        assert data["name"] == "mybot"
        # .env.example documents required env vars
        env_text = (project / ".env.example").read_text()
        assert "OPENROUTER_API_KEY" in env_text or "MAILBOX_ROOT" in env_text


def test_init_refuses_existing_directory(runner: CliRunner):
    with runner.isolated_filesystem() as fs:
        Path(fs, "mybot").mkdir()
        result = runner.invoke(cli, ["init", "mybot"])
        assert result.exit_code != 0
        assert "exists" in result.output.lower() or "already" in result.output.lower()


# ---------------------------------------------------------------------------
# run — execute a workflow
# ---------------------------------------------------------------------------

def test_run_with_no_workflow_file_errors(runner: CliRunner):
    with runner.isolated_filesystem() as fs:
        # --agent is required; pass it so the workflow-file check is the
        # first error to surface.
        result = runner.invoke(cli, ["run", "nonexistent.yaml", "--agent", "x"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "no such" in result.output.lower()


def test_run_executes_workflow(runner: CliRunner, tmp_path: Path):
    """End-to-end: write a workflow.yaml, run it, verify state was executed."""
    # Build a minimal workflow that just receives + responds
    wf_yaml = tmp_path / "wf.yaml"
    wf_yaml.write_text(yaml.safe_dump({
        "name": "test",
        "steps": [
            {"id": "receive", "type": "receive"},
            {"id": "respond", "type": "respond",
             "inputs": {"to": "user", "content": "got: {{ receive.content }}"}},
        ],
    }))
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    # Pre-seed an inbox message
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    mbox = FileMailbox(root=mailbox)
    mbox.send(Message(from_="user", to="bot", content="hello"))

    result = runner.invoke(cli, [
        "run", str(wf_yaml),
        "--mailbox", str(mailbox),
        "--agent", "bot",
    ])
    assert result.exit_code == 0, result.output
    # The respond step should have written to user's inbox
    user_inbox = mbox.list_inbox("user", include_read=True)
    assert any("got: hello" in m.content for m in user_inbox)


# ---------------------------------------------------------------------------
# status — show mailbox stats
# ---------------------------------------------------------------------------

def test_status_shows_mailbox_stats(runner: CliRunner, tmp_path: Path):
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    mbox = FileMailbox(root=tmp_path / "mailbox")
    mbox.send(Message(from_="user", to="bot", content="hi"))

    result = runner.invoke(cli, ["status", "--mailbox", str(tmp_path / "mailbox")])
    assert result.exit_code == 0, result.output
    # Output should mention the bot agent
    assert "bot" in result.output

"""Unit tests for agentforge.cli — Click-based command-line interface.

Uses click.testing.CliRunner to invoke the CLI in-process. No subprocess,
no real filesystem pollution — CliRunner.isolated_filesystem() sandboxes
writes to a temp dir.
"""

from __future__ import annotations

import json
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
# tenants — multi-tenant management
# ---------------------------------------------------------------------------

def test_tenants_add_prints_generated_key(runner: CliRunner):
    """agentforge tenants add <id> creates the tenant and prints the
    generated API key (one-time display, like moltbook)."""
    with runner.isolated_filesystem() as fs:
        tenants_path = Path(fs) / "tenants.json"
        result = runner.invoke(cli, [
            "--tenants", str(tenants_path), "tenants", "add", "acme",
        ])
        assert result.exit_code == 0, result.output
        assert "acme" in result.output
        assert "API key:" in result.output
        # Key was actually written
        import json
        data = json.loads(tenants_path.read_text())
        assert "acme" in data["tenants"]


def test_tenants_list_shows_added_tenants(runner: CliRunner):
    with runner.isolated_filesystem() as fs:
        tenants_path = Path(fs) / "tenants.json"
        # Add two
        runner.invoke(cli, ["--tenants", str(tenants_path), "tenants", "add", "acme"])
        runner.invoke(cli, ["--tenants", str(tenants_path), "tenants", "add", "corp"])
        result = runner.invoke(cli, ["--tenants", str(tenants_path), "tenants", "list"])
        assert result.exit_code == 0
        assert "acme" in result.output
        assert "corp" in result.output


def test_tenants_remove(runner: CliRunner):
    with runner.isolated_filesystem() as fs:
        tenants_path = Path(fs) / "tenants.json"
        runner.invoke(cli, ["--tenants", str(tenants_path), "tenants", "add", "acme"])
        result = runner.invoke(cli, ["--tenants", str(tenants_path), "tenants", "remove", "acme"])
        assert result.exit_code == 0
        # Subsequent list should be empty
        result = runner.invoke(cli, ["--tenants", str(tenants_path), "tenants", "list"])
        assert "acme" not in result.output


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
# --llm wiring (Phase 6.5: CLI LLM hookup)
# ---------------------------------------------------------------------------

def test_run_auto_resolves_llm_from_env(runner: CliRunner, tmp_path: Path, monkeypatch):
    """--llm auto (default) reads OPENROUTER_API_KEY from env and constructs
    a provider. Workflows with llm_call steps must reach the step, not fail
    on 'no LLM provider configured'."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-fake")
    wf_yaml = tmp_path / "wf.yaml"
    wf_yaml.write_text(yaml.safe_dump({
        "name": "llmtest",
        "steps": [
            {"id": "receive", "type": "receive"},
            # Use a fake chat() implementation so we don't hit the network
            {"id": "think", "type": "llm_call",
             "inputs": {"system": "sys", "user": "{{ receive.content }}", "output_key": "think"}},
            {"id": "respond", "type": "respond",
             "inputs": {"to": "user", "content": "echo: {{ think }}"}},
        ],
    }))
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    FileMailbox(root=mailbox).send(Message(from_="user", to="bot", content="hi"))

    # Patch urllib.request.urlopen with a context-manager mock that
    # returns a fake OpenAI-compat completion body.
    fake_body = json.dumps({
        "choices": [{"message": {"content": "  hi from fake llm  "}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4},
    }).encode("utf-8")

    class _MockResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return fake_body

    with mock.patch("urllib.request.urlopen", return_value=_MockResp()):
        result = runner.invoke(cli, [
            "run", str(wf_yaml),
            "--mailbox", str(mailbox),
            "--agent", "bot",
        ])

    assert result.exit_code == 0, result.output
    assert "OpenRouterAdapter" in result.output
    user_inbox = FileMailbox(root=mailbox).list_inbox("user", include_read=True)
    assert any("hi from fake llm" in m.content for m in user_inbox), user_inbox


def test_run_explicit_no_llm_skips_llm_construction(runner: CliRunner, tmp_path: Path, monkeypatch):
    """--llm '' (empty) explicitly disables LLM. Workflows with no llm_call
    step must still work without an LLM provider."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    wf_yaml = tmp_path / "wf.yaml"
    wf_yaml.write_text(yaml.safe_dump({
        "name": "nollm",
        "steps": [
            {"id": "receive", "type": "receive"},
            {"id": "respond", "type": "respond",
             "inputs": {"to": "user", "content": "no llm needed"}},
        ],
    }))
    mailbox = tmp_path / "mailbox"
    mailbox.mkdir()
    from agentforge.core.mailbox import FileMailbox
    from agentforge.core.message import Message
    FileMailbox(root=mailbox).send(Message(from_="user", to="bot", content="x"))

    result = runner.invoke(cli, [
        "run", str(wf_yaml),
        "--mailbox", str(mailbox),
        "--agent", "bot",
        "--llm", "",
    ])
    assert result.exit_code == 0, result.output
    # The "llm: ..." line is suppressed when provider is None
    assert "llm:" not in result.output


def test_run_unknown_llm_spec_errors_cleanly(runner: CliRunner, tmp_path: Path):
    """--llm garbage prints a clear usage error, not a stacktrace."""
    wf_yaml = tmp_path / "wf.yaml"
    wf_yaml.write_text(yaml.safe_dump({"name": "x", "steps": []}))
    result = runner.invoke(cli, [
        "run", str(wf_yaml),
        "--mailbox", str(tmp_path / "mailbox"),
        "--agent", "x",
        "--llm", "garbage",
    ])
    assert result.exit_code != 0
    assert "unknown provider" in result.output.lower() or "usage" in result.output.lower()


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

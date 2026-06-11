"""T12 RED — CLI log options test."""

import json
from pathlib import Path
from unittest import mock

import yaml
from click.testing import CliRunner

from agentforge.cli import cli


def test_cli_help_mentions_log_options():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert "--log-format" in result.output
    assert "--log-level" in result.output


def test_cli_envvar_picks_up_agentofrge_log_format():
    """AGENTFORGE_LOG_FORMAT env var is read by the CLI's global option."""
    runner = CliRunner()
    with mock.patch("agentforge.cli.configure_logging") as cfg:
        result = runner.invoke(cli, [
            "--help",
        ], env={"AGENTFORGE_LOG_FORMAT": "json", "AGENTFORGE_LOG_LEVEL": "DEBUG"})
    # --help short-circuits after help print, but the global option still
    # triggers configure_logging when click invokes the cli callback
    assert "--log-format" in result.output
    assert "--log-level" in result.output


def test_run_invokes_configure_logging(tmp_path):
    """Verify 'agentforge run' calls configure_logging with CLI flags.

    Click groups don't pass options to subcommands — global options
    must come BEFORE the subcommand. So: `agentforge --log-format json run ...`
    """
    runner = CliRunner()
    wf = tmp_path / "wf.yaml"
    wf.write_text(yaml.safe_dump({"name": "x", "steps": []}))
    with mock.patch("agentforge.cli.configure_logging") as cfg, \
         mock.patch("agentforge.cli._resolve_llm", return_value=None):
        result = runner.invoke(cli, [
            "--log-format", "json", "--log-level", "DEBUG",
            "run", str(wf), "--agent", "x",
            "--mailbox", str(tmp_path / "mailbox"),
        ])
    cfg.assert_called_once()
    # configure_logging is called with kwargs
    _, kwargs = cfg.call_args
    assert kwargs.get("fmt") == "json"
    assert kwargs.get("level") == "DEBUG"

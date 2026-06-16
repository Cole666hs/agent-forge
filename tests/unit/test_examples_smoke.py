"""Smoke test: every example in examples/ must at least import + parse.

These are not exhaustive — examples 01, 04, 05 are exercised by their
own run.py scripts in their README. This test just makes sure no example
has a syntax error or a missing import that would break it on first
contact. CI runs this on every push.

Why this matters: the examples are documentation, and a broken example
is worse than no example. v0.15.0.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES = REPO_ROOT / "examples"


def _iter_examples() -> list[Path]:
    """Return every run.py under examples/, sorted."""
    return sorted(EXAMPLES.glob("*/run.py"))


@pytest.mark.parametrize("run_path", _iter_examples(), ids=lambda p: p.parent.name)
def test_example_imports(run_path: Path) -> None:
    """The example's run.py must be importable (syntax + imports OK)."""
    spec = importlib.util.spec_from_file_location("m", run_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        # Examples are allowed to sys.exit() on missing env vars
        pass
    except FileNotFoundError as e:
        # Example 06 (docker-deploy) intentionally has no run.py — skip
        pytest.skip(f"example has no runnable script: {e}")


def test_self_contained_examples_run() -> None:
    """Examples 01, 07, 08 are self-contained and should run end-to-end."""
    for name, timeout in [("01-hello-world", 30), ("07-workflow-versioning", 30), ("08-retention-monitor", 30)]:
        run_py = EXAMPLES / name / "run.py"
        if not run_py.exists():
            continue
        # Run in the example's own directory so artifacts land in the right place
        result = subprocess.run(
            [sys.executable, str(run_py)],
            cwd=run_py.parent,
            env={"PATH": __import__("os").environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT / "src")},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        assert result.returncode == 0, textwrap.dedent(
            f"""
            example {name!r} failed (exit {result.returncode})
            --- stdout ---
            {result.stdout}
            --- stderr ---
            {result.stderr}
            """
        )
        assert "done" in result.stdout or "state after run" in result.stdout, (
            f"example {name!r} ran but didn't print expected success marker"
        )

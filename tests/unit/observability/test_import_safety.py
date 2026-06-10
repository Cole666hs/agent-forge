"""Side-effect-free import contract.

Importing the observability subpackage must not register log handlers,
spawn threads, or initialize the global metrics registry. We rely on
this to keep `import agentforge` cheap and safe in unit tests.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap


def test_observability_import_registers_no_log_handlers():
    """After import, the agentforge logger tree has no handlers attached."""
    # Import the package fresh in this process
    import agentforge.observability  # noqa: F401

    root = logging.getLogger("agentforge")
    # The agentforge logger exists (or is created lazily); check for handlers
    assert root.handlers == [], (
        f"agentforge logger has handlers after import: {root.handlers!r}"
    )


def test_observability_subprocess_import_does_not_print():
    """Run a fresh subprocess that imports the package; stderr should be empty."""
    code = textwrap.dedent("""
        import agentforge.observability  # noqa: F401
    """)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"import raised: {result.stderr}"
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"

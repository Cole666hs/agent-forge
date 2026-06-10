"""Import-safety contract — the library must not run anything on import."""

from __future__ import annotations

import importlib
import signal

import agentforge


def test_import_does_not_register_signal_handlers():
    """No SIGTERM/SIGINT handlers installed by import."""
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)
    importlib.reload(agentforge)
    after_int = signal.getsignal(signal.SIGINT)
    after_term = signal.getsignal(signal.SIGTERM)
    assert before_int == after_int
    assert before_term == after_term


def test_import_exposes_all_attribute():
    assert hasattr(agentforge, "__all__")
    assert isinstance(agentforge.__all__, list)


def test_version_is_semver_string():
    assert isinstance(agentforge.__version__, str)
    parts = agentforge.__version__.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts)

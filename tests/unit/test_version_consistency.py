"""Version-drift regression tests (v0.18.0).

Before this version, `agentforge.__version__`, `pyproject.toml [version]`,
`FastAPI(title=..., version=...)`, and the OTel `service_version` were
each maintained by hand. They drifted apart — `__version__` was stuck
at 0.11.0 while the package was actually 0.17.0. This test pins all
of them to a single source of truth so the drift can't happen again.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _pyproject_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["project"]["version"]


def test_pyproject_version_matches_runtime() -> None:
    import agentforge

    pyproject = _pyproject_version()
    runtime = agentforge.__version__
    assert pyproject == runtime, (
        f"pyproject.toml version={pyproject!r} but "
        f"agentforge.__version__={runtime!r}. Bump one or the other."
    )


def test_pyproject_version_matches_fastapi_app() -> None:
    """The FastAPI app's `version=` field must match the package version."""
    from fastapi.testclient import TestClient

    from agentforge.serve import create_app
    from agentforge.tenants.registry import TenantRegistry

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tenants = TenantRegistry(path=tmp_path / "tenants.json")
        api_key = tenants.add("acme")
        app = create_app(
            tenants_path=tenants.path,
            mailbox_root=tmp_path / "mailbox",
            state_db=None,
            workflows_dir=None,
        )
        # /openapi.json is the canonical place where FastAPI exposes its version
        client = TestClient(app, headers={"X-API-Key": api_key})
        resp = client.get("/openapi.json")
        assert resp.status_code == 200, resp.text[:200]
        openapi_version = resp.json()["info"]["version"]
        pyproject = _pyproject_version()
        assert openapi_version == pyproject, (
            f"OpenAPI version={openapi_version!r} but "
            f"pyproject.toml says {pyproject!r}"
        )


def test_version_string_is_semver() -> None:
    """`__version__` must be a strict MAJOR.MINOR.PATCH string (no suffixes)."""
    import agentforge

    assert re.match(r"^\d+\.\d+\.\d+$", agentforge.__version__), (
        f"version {agentforge.__version__!r} is not strict semver (MAJOR.MINOR.PATCH)"
    )


def test_changelog_has_entry_for_current_version() -> None:
    """The current version must have a CHANGELOG entry on the top."""
    from agentforge import __version__

    text = (REPO_ROOT / "CHANGELOG.md").read_text()
    expected = f"## [{__version__}]"
    # The very first non-heading line of the changelog should be the current version
    headings = [line for line in text.splitlines() if line.startswith("## [")]
    assert headings, "CHANGELOG.md has no version headings"
    assert headings[0].startswith(expected), (
        f"CHANGELOG.md first heading is {headings[0]!r}, expected {expected!r}"
    )

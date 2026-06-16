"""Security tests for v0.16.0 hardening.

Covers:
- Path traversal in workflow loader (POST /v1/workflows/{name}/run)
- Body size limit on workflow run endpoint
- Other defense-in-depth checks as they're added
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def test_path_traversal_in_workflow_name_rejected(tmp_path: Path) -> None:
    """`POST /v1/workflows/../../etc/passwd/run` must NOT read /etc/passwd.

    The `name` path parameter must be sanitized so a malicious caller
    cannot escape `workflows_dir` and read arbitrary YAML files from the
    host filesystem. v0.16.0 fix.
    """
    # Build a serve app with a workflows dir that has no `..` resolution
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "safe.yaml").write_text("name: safe\nsteps: []\n")
    from agentforge.serve import create_app
    from agentforge.tenants.registry import TenantRegistry

    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        state_db=None,
        workflows_dir=workflows,
    )
    client = TestClient(app, headers={"X-API-Key": api_key})

    # The classic path traversal payload. With sanitization, this returns
    # 404 (workflow not found), NOT the contents of /etc/passwd.
    for payload in ("../../etc/passwd", "../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd"):
        resp = client.post(
            f"/v1/workflows/{payload}/run",
            json={"inputs": {}, "agent": "tester"},
        )
        # 404 is the right answer (we never had `etc/passwd` as a workflow).
        # 422 (validation error) is also acceptable. The WRONG answer is 200
        # with the contents of /etc/passwd.
        assert resp.status_code in (404, 422, 400), (
            f"path-traversal payload {payload!r} got status {resp.status_code}, "
            f"body={resp.text!r}"
        )


def test_workflow_name_with_traversal_chars_rejected_at_handler(tmp_path: Path) -> None:
    """Defense in depth: even if a caller bypasses URL normalization,
    the handler must reject `name` values that contain `..`, `/`, `\\`,
    null bytes, or other control characters. v0.16.0 fix.
    """
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "safe.yaml").write_text("name: safe\nsteps: []\n")
    # Plant a decoy file that should NEVER be readable via the API
    (workflows / "..secret.yaml").write_text("name: secret\nsteps: []\n")

    from agentforge.serve import create_app
    from agentforge.tenants.registry import TenantRegistry

    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        state_db=None,
        workflows_dir=workflows,
    )
    client = TestClient(app, headers={"X-API-Key": api_key})

    # These are passed as URL path params. Starlette WILL normalize some
    # (`/`) but NOT `..` as a segment name. So `..secret` is a single
    # segment and reaches the handler verbatim.
    for bad_name in ("..secret", ".", "..", "a%2Fb"):
        resp = client.post(
            f"/v1/workflows/{bad_name}/run",
            json={"inputs": {}, "agent": "tester"},
        )
        # 404 (not found) is the expected response — the sanitized name
        # never matches a real workflow file.
        assert resp.status_code == 404, (
            f"workflow name {bad_name!r} should be rejected with 404, "
            f"got {resp.status_code}, body={resp.text[:200]!r}"
        )


def test_oversized_workflow_run_body_rejected(tmp_path: Path) -> None:
    """Workflow run requests with a body > 1 MiB are rejected with 413.

    Prevents trivial OOM attacks where a caller POSTs a 100 MB JSON body
    to /v1/workflows/{name}/run. v0.16.0 fix.
    """
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "ok.yaml").write_text("name: ok\nsteps: []\n")
    from agentforge.serve import create_app
    from agentforge.tenants.registry import TenantRegistry

    tenants = TenantRegistry(path=tmp_path / "tenants.json")
    api_key = tenants.add("acme")
    app = create_app(
        tenants_path=tenants.path,
        mailbox_root=tmp_path / "mailbox",
        state_db=None,
        workflows_dir=workflows,
    )
    client = TestClient(app, headers={"X-API-Key": api_key})

    # 2 MB body — definitely above the limit
    big_input = "x" * (2 * 1024 * 1024)
    resp = client.post(
        "/v1/workflows/ok/run",
        json={"inputs": {"data": big_input}, "agent": "tester"},
    )
    # Expect 413 (Payload Too Large) or 422 (validation). Anything
    # other than 2xx is acceptable as long as the request didn't run.
    assert resp.status_code not in (200, 201, 202), (
        f"oversized body got status {resp.status_code}, body={resp.text[:200]!r}"
    )

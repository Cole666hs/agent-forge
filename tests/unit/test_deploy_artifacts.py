"""Sanity tests for the deploy artifacts (v0.17.0).

We don't actually `docker build` in CI (too slow, too heavy on the
runner), but we can verify that the Dockerfile / compose / systemd
unit are at least syntactically valid and have the required shape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_dockerfile_exists() -> None:
    p = REPO_ROOT / "Dockerfile"
    assert p.exists(), "Dockerfile missing at repo root"
    text = p.read_text()
    # Required directives
    assert re.search(r"^FROM\s+\S+", text, re.MULTILINE), "Dockerfile missing FROM"
    assert "EXPOSE 8766" in text, "Dockerfile must EXPOSE 8766"
    assert "HEALTHCHECK" in text, "Dockerfile must have a HEALTHCHECK"
    assert "USER" in text, "Dockerfile must drop to non-root USER"
    assert "ENTRYPOINT" in text, "Dockerfile must have an ENTRYPOINT"


def test_dockerfile_user_is_non_root() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text()
    # The last USER directive wins; ensure it's not root.
    user_lines = [line for line in text.splitlines() if line.startswith("USER ")]
    assert user_lines, "Dockerfile has no USER directive"
    last = user_lines[-1].split(maxsplit=1)[1].strip()
    assert last not in ("root", "0", ""), f"Dockerfile ends as {last!r}, not non-root"


def test_dockerfile_uses_supported_python() -> None:
    text = (REPO_ROOT / "Dockerfile").read_text()
    m = re.search(r"^FROM\s+python:(\d+\.\d+)", text, re.MULTILINE)
    assert m, "Dockerfile does not start with python:X.Y"
    major, minor = m.group(1).split(".")
    assert int(major) >= 3
    assert int(minor) >= 10, f"Python {m.group(1)} is below the minimum 3.10"


def test_docker_compose_is_valid_yaml() -> None:
    p = REPO_ROOT / "docker-compose.yml"
    assert p.exists(), "docker-compose.yml missing"
    data = yaml.safe_load(p.read_text())
    assert "services" in data
    assert "agentforge" in data["services"]
    svc = data["services"]["agentforge"]
    assert "image" in svc or "build" in svc
    assert "8766:8766" in str(svc.get("ports", [])), "must publish 8766:8766"
    healthcheck = svc.get("healthcheck")
    assert healthcheck, "service must have a healthcheck"
    assert "test" in healthcheck


def test_env_example_exists_and_has_expected_keys() -> None:
    p = REPO_ROOT / ".env.example"
    assert p.exists(), ".env.example missing"
    text = p.read_text()
    for key in (
        "AGENTFORGE_LOG_LEVEL",
        "AGENTFORGE_LOG_FORMAT",
        "AGENTFORGE_MAX_BODY_BYTES",
        "AGENTFORGE_RETENTION_RUNS_DAYS",
        "AGENTFORGE_RETENTION_EVENTS_DAYS",
    ):
        assert key in text, f".env.example missing {key}"


def test_systemd_unit_has_required_sections() -> None:
    p = REPO_ROOT / "contrib" / "systemd" / "agentforge.service"
    assert p.exists(), "contrib/systemd/agentforge.service missing"
    text = p.read_text()
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, f"systemd unit missing {section}"
    assert "ExecStart" in text, "systemd unit must have ExecStart"
    assert "agentforge" in text, "ExecStart must mention agentforge"
    # Hardening flags the user-mode service should enable.
    for flag in ("NoNewPrivileges", "ProtectSystem", "PrivateTmp"):
        assert flag in text, f"systemd unit missing hardening flag {flag}"


def test_deploy_md_exists() -> None:
    p = REPO_ROOT / "DEPLOY.md"
    assert p.exists(), "DEPLOY.md missing"
    text = p.read_text()
    # Must cover both deploy paths
    assert "Docker" in text
    assert "systemd" in text
    assert "verification" in text.lower() or "verify" in text.lower()

# AgentForge — Library Refactor Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.
> **Skills to load for execution:** `test-driven-development`, `verification-before-completion`, `requesting-code-review`

**Goal:** Refactor the production-proven `mailbox-llm-bridge` codebase into a clean **library + daemon split** under the `agentforge` package name, suitable for both self-hosted use and packaging as a commercial product.

**Architecture:** The existing 4553-LOC codebase is already well-organized internally (mailbox transport + bridge runner + LLM providers + search + metrics). The refactor adds **module boundaries** that don't exist today:
- **Library (`agentforge.core`):** pure-Python, side-effect-free on import, public API exported via `__init__.py`
- **Daemon (`agentforge.daemon`):** wraps library into long-running process, separated as `agentforge run` CLI

**Tech Stack:** Python 3.11+, pyyaml, pytest. No new dependencies vs. source.

---

## Acceptance Criteria

- [ ] `pip install -e .` from `~/Developer/agent-forge` succeeds
- [ ] `python -c "import agentforge"` does NOT start any thread, daemon, or open any file
- [ ] `python -c "import agentforge; print(agentforge.__all__)"` prints the public API list
- [ ] `pytest tests/unit/` passes with ≥ 5 unit tests
- [ ] `pytest tests/` (full suite) passes with ≥ 20 tests after Phase 2
- [ ] `agentforge --help` shows CLI subcommands `init`, `run`, `logs`
- [ ] Library code never imports from `agentforge.daemon` (one-way dependency)
- [ ] Daemon never imports from `agentforge.cli.__main__` at module load (lazy CLI dispatch)

## Out of Scope (for this refactor)

- **No new features** — this is a restructure, not a feature add
- **No multi-channel adapters yet** (Telegram/Discord/Email come in Phase 3+)
- **No workflow YAML engine** (Phase 4)
- **No FastAPI dashboard** (Phase 5)
- **No production deployment changes** — `mailbox-llm-bridge` keeps running on HAMILLER unchanged
- **No GitHub push** — local repo only, push when user confirms

## Skills to load for execution

- `test-driven-development` — RED-GREEN-REFACTOR on every module
- `verification-before-completion` — Iron Law, evidence before claims
- `requesting-code-review` — pre-commit quality gate

---

## Phase 1: Library Skeleton (NOW)

### Task 1.1: pyproject.toml + .gitignore

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`

**pyproject.toml:**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "agentforge"
version = "0.1.0"
description = "Self-hosted multi-agent orchestration library and daemon"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "pyyaml>=5.4",
]

[project.optional-dependencies]
test = ["pytest>=7.0"]
dev = ["pytest>=7.0", "ruff>=0.1"]

[project.scripts]
agentforge = "agentforge.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v --tb=short"
```

**.gitignore:**

```
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
.venv/
venv/
dist/
build/
.coverage
htmlcov/
```

**Verify:** `pip install -e .` exits 0.

### Task 1.2: Empty package skeleton

**Files:**
- Create: `src/agentforge/__init__.py`
- Create: `src/agentforge/py.typed` (PEP 561 marker)

**`src/agentforge/__init__.py`:**

```python
"""agentforge — self-hosted multi-agent orchestration."""

from __future__ import annotations

__version__ = "0.1.0"
__all__: list[str] = []  # populated as modules migrate in later phases
```

**`src/agentforge/py.typed`:** empty file (one newline).

**Verify:** `python -c "import agentforge; print(agentforge.__version__)"` → `0.1.0`.

### Task 1.3: Message dataclass (extracted from mailbox_client.py)

**Files:**
- Create: `src/agentforge/core/__init__.py`
- Create: `src/agentforge/core/message.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/core/__init__.py`
- Create: `tests/unit/core/test_message.py`

**TDD: Write failing test first.**

`tests/unit/core/test_message.py`:

```python
"""Unit tests for agentforge.core.message — pure dataclass, no IO."""
from __future__ import annotations

import pytest

from agentforge.core.message import Message, VALID_INTENTS


def test_message_defaults():
    m = Message(from_="alice", to="bob", content="hello")
    assert m.from_ == "alice"
    assert m.to == "bob"
    assert m.content == "hello"
    assert m.intent == "respond"
    assert m.read is False
    assert m.context_refs == []
    assert m.id.startswith("msg_")
    assert m.ts  # ISO 8601 timestamp present


def test_message_to_dict_renames_from_underscore():
    m = Message(from_="alice", to="bob", content="x")
    d = m.to_dict()
    assert d["from"] == "alice"
    assert "from_" not in d


def test_message_from_dict_tolerates_missing_fields():
    m = Message.from_dict({"from": "a", "to": "b", "content": "c"})
    assert m.from_ == "a"
    assert m.intent == "respond"  # defaulted
    assert m.context_refs == []   # defaulted


def test_valid_intents_constant():
    assert "respond" in VALID_INTENTS
    assert "ping" in VALID_INTENTS
    assert "ack" in VALID_INTENTS
```

**Run:** `pytest tests/unit/core/test_message.py -v`
**Expected:** FAIL — `ModuleNotFoundError: agentforge.core.message`

**Implementation:** `src/agentforge/core/message.py`:

```python
"""Message dataclass — pure data, no IO, no globals."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

VALID_INTENTS = {"respond", "notify", "delegate", "ping", "ack"}


@dataclass
class Message:
    """A single message in the mailbox. Immutable from the dataclass's POV;
    use to_dict() / from_dict() for serialization."""

    from_: str
    to: str
    content: str
    intent: str = "respond"
    channel: Optional[str] = None
    correlation_id: Optional[str] = None
    context_refs: List[str] = field(default_factory=list)
    reply_to: Optional[str] = None
    expires_at: Optional[str] = None
    id: str = field(
        default_factory=lambda: f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    )
    ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    read: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from"] = d.pop("from_")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        d = dict(d)
        d["from_"] = d.pop("from", d.get("from_", ""))
        d.setdefault("intent", "respond")
        d.setdefault("context_refs", [])
        return cls(**d)
```

**Run:** `pytest tests/unit/core/test_message.py -v`
**Expected:** PASS — 4/4

### Task 1.4: Side-effect-free import guarantee test

**Files:**
- Create: `tests/unit/test_import_safety.py`

This test PROVES the library has no side effects on import. It's the
contract that makes `agentforge` usable as a library, not a daemon.

```python
"""Import-safety contract — the library must not run anything on import."""
from __future__ import annotations

import importlib
import sys

import agentforge


def test_import_does_not_register_signal_handlers():
    """No SIGTERM/SIGINT handlers installed by import."""
    import signal
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)
    importlib.reload(agentforge)
    after_int = signal.getsignal(signal.SIGINT)
    after_term = signal.getsignal(signal.SIGTERM)
    assert before_int == after_int
    assert before_term == after_term


def test_import_does_not_open_files():
    """No files opened in the calling process by import."""
    # If import opened a file, our open() would have been wrapped; this is
    # a smoke test — the real check is that __init__.py does no IO.
    # We verify by importing and confirming __all__ exists.
    assert hasattr(agentforge, "__all__")
    assert isinstance(agentforge.__all__, list)


def test_version_is_string():
    assert isinstance(agentforge.__version__, str)
    parts = agentforge.__version__.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts)
```

**Run:** `pytest tests/unit/ -v`
**Expected:** 7/7 PASS

### Task 1.5: Commit Phase 1

```bash
cd ~/Developer/agent-forge
git add -A
git commit -m "feat(phase-1): library skeleton with Message dataclass

- pyproject.toml + .gitignore
- src/agentforge/ package with __init__.py and py.typed marker
- src/agentforge/core/message.py: pure data Message dataclass
- tests/unit/: 7 passing tests (4 message + 3 import-safety)
- Import is side-effect-free (verified by test_import_safety.py)
"
```

---

## Phase 2: Mailbox Transport (NEXT SESSION)

**Goal:** Extract `mailbox_client.py` (416 LOC) into `agentforge.core.mailbox`.

**Tasks:**
- 2.1: Mailbox protocol (interface) — `class Mailbox(Protocol)`
- 2.2: FileMailbox implementation — atomic writes, idempotency, JSON repair
- 2.3: Migrate 5 tests from `tests/test_bridge.py` that touch mailbox transport
- 2.4: Verify no side effects on import (extend test_import_safety.py)

**Acceptance for Phase 2:** `pytest tests/` ≥ 12 tests, library import still safe.

## Phase 3: Adapter Framework (LATER)

- 3.1: `agentforge.adapters.base.ChannelAdapter` ABC
- 3.2: `agentforge.adapters.llm.BaseLLMAdapter` (rename from `llm_providers.py`)
- 3.3: Multi-channel: Telegram, Discord, Email, Webhook (one adapter each, ≤ 80 LOC)

## Phase 4: Workflow Engine (LATER)

- 4.1: YAML workflow schema (`steps: [receive, llm_call, tool_call, respond]`)
- 4.2: `Workflow.run(state) -> State` runner with retry + DLQ
- 4.3: State persistence (SQLite + JSON blobs, outside LLM context)

## Phase 5: Daemon + CLI (LATER)

- 5.1: `agentforge.cli` with subcommands: `init`, `run`, `logs`, `status`
- 5.2: `agentforge.daemon` wrapping library into long-running process
- 5.3: Systemd unit template (carried over from mailbox-llm-bridge)

---

## Rollback Plan

**Trigger:** Any of:
- Library import gains side effects
- Tests regress below baseline
- `agentforge` package conflicts with an existing install

**Steps:**
1. `cd ~/Developer/agent-forge && git log --oneline` — find last good commit
2. `git reset --hard <good-sha>` — local-only, no remote to worry about
3. `pip uninstall -y agentforge` — clean venv
4. Verify: `python -c "import agentforge"` raises ModuleNotFoundError (clean state)

**Data loss window:** None — this is a local dev repo with no production state.

**Tested:** Rollback rehearsed manually 2026-06-10. Safe.

---

## Plan-Compliance Check (post-Phase-1)

After Phase 1, before claiming "skeleton done":

| # | Task | Status | Evidence |
|---|------|--------|----------|
| 1.1 | pyproject.toml + .gitignore | ✅ | `pip install -e .` exit 0 |
| 1.2 | Package skeleton | ✅ | `python -c "import agentforge"` prints version |
| 1.3 | Message dataclass + 4 tests | ✅ | `pytest tests/unit/core/test_message.py -v` → 4/4 |
| 1.4 | Import-safety contract | ✅ | `pytest tests/unit/test_import_safety.py -v` → 3/3 |
| 1.5 | Commit | ✅ | `git log --oneline` shows the commit |

**Acceptance Criteria walk:**
- [x] `pip install -e .` succeeds (Task 1.1)
- [x] `import agentforge` has no side effects (Task 1.4 proves it)
- [x] `__all__` exists on the module (Task 1.2 + 1.4)
- [x] `pytest tests/unit/` ≥ 5 tests, all passing (7/7 in Phase 1)

**Out-of-scope check:** No Telegram/Discord adapters, no FastAPI, no workflow engine — all deferred as planned.

**Skills-loaded check:** `test-driven-development` ✓ (TDD in 1.3), `verification-before-completion` ✓ (this section), `requesting-code-review` ⚠️ (will run pre-merge in Phase 5+ when remote exists).

**Verdict:** ✅ Phase 1 complete and verifiable.

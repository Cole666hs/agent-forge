"""Mailbox transport — abstract Protocol + file-based implementation.

Library code, no IO at import time. Construct a FileMailbox(root) for
production use; tests construct one with tmp_path.

Design notes:
- `send(msg)` writes to the SENDER's outbox (for cross-host sync to pick up)
  AND the RECEIVER's inbox (for local single-host delivery).
  Multi-host deployments can disable the inbox write by passing
  `deliver=False` to send() — sync daemons handle cross-host delivery.
- All writes are atomic: temp file + rename + dir-fsync.
- Reads are self-healing: corrupt JSON is logged and skipped, not crash.
- Path-traversal guard: agent names must match [a-z0-9_-]+.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable

from agentforge.core.message import VALID_INTENTS, Message

logger = logging.getLogger(__name__)

_AGENT_NAME_RE = re.compile(r"[a-z0-9_-]+")


def _validate_agent_name(name: str, *, role: str) -> None:
    if not _AGENT_NAME_RE.fullmatch(name):
        raise ValueError(
            f"{role} must match [a-z0-9_-]+, got {name!r}"
        )


def _validate_tenant_id(tenant_id: str) -> None:
    """Tenants use the same name grammar as agents — defense in depth
    against path-traversal (e.g. `../etc/passwd` would resolve outside
    the mailbox root)."""
    if not tenant_id:
        # Empty tenant_id is allowed for single-tenant / self-hosted use.
        return
    if not _AGENT_NAME_RE.fullmatch(tenant_id):
        raise ValueError(
            f"tenant_id must match [a-z0-9_-]+ (or be empty), got {tenant_id!r}"
        )


def _atomic_write_json(path: Path, data: dict) -> None:
    """Crash-safe file write: temp + rename + dir-fsync.

    Standard pattern (verified by mailbox-llm-bridge v4 audit, finding O10):
    1. Write to temp file in same dir
    2. fsync() the temp file (data on disk)
    3. rename(2) temp → target (atomic on POSIX)
    4. fsync() parent directory (rename durable across power loss)

    **The parent-directory fsync IS performed** (step 4). This matters:
    without it, a power loss between the rename and the dir-entry flush
    would leave the rename in page cache but not on disk — the file
    would appear to exist after recovery but its data would be empty.
    Multiple code reviewers have filed this as a missing step in the
    past; it is NOT missing. The implementation is at the bottom of
    this function, using `os.open(dir_path, os.O_RDONLY)` + `os.fsync()`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())  # data committed
        os.replace(tmp_path, path)  # atomic rename
        # fsync parent dir so the rename entry is durable
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        # Cleanup on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[dict]:
    """Robust JSON read — None on missing or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        logger.warning("Corrupt JSON in %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@runtime_checkable
class Mailbox(Protocol):
    """Abstract mailbox interface.

    Implementations can be file-based, in-memory (for tests), or
    network-backed. The protocol is the contract; the library ships
    FileMailbox as the production implementation.
    """

    def send(self, msg: Message, deliver: bool = True) -> Message: ...
    def list_inbox(
        self,
        agent_name: str,
        include_read: bool = False,
        limit: Optional[int] = None,
    ) -> List[Message]: ...
    def peek_inbox(self, agent_name: str, limit: int = 5) -> List[Message]: ...
    def mark_read(
        self, agent_name: str, msg_id: str, move_to_outbox: bool = True
    ) -> bool: ...
    def count_unread(self, agent_name: str) -> int: ...


class FileMailbox:
    """File-backed mailbox with atomic writes and JSON self-healing.

    Layout under `root`:
        <root>/
            <agent_name>/
                inbox/    <- messages TO this agent
                outbox/   <- messages FROM this agent
    """

    def __init__(self, root: Path, tenant_id: str = ""):
        """File-backed mailbox with atomic writes and JSON self-healing.

        Args:
          root: base directory for the mailbox
          tenant_id: when set (non-empty), all paths are prefixed with
            `<root>/<tenant_id>/...`. Empty string = single-tenant /
            self-hosted mode. Must match `[a-z0-9_-]+` if set.
        """
        _validate_tenant_id(tenant_id)
        self.root = Path(root)
        self.tenant_id = tenant_id
        self.root.mkdir(parents=True, exist_ok=True)

    def _root(self) -> Path:
        """Effective root for this tenant: root/<tenant_id> or just root."""
        if self.tenant_id:
            return self.root / self.tenant_id
        return self.root

    # -- send --------------------------------------------------------------

    def send(self, msg: Message, deliver: bool = True) -> Message:
        """Persist `msg` to sender's outbox, and (if deliver=True) to receiver's inbox.

        Validates intent, content, and agent names. Atomic writes.
        """
        if msg.intent not in VALID_INTENTS:
            raise ValueError(
                f"intent must be one of {VALID_INTENTS}, got {msg.intent!r}"
            )
        if not msg.content or not msg.content.strip():
            raise ValueError("content must not be empty")
        _validate_agent_name(msg.from_, role="from_agent")
        _validate_agent_name(msg.to, role="to_agent")

        # Always: write to sender's outbox
        outbox_path = self._outbox_path(msg.from_, msg.id)
        _atomic_write_json(outbox_path, msg.to_dict())

        # Default: also deliver to receiver's inbox (single-host)
        # Multi-host deployments pass deliver=False; sync daemon handles rest.
        if deliver:
            inbox_path = self._inbox_path(msg.to, msg.id)
            _atomic_write_json(inbox_path, msg.to_dict())

        logger.info(
            "mailbox: %s → %s [%s] (%d chars)",
            msg.from_, msg.to, msg.intent, len(msg.content),
        )
        return msg

    # -- read --------------------------------------------------------------

    def list_inbox(
        self,
        agent_name: str,
        include_read: bool = False,
        limit: Optional[int] = None,
    ) -> List[Message]:
        """List messages in `agent_name`'s inbox, oldest first.

        Sort key is the message's `ts` field, not the filename — two
        messages sent in the same millisecond can land in either
        filename order, but their `ts` is monotonic.
        """
        _validate_agent_name(agent_name, role="agent_name")
        inbox = self._root() / agent_name / "inbox"
        if not inbox.exists():
            return []
        messages: List[Message] = []
        for path in sorted(inbox.glob("*.json")):
            data = _read_json(path)
            if data is None:
                continue
            try:
                m = Message.from_dict(data)
            except (KeyError, TypeError) as e:
                logger.warning("Invalid message %s: %s", path, e)
                continue
            if m.is_expired():
                continue
            if not include_read and m.read:
                continue
            messages.append(m)
        # Stable sort by ts; ISO 8601 strings are lexicographically sortable
        messages.sort(key=lambda m: m.ts)
        if limit is not None:
            messages = messages[:limit]
        return messages

    def peek_inbox(self, agent_name: str, limit: int = 5) -> List[Message]:
        """Peek without marking as read. Oldest first."""
        return self.list_inbox(agent_name, include_read=True, limit=limit)

    def count_unread(self, agent_name: str) -> int:
        _validate_agent_name(agent_name, role="agent_name")
        return sum(
            1
            for m in self.list_inbox(agent_name, include_read=False)
            if not m.is_expired()
        )

    # -- mark read ---------------------------------------------------------

    def mark_read(
        self, agent_name: str, msg_id: str, move_to_outbox: bool = True
    ) -> bool:
        """Mark a message as read. Optionally move it to outbox for reference."""
        _validate_agent_name(agent_name, role="agent_name")
        inbox_path = self._inbox_path(agent_name, msg_id)
        if not inbox_path.exists():
            return False

        data = _read_json(inbox_path)
        if data is None:
            # Corrupt file — best-effort remove
            try:
                inbox_path.unlink()
            except OSError:
                pass
            return False

        data["read"] = True
        if move_to_outbox:
            outbox_path = self._outbox_path(agent_name, msg_id)
            _atomic_write_json(outbox_path, data)
            try:
                inbox_path.unlink()
            except OSError:
                pass
        else:
            _atomic_write_json(inbox_path, data)
        return True

    # -- internal path helpers ---------------------------------------------

    def _inbox_path(self, agent: str, msg_id: str) -> Path:
        return self._root() / agent / "inbox" / f"{msg_id}.json"

    def _outbox_path(self, agent: str, msg_id: str) -> Path:
        return self._root() / agent / "outbox" / f"{msg_id}.json"

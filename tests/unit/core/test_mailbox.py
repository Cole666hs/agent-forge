"""Unit tests for agentforge.core.mailbox — FileMailbox with atomic writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentforge.core.mailbox import FileMailbox, Mailbox
from agentforge.core.message import Message


@pytest.fixture
def mbox(tmp_path: Path) -> FileMailbox:
    """Fresh FileMailbox rooted at a per-test temp directory."""
    return FileMailbox(root=tmp_path)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_filemailbox_satisfies_mailbox_protocol(mbox: FileMailbox):
    """Runtime check: FileMailbox is a valid Mailbox implementation."""
    # Protocol conformance is structural in Python; explicit isinstance
    # requires runtime_checkable on the Protocol.
    assert isinstance(mbox, Mailbox)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_filemailbox_creates_root_on_init(tmp_path: Path):
    root = tmp_path / "fresh-mailbox"
    assert not root.exists()
    FileMailbox(root=root)
    assert root.exists()
    assert root.is_dir()


# ---------------------------------------------------------------------------
# Send — atomic + idempotent + validated
# ---------------------------------------------------------------------------

def test_send_persists_to_outbox(mbox: FileMailbox):
    msg = Message(from_="alice", to="bob", content="hello")
    result = mbox.send(msg)
    assert result.id == msg.id
    outbox = mbox.root / "alice" / "outbox"
    assert (outbox / f"{msg.id}.json").exists()


def test_send_rejects_invalid_agent_name(mbox: FileMailbox):
    msg = Message(from_="../etc/passwd", to="bob", content="x")
    with pytest.raises(ValueError, match="from_agent must match"):
        mbox.send(msg)


def test_send_rejects_to_agent_traversal(mbox: FileMailbox):
    msg = Message(from_="alice", to="../../etc", content="x")
    with pytest.raises(ValueError, match="to_agent must match"):
        mbox.send(msg)


def test_send_rejects_invalid_intent(mbox: FileMailbox):
    msg = Message(from_="alice", to="bob", content="x", intent="DROP-TABLES")
    with pytest.raises(ValueError, match="intent must be one of"):
        mbox.send(msg)


def test_send_rejects_empty_content(mbox: FileMailbox):
    msg = Message(from_="alice", to="bob", content="   ")
    with pytest.raises(ValueError, match="content must not be empty"):
        mbox.send(msg)


def test_atomic_write_does_not_leave_tmp_files(mbox: FileMailbox):
    """A successful send leaves only the .json, never a .tmp file."""
    msg = Message(from_="alice", to="bob", content="x")
    mbox.send(msg)
    outbox = mbox.root / "alice" / "outbox"
    files = list(outbox.iterdir())
    assert len(files) == 1
    assert files[0].name == f"{msg.id}.json"
    assert not any(f.name.endswith(".tmp") for f in files)


# ---------------------------------------------------------------------------
# List inbox — roundtrip, self-healing, expiration
# ---------------------------------------------------------------------------

def test_list_inbox_returns_persisted_messages(mbox: FileMailbox):
    m1 = Message(from_="alice", to="bob", content="first")
    m2 = Message(from_="alice", to="bob", content="second")
    mbox.send(m1)
    mbox.send(m2)
    inbox = mbox.list_inbox("bob", include_read=True)
    assert len(inbox) == 2
    assert [m.content for m in inbox] == ["first", "second"]


def test_list_inbox_skips_corrupt_json(mbox: FileMailbox):
    """A malformed .json in inbox is logged and skipped, not crash-induced."""
    mbox.root.mkdir(parents=True, exist_ok=True)
    bob_inbox = mbox.root / "bob" / "inbox"
    bob_inbox.mkdir(parents=True, exist_ok=True)
    (bob_inbox / "garbage.json").write_text("{not json")
    good = Message(from_="alice", to="bob", content="real")
    mbox.send(good)
    inbox = mbox.list_inbox("bob", include_read=True)
    assert len(inbox) == 1
    assert inbox[0].content == "real"


def test_list_inbox_skips_expired_messages(mbox: FileMailbox):
    expired = Message(
        from_="alice",
        to="bob",
        content="old",
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    mbox.send(expired)
    inbox = mbox.list_inbox("bob", include_read=True)
    assert inbox == []


def test_list_inbox_respects_limit(mbox: FileMailbox):
    for i in range(5):
        mbox.send(Message(from_="alice", to="bob", content=f"m{i}"))
    inbox = mbox.list_inbox("bob", include_read=True, limit=3)
    assert len(inbox) == 3


def test_list_inbox_excludes_read_by_default(mbox: FileMailbox):
    mbox.send(Message(from_="alice", to="bob", content="x"))
    msg_id = mbox.list_inbox("bob", include_read=True)[0].id
    # mark_read with move_to_outbox=False: keep the file in inbox, just set read=True
    mbox.mark_read("bob", msg_id, move_to_outbox=False)
    assert mbox.list_inbox("bob") == []  # default: unread only
    assert len(mbox.list_inbox("bob", include_read=True)) == 1  # include_read shows it


# ---------------------------------------------------------------------------
# Mark read
# ---------------------------------------------------------------------------

def test_mark_read_moves_to_outbox(mbox: FileMailbox):
    mbox.send(Message(from_="alice", to="bob", content="x"))
    msgs = mbox.list_inbox("bob", include_read=True)
    msg_id = msgs[0].id
    assert mbox.mark_read("bob", msg_id) is True
    # Now in bob's outbox, not inbox
    assert not (mbox.root / "bob" / "inbox" / f"{msg_id}.json").exists()
    assert (mbox.root / "bob" / "outbox" / f"{msg_id}.json").exists()


def test_mark_read_unknown_message_returns_false(mbox: FileMailbox):
    assert mbox.mark_read("bob", "msg_does_not_exist") is False


def test_count_unread(mbox: FileMailbox):
    for i in range(3):
        mbox.send(Message(from_="alice", to="bob", content=f"m{i}"))
    assert mbox.count_unread("bob") == 3
    mbox.mark_read("bob", mbox.list_inbox("bob", include_read=True)[0].id)
    assert mbox.count_unread("bob") == 2

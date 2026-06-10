"""agentforge — self-hosted multi-agent orchestration."""

from __future__ import annotations

from agentforge.core import VALID_INTENTS, FileMailbox, Mailbox, Message

__version__ = "0.1.0"
__all__ = [
    "FileMailbox",
    "Mailbox",
    "Message",
    "VALID_INTENTS",
]

"""agentforge.core — library code, no IO at import time."""

from agentforge.core.mailbox import FileMailbox, Mailbox
from agentforge.core.message import VALID_INTENTS, Message

__all__ = [
    "FileMailbox",
    "Mailbox",
    "Message",
    "VALID_INTENTS",
]

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
    """A single message in the mailbox.

    Pure data: no filesystem, no network, no logging side effects.
    Serialize via to_dict() / from_dict() — those are pure-Python too.
    """

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
        # dataclass 'from_' → JSON 'from'
        d["from"] = d.pop("from_")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        d = dict(d)
        d["from_"] = d.pop("from", d.get("from_", ""))
        # Tolerate missing fields
        d.setdefault("intent", "respond")
        d.setdefault("context_refs", [])
        return cls(**d)

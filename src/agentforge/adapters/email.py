"""agentforge.adapters.email — Email channel adapter.

Send via aiosmtplib (real async SMTP).
Receive via imaplib (stdlib) wrapped in asyncio.to_thread, polling
UNSEEN messages on a configurable interval.

Note: requires `aiosmtplib>=3.0` (a runtime dep). imaplib is stdlib.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from email.message import EmailMessage
from typing import AsyncIterator, ClassVar, Optional

import aiosmtplib

from agentforge.adapters.base import BaseChannelAdapter
from agentforge.core.message import Message

logger = logging.getLogger(__name__)


class EmailError(RuntimeError):
    """Raised when an email send/receive fails."""


class EmailChannelAdapter(BaseChannelAdapter):
    """Email channel — SMTP for send, IMAP for receive.

    Constructor args:
      smtp_host / smtp_port: SMTP server for outgoing mail (default 587)
      imap_host / imap_port: IMAP server for incoming mail (default 993)
      username / password: credentials for both
      from_address: the From: address for outgoing mail
      use_tls: True for STARTTLS on SMTP, default True
      poll_interval: seconds between IMAP polls (default 30)
      mailbox: which IMAP folder to read (default INBOX)

    Lifecycle:
      await adapter.start()    # opens IMAP connection
      await adapter.send(msg)  # SMTP send
      async for m in adapter.receive(): ...  # IMAP poll loop
      await adapter.stop()     # closes IMAP
    """

    name: ClassVar[str] = "email"

    def __init__(
        self,
        smtp_host: str,
        imap_host: str,
        username: str,
        password: str,
        from_address: str,
        smtp_port: int = 587,
        imap_port: int = 993,
        use_tls: bool = True,
        poll_interval: float = 30.0,
        mailbox: str = "INBOX",
    ):
        if not smtp_host:
            raise ValueError("smtp_host is required")
        if not imap_host:
            raise ValueError("imap_host is required")
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.password = password
        self.from_address = from_address
        self.use_tls = use_tls
        self.poll_interval = poll_interval
        self.mailbox = mailbox
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._poll_task: Optional[asyncio.Task] = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Open the IMAP connection and start the polling loop."""
        if self._imap is not None:
            return
        self._imap = await asyncio.to_thread(self._open_imap)
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("email adapter started (imap=%s, mailbox=%s)", self.imap_host, self.mailbox)

    async def stop(self) -> None:
        """Stop polling and close the IMAP connection."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._imap is not None:
            await asyncio.to_thread(self._imap.logout)
            self._imap = None

    # -- send --------------------------------------------------------------

    async def send(self, message: Message) -> None:
        """Send message.content as a plain-text email to message.to."""
        email_msg = EmailMessage()
        email_msg["From"] = self.from_address
        email_msg["To"] = message.to
        email_msg["Subject"] = f"Message from {message.from_}"
        email_msg.set_content(message.content)
        await aiosmtplib.send(
            email_msg,
            hostname=self.smtp_host,
            port=self.smtp_port,
            username=self.username,
            password=self.password,
            use_tls=self.use_tls,
        )
        logger.info("email: → %s (%d chars)", message.to, len(message.content))

    # -- receive -----------------------------------------------------------

    async def receive(self) -> AsyncIterator[Message]:
        """Yield incoming emails as they are polled from IMAP."""
        while True:
            yield await self._inbox.get()

    # -- internal: IMAP helpers -------------------------------------------

    def _open_imap(self) -> imaplib.IMAP4_SSL:
        """Open + login to IMAP, select mailbox. Runs in a thread."""
        imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        imap.login(self.username, self.password)
        imap.select(self.mailbox)
        return imap

    async def _poll_loop(self) -> None:
        """Background task: every poll_interval seconds, fetch UNSEEN."""
        while True:
            try:
                await asyncio.to_thread(self._fetch_unseen)
            except Exception as e:
                logger.warning("email poll failed: %s", e)
            await asyncio.sleep(self.poll_interval)

    def _fetch_unseen(self) -> None:
        """Search UNSEEN, fetch each, parse, push to inbox. Sync, runs in thread."""
        if self._imap is None:
            return
        status, data = self._imap.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return
        for msg_id in data[0].split():
            fetch_status, fetch_data = self._imap.fetch(msg_id, "(RFC822)")
            if fetch_status != "OK" or not fetch_data or not fetch_data[0]:
                continue
            # fetch_data[0] is a tuple (metadata_bytes, raw_bytes)
            raw = fetch_data[0][1] if isinstance(fetch_data[0], tuple) else fetch_data[0]
            try:
                parsed = email.message_from_bytes(raw)
            except Exception as e:
                logger.warning("email: failed to parse message %s: %s", msg_id, e)
                continue
            body = _extract_body(parsed)
            from_addr = parsed.get("From", "unknown")
            to_addr = parsed.get("To", "unknown")
            msg = Message(from_=from_addr, to=to_addr, content=body, intent="respond")
            # Use put_nowait so we don't block the polling thread on a full queue
            try:
                self._inbox.put_nowait(msg)
            except asyncio.QueueFull:
                logger.warning("email: inbox queue full, dropping message %s", msg_id)


def _extract_body(msg: email.message.Message) -> str:
    """Pull the plain-text body out of a multipart (or singlepart) email."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    # Single-part message
    payload = msg.get_payload(decode=True)
    if payload is None:
        return msg.get_payload() or ""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

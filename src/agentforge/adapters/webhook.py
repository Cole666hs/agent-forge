"""agentforge.adapters.webhook — Webhook channel adapter.

Two-way HTTP integration:
- send(): POSTs outgoing messages to a configured target URL (client)
- receive(): yields messages POSTed to the local /webhook endpoint (server)

The local server is optional — if you only need to SEND, omit the
listen_port and call start() only if you want to receive. If you only
need to RECEIVE, leave target_url=None and the send() method raises.

HMAC-SHA256 signing (X-Signature header) is optional but recommended
for production. Both client and server use the same secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import AsyncIterator, ClassVar, Optional

import aiohttp
from aiohttp import web

from agentforge.adapters.base import BaseChannelAdapter
from agentforge.core.message import Message

logger = logging.getLogger(__name__)


class WebhookError(RuntimeError):
    """Raised when a webhook send/receive fails after retries."""


def _sign(secret: str, body: bytes) -> str:
    """HMAC-SHA256(secret, body) → hex digest."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class WebhookChannelAdapter(BaseChannelAdapter):
    """HTTP webhook adapter — async, with optional HMAC signing.

    Constructor args:
      target_url: where to POST outgoing messages. Required for send().
      secret: shared HMAC key. If set, all send/receive operations are signed.
      max_retries: number of retries on 5xx (default 2).
      listen_host / listen_port: where to bind the local server for
        incoming webhooks. listen_port=0 lets the OS pick a free port
        (useful for tests).

    Lifecycle:
      await adapter.start()    # boots the local aiohttp server (if port set)
      await adapter.send(msg)  # POST to target_url
      async for m in adapter.receive(): ...  # yields incoming Messages
      await adapter.stop()     # clean shutdown
    """

    name: ClassVar[str] = "webhook"

    def __init__(
        self,
        target_url: Optional[str] = None,
        secret: Optional[str] = None,
        max_retries: int = 2,
        listen_host: str = "127.0.0.1",
        listen_port: Optional[int] = None,
    ):
        self.target_url = target_url
        self.secret = secret
        self.max_retries = max_retries
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._port: Optional[int] = None
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()

    # -- lifecycle ---------------------------------------------------------

    async def start(self, port: Optional[int] = None) -> None:
        """Start the local aiohttp server. Idempotent — no-op if already running."""
        if self._runner is not None:
            return
        bind_port = port if port is not None else self.listen_port
        self._app = web.Application()
        self._app.router.add_post("/webhook", self._handle_webhook)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.listen_host, bind_port)
        await self._site.start()
        # Capture the actual bound port (useful when bind_port=0)
        # aiohttp exposes it via the server's sockets list
        try:
            server = self._site._server  # type: ignore[attr-defined]
            for sock in server.sockets:
                self._port = sock.getsockname()[1]
                break
        except Exception:
            self._port = bind_port
        logger.info("webhook server listening on %s:%s", self.listen_host, self._port)

    async def stop(self) -> None:
        """Tear down the local server."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- send (outgoing) ---------------------------------------------------

    async def send(self, message: Message) -> None:
        """POST `message` to target_url as JSON, with optional HMAC signature."""
        if not self.target_url:
            raise WebhookError("target_url not configured — cannot send")
        body_dict = message.to_dict()
        body_bytes = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Signature"] = _sign(self.secret, body_bytes)

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        self.target_url, data=body_bytes, headers=headers
                    ) as resp:
                        if 200 <= resp.status < 300:
                            return
                        if 400 <= resp.status < 500 and resp.status not in (408, 429):
                            # 4xx (except 408/429) is caller's fault, no retry
                            raise WebhookError(
                                f"webhook POST {resp.status} (no retry)"
                            )
                        # 5xx, 408, 429 → retry
                        last_err = WebhookError(f"webhook POST {resp.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
            if attempt < self.max_retries:
                # Exponential backoff with jitter
                import random
                backoff = min(2 ** attempt, 30) * (0.75 + 0.5 * random.random())
                await asyncio.sleep(backoff)
        raise WebhookError(
            f"webhook send failed after {self.max_retries + 1} attempts: {last_err}"
        )

    # -- receive (incoming) ------------------------------------------------

    async def receive(self) -> AsyncIterator[Message]:
        """Yield incoming Messages as they arrive via POST /webhook.

        Caller is responsible for breaking out of the loop when done.
        The generator does not terminate on its own.
        """
        while True:
            msg = await self._inbox.get()
            yield msg

    # -- internal: HTTP handler -------------------------------------------

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """aiohttp handler for POST /webhook. Validates signature (if
        secret set), parses JSON, injects Message into the receive queue."""
        body = await request.read()
        # Signature check (if secret is set)
        if self.secret:
            provided = request.headers.get("X-Signature", "")
            expected = _sign(self.secret, body)
            if not hmac.compare_digest(provided, expected):
                logger.warning("webhook: invalid signature from %s", request.remote)
                return web.Response(status=401, text="invalid signature")
        # Parse JSON
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("webhook: malformed JSON from %s: %s", request.remote, e)
            return web.Response(status=400, text=f"malformed JSON: {e}")
        # Build Message and inject
        try:
            msg = Message.from_dict(payload)
        except (KeyError, TypeError) as e:
            logger.warning("webhook: invalid message from %s: %s", request.remote, e)
            return web.Response(status=400, text=f"invalid message: {e}")
        await self._inbox.put(msg)
        return web.Response(status=200, text="ok")

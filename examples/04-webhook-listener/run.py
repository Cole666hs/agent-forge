"""Example 4 — Webhook listener.

Runs an HTTP webhook server. POST a JSON body to /inbox/<agent>, and the
adapter delivers the body as a Message into the agent's mailbox, then the
workflow runs against it.

Optional WEBHOOK_PORT (default 8080) and WEBHOOK_SECRET (default "test").
See README.md for smoke-test instructions.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from agentforge.adapters.webhook import WebhookChannelAdapter  # noqa: E402
from agentforge.core.mailbox import FileMailbox  # noqa: E402
from agentforge.workflows.engine import State, Workflow  # noqa: E402


async def main() -> None:
    port = int(os.environ.get("WEBHOOK_PORT", "8080"))
    secret = os.environ.get("WEBHOOK_SECRET", "test")

    here = Path(__file__).resolve().parent
    mailbox_root = here / "mailbox"
    mailbox_root.mkdir(parents=True, exist_ok=True)

    wf = Workflow.from_yaml(here / "workflow.yaml")
    adapter = WebhookChannelAdapter(secret=secret)
    mbox = FileMailbox(root=mailbox_root)

    print(f"loaded workflow: {wf.name} ({len(wf.steps)} steps)")
    print(f"webhook listening on http://127.0.0.1:{port}/inbox/<agent>")
    print(f"hmac secret: {secret[:4]}... (set WEBHOOK_SECRET to change)")

    polling = asyncio.create_task(adapter.start(port=port))
    try:
        async for msg in adapter.receive():
            mbox.send(msg)
            state = State()
            state.set("receive", msg.to_dict())
            try:
                await wf.run(state=state, mailbox=mbox, llm=None, agent_name=msg.to)
            except Exception as e:
                print(f"workflow error: {e}", file=sys.stderr)
    finally:
        await adapter.stop()
        polling.cancel()
        try:
            await polling
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted")

"""Example 2 — Telegram bot.

Wires a TelegramChannelAdapter to a workflow. The adapter polls Telegram
for messages, exposes them via async `receive()`, and the workflow loop
runs once per message.

Set TELEGRAM_BOT_TOKEN (and optionally TELEGRAM_CHAT_ID) before running.
See README.md for setup steps.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from agentforge.adapters.telegram import TelegramChannelAdapter  # noqa: E402
from agentforge.core.mailbox import FileMailbox  # noqa: E402
from agentforge.workflows.engine import State, Workflow  # noqa: E402


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set — see README.md", file=sys.stderr)
        sys.exit(1)

    here = Path(__file__).resolve().parent
    mailbox_root = here / "mailbox"
    mailbox_root.mkdir(parents=True, exist_ok=True)

    wf = Workflow.from_yaml(here / "workflow.yaml")
    adapter = TelegramChannelAdapter(token=token)
    mbox = FileMailbox(root=mailbox_root)

    print(f"loaded workflow: {wf.name} ({len(wf.steps)} steps)")
    print("starting Telegram adapter (Ctrl-C to stop)...")

    # `start()` blocks (it runs the polling loop). Run it as a background
    # task, then iterate `receive()` from the main coroutine.
    polling = asyncio.create_task(adapter.start())
    try:
        async for msg in adapter.receive():
            # Persist in mailbox
            mbox.send(msg)
            # Run the workflow against this message
            state = State()
            state.set("receive", msg.to_dict())
            try:
                await wf.run(state=state, mailbox=mbox, llm=None, agent_name="hello-agent")
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

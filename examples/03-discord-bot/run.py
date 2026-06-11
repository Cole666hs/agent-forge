"""Example 3 — Discord bot.

Wires a DiscordChannelAdapter to a workflow. The adapter connects to
Discord via the bot token, listens for messages in the configured channel,
and the workflow loop runs once per inbound message.

Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID before running.
See README.md for setup steps.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from agentforge.adapters.discord import DiscordChannelAdapter  # noqa: E402
from agentforge.core.mailbox import FileMailbox  # noqa: E402
from agentforge.workflows.engine import State, Workflow  # noqa: E402


async def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id_str = os.environ.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id_str:
        print("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID required — see README.md", file=sys.stderr)
        sys.exit(1)
    channel_id = int(channel_id_str)

    here = Path(__file__).resolve().parent
    mailbox_root = here / "mailbox"
    mailbox_root.mkdir(parents=True, exist_ok=True)

    wf = Workflow.from_yaml(here / "workflow.yaml")
    adapter = DiscordChannelAdapter(token=token, channel_id=channel_id)
    mbox = FileMailbox(root=mailbox_root)

    print(f"loaded workflow: {wf.name} ({len(wf.steps)} steps)")
    print(f"starting Discord adapter (channel={channel_id}, Ctrl-C to stop)...")

    polling = asyncio.create_task(adapter.start())
    try:
        async for msg in adapter.receive():
            mbox.send(msg)
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

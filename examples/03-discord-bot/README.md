# Example 3 — Discord Bot

Wires the **DiscordChannelAdapter** to a workflow. Any message in the
configured channel flows into the agent's mailbox, the workflow runs, and
the response is sent back to the channel.

## What you'll learn

- Creating a Discord adapter (bot token + channel ID)
- Setting up a bot in the [Discord Developer Portal](https://discord.com/developers/applications)
- Running a long-lived polling adapter + workflow loop

## Setup

1. Create an application + bot in the Discord Developer Portal.
2. Enable the **Message Content Intent** under Bot → Privileged Gateway Intents.
3. Invite the bot to your server with the `bot` scope + `Send Messages` permission.
4. Copy the bot token and the target channel ID (right-click channel →
   Copy Channel ID; requires Developer Mode enabled in Discord settings).
5. Set env vars:

```bash
export DISCORD_BOT_TOKEN="..."
export DISCORD_CHANNEL_ID="1234567890"
```

## Run it

```bash
.venv/bin/python examples/03-discord-bot/run.py
```

Mention or DM your bot, or just send a message in the channel — the bot will
respond with a short LLM-generated reply.

## Files

- `workflow.yaml` — receive → respond with a small LLM call
- `run.py` — boots the adapter and runs the workflow loop
- `mailbox/` — created on first run

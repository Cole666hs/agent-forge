# Example 2 — Telegram Bot

Wires the **TelegramChannelAdapter** to a workflow. Any message the bot
receives flows into the agent's mailbox, the workflow runs, and the response
goes back to the chat.

## What you'll learn

- Creating a Telegram adapter (token + chat_id)
- Running the adapter in `start()` (long-lived aiohttp polling loop)
- Loading + running a workflow per inbound message

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather), copy the token.
2. Send `/start` to your bot in Telegram, then look up the chat_id via
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Set the env vars:

```bash
export TELEGRAM_BOT_TOKEN="123:abc"
export TELEGRAM_CHAT_ID="987654"
```

## Run it

```bash
.venv/bin/python examples/02-telegram-bot/run.py
```

Now send your bot a message in Telegram. It will:

1. Persist the message in `mailbox/hello-agent/inbox/`
2. Run `workflow.yaml` (echo + LLM respond)
3. Send the response back to your chat

## Files

- `workflow.yaml` — receive → respond with a small LLM call
- `run.py` — boots the adapter and the workflow loop
- `mailbox/` — created on first run

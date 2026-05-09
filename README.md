# Mr. Stat

Telegram bot that watches a group chat, stores every message in SQLite, and once per day posts an AI-generated recap with a satirical IQ leaderboard. Users with fewer than 10 words in the period are skipped.

## Setup

```bash
uv sync
cp .env.example .env && echo "!!! fill in the values in .env !!!"
uv run python main.py
```

Add the bot to a group **as an admin** so it can read messages (Telegram bots get group messages only when they are admins or have privacy mode disabled via @BotFather).

## Commands

- `/summary` — anyone in the chat can trigger an on-demand recap for the last 24 hours.
- The same recap fires automatically every day at `SUMMARY_HOUR` in `SUMMARY_TZ` (defaults: 10:00 Europe/Moscow).

## Deploy (Docker)

```bash
git clone <repo> && cd mr-stat
cp .env.example .env && echo "!!! fill in the values in .env !!!"
docker compose up -d
docker compose logs -f
```

The database is stored in a named volume (`db-data`) and survives container restarts. The bot starts automatically on VPS reboot (`restart: unless-stopped`).

## Configuration

All settings come from environment variables, see `.env.example`. The OpenRouter model is configurable via `OPENROUTER_MODEL`; any chat-completion model that supports `response_format: json_object` will work.

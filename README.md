# Mr. Stat

Telegram bot that watches a group chat, stores messages in SQLite, and posts an AI-generated recap with an IQ leaderboard. Users with fewer than `MIN_WORDS` words in the period are skipped from the leaderboard, except allowlisted bot usernames from `SUMMARY_BOT_USERNAMES`.

## Setup

```bash
uv sync
cp .env.example .env && echo "!!! fill in the values in .env !!!"
uv run python main.py
```

Add the bot to a group **as an admin** so it can read messages (Telegram bots get group messages only when they are admins or have privacy mode disabled via @BotFather).

If the bot should see messages from other bots, enable Bot-to-Bot Communication Mode for it in @BotFather: https://core.telegram.org/bots/features#bot-to-bot-communication. In groups, the bot must also be an admin or have Group Privacy Mode disabled to receive all bot messages without explicit mentions or replies.

## AI Providers

The bot supports OpenAI-compatible chat-completions providers and Gemini.

OpenAI-compatible providers are the default for `/summary` and the fallback for `/ask`. Priority: `OPENAI_*`, then `FREEMODEL_*`, then `OPENROUTER_*`.

```env
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL=...

# Or Freemodel:
FREEMODEL_API_KEY=...
FREEMODEL_BASE_URL=https://api.freemodel.dev
FREEMODEL_MODEL=gpt-5.5
```

Gemini is enabled separately:

```env
GEMINI_API_KEY=...
GEMINI_MODEL_ASK=gemini-2.5-pro
GEMINI_MODEL_SUMMARY=
```

With `GEMINI_API_KEY`, `/ask` uses Gemini. Set `GEMINI_MODEL_SUMMARY` too if `/summary` should use Gemini; leave it empty to keep summaries on the OpenAI-compatible provider.

`LLM_TIMEOUT_SECONDS` controls OpenAI-compatible requests. The default is `180`.

## Commands

- `/summary` — anyone in the chat can trigger an on-demand recap for the last 24 hours.
- The same recap fires automatically every day at `SUMMARY_HOUR` in `SUMMARY_TZ` (defaults: 10:00 Europe/Moscow).
- `/ask <question>` — ask the configured model a one-off question with short per-user history.
  You can attach photos with `/ask` in the caption, or reply with `/ask <question>` to a message/photo.
  Replies include nearby stored chat messages as context; replies to new photo albums include all stored album photos.

Summary generation runs as two parallel model calls: the recap body receives the full chronological message history with timestamps, while the IQ leaderboard receives messages grouped by user.

Bot messages are ignored by default unless the bot username is listed in `SUMMARY_BOT_USERNAMES` (default: `ainemotronbot`). Use comma-separated usernames without or with `@`.

## Deploy (Docker)

```bash
cp .env.example .env && echo "!!! fill in the values in .env !!!"
docker compose up -d
docker compose logs -f
```

The database is stored in a named volume (`db-data`) and survives container restarts. The bot starts automatically on VPS reboot (`restart: unless-stopped`).

## Photo messages

When a photo is posted, the bot stores `[photo]` plus the caption, if present. It also stores the Telegram `file_id` and `media_group_id` so `/ask` replies can include all photos from a referenced album. It does not store image bytes in SQLite.

For `/ask`, the bot temporarily downloads attached or replied-to photos and sends them to the ask model with the question. Replied messages also add a short window of nearby stored chat messages to the model prompt. The image bytes are not stored in SQLite; ask history stores only the user question plus an image marker.
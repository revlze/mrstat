# Mr. Stat

Telegram bot that watches a group chat, stores messages in SQLite, and posts an AI-generated recap with a satirical IQ leaderboard. Photo messages are stored as a `[photo]` marker plus caption, without downloading or analyzing the image itself. Users with fewer than `MIN_WORDS` words in the period are skipped from the leaderboard.

## Setup

```bash
uv sync
cp .env.example .env && echo "!!! fill in the values in .env !!!"
uv run python main.py
```

Add the bot to a group **as an admin** so it can read messages (Telegram bots get group messages only when they are admins or have privacy mode disabled via @BotFather).

## AI Providers

`/summary` uses an OpenAI-compatible chat-completions API by default. For Freemodel, use the values from the dashboard:

```env
FREEMODEL_API_KEY=...
FREEMODEL_BASE_URL=https://api.freemodel.dev
FREEMODEL_MODEL=gpt-5.5
GEMINI_MODEL_SUMMARY=
```

`OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` can be used instead for any OpenAI-compatible provider. If neither `OPENAI_*` nor `FREEMODEL_*` is set, the bot falls back to `OPENROUTER_*`.

`/ask` always uses Gemini:

```env
GEMINI_API_KEY=...
GEMINI_MODEL_ASK=gemma-4-31b-it
```

If `GEMINI_MODEL_SUMMARY` is set to a non-empty value, summaries use Gemini too. If it is empty or missing, summaries use Freemodel/OpenAI-compatible settings.

`LLM_TIMEOUT_SECONDS` controls the timeout for OpenAI-compatible summary requests. The default is `180`.

## Commands

- `/summary` — anyone in the chat can trigger an on-demand recap for the last 24 hours.
- The same recap fires automatically every day at `SUMMARY_HOUR` in `SUMMARY_TZ` (defaults: 10:00 Europe/Moscow).
- `/ask <question>` — ask the configured model a one-off question with short per-user history.
  You can attach a photo with `/ask` in the caption, or reply with `/ask <question>` to a photo.

Summary generation runs as two parallel model calls: the recap body receives the full chronological message history with timestamps, while the IQ leaderboard receives messages grouped by user.

## Deploy (Docker)

```bash
cp .env.example .env && echo "!!! fill in the values in .env !!!"
docker compose up -d
docker compose logs -f
```

The database is stored in a named volume (`db-data`) and survives container restarts. The bot starts automatically on VPS reboot (`restart: unless-stopped`).

## Photo messages

When a photo is posted, the bot stores only `[photo]` and the caption, if present. It does not save files, Telegram `file_id`s, or image bytes for summaries.

For `/ask`, the bot can temporarily download one attached or replied-to photo and send it to the ask model with the question. The image bytes are not stored in SQLite; ask history stores only the question plus an `[image attached]` marker.

## Configuration

All settings come from environment variables, see `.env.example`. The summary model must support chat completions and JSON-object responses.

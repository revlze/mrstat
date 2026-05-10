import logging
import time
import traceback
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .analytics import aggregate_by_user, build_summary
from .config import Config
from .db import delete_old_messages, get_active_chat_ids, get_messages_for_period
from .handlers import _chat_ref
from .logging_sink import log_to_chat


logger = logging.getLogger(__name__)
PERIOD_SECONDS = 24 * 3600 # 24 hours


async def run_summary_for_all_chats(bot: Bot, config: Config) -> None:
    since_ts = int(time.time()) - PERIOD_SECONDS
    chat_ids = await get_active_chat_ids(config.db_path, since_ts)
    logger.info("daily summary tick: %d active chat(s)", len(chat_ids))

    bot_id = (await bot.get_me()).id

    for chat_id in chat_ids:
        try:
            chat_obj = await bot.get_chat(chat_id)
            chat_username = chat_obj.username
        except Exception:
            chat_username = None

        try:
            member = await bot.get_chat_member(chat_id, bot_id)
            if member.status in ("kicked", "left"):
                logger.info("skipping chat %d: bot status is %s", chat_id, member.status)
                continue
        except Exception:
            logger.info("skipping chat %d: could not get bot membership", chat_id)
            continue

        try:
            messages = await get_messages_for_period(config.db_path, chat_id, since_ts)
            per_user = aggregate_by_user(messages, config.min_words)
            if not per_user:
                continue
            text = await build_summary(
                per_user,
                api_key=config.openrouter_api_key,
                model=config.openrouter_model,
            )
            await bot.send_message(chat_id, text)
            await log_to_chat(
                bot, config.logs_chat_id, f"daily summary in {_chat_ref(chat_id, chat_username)}:\n{text}"
            )
        except Exception:
            tb = traceback.format_exc()
            await log_to_chat(
                bot,
                config.logs_chat_id,
                f"daily summary failed for {_chat_ref(chat_id, chat_username)}:\n{tb}",
                level=logging.ERROR,
            )

    cutoff_ts = int(time.time()) - config.retention_days * 86400
    deleted = await delete_old_messages(config.db_path, cutoff_ts)
    if deleted:
        logger.info("db rotation: deleted %d messages older than %d days", deleted, config.retention_days)


def build_scheduler(bot: Bot, config: Config) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.summary_tz))
    scheduler.add_job(
        run_summary_for_all_chats,
        CronTrigger(hour=config.summary_hour, minute=0, timezone=ZoneInfo(config.summary_tz)),
        kwargs={"bot": bot, "config": config},
        id="daily-summary",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    return scheduler

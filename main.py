from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import UpdateType
from dotenv import load_dotenv

from bot.config import Config
from bot.db import init_db
from bot.handlers import build_router
from bot.scheduler import build_scheduler


async def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("bot").setLevel(logging.DEBUG)
    logging.getLogger("__main__").setLevel(logging.DEBUG)
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    load_dotenv()
    config = Config.from_env()
    await init_db(config.db_path)

    bot = Bot(token=config.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(config))

    scheduler = build_scheduler(bot, config)
    scheduler.start()
    logging.info(
        "scheduler started: daily summary at %02d:00 %s",
        config.summary_hour,
        config.summary_tz,
    )

    try:
        logging.info("polling started")
        await dispatcher.start_polling(bot, allowed_updates=[
            UpdateType.MESSAGE,
            UpdateType.EDITED_MESSAGE,
            UpdateType.CHAT_MEMBER,
        ])
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.access_control import ExternalMemberTopicMiddleware
from app.config import load_config
from app.db import Database
from app.group_handlers import router as group_router
from app.handlers import router
from app.telethon_manager import TelethonManager


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    db = Database(config.db_path)
    await db.init()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    telethon = TelethonManager(config, db)
    await telethon.initialize()

    dp = Dispatcher()
    # Approved representatives of other factions may write only in General.
    # The middleware is fail-open until /set_general_topic is configured.
    dp.message.outer_middleware(ExternalMemberTopicMiddleware())
    dp.edited_message.outer_middleware(ExternalMemberTopicMiddleware())

    # Group-first router goes first so forum-topic workflows stay inside the group.
    dp.include_router(group_router)
    dp.include_router(router)
    dp["db"] = db
    dp["config"] = config
    dp["telethon"] = telethon

    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(bot)
    finally:
        await telethon.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

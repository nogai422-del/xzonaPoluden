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
from app.telethon_web import TelethonWebAuth


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    db = Database(config.db_path)
    await db.init()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    telethon = TelethonManager(config, db)
    await telethon.initialize()
    telethon_web = TelethonWebAuth(
        telethon,
        host=config.telethon_web_host,
        port=config.telethon_web_port,
        public_url=config.telethon_web_public_url,
        ticket_ttl_seconds=config.telethon_web_ticket_ttl,
    )
    await telethon_web.start()
    logging.info(
        "Telethon authorization window: %s (bind %s:%s)",
        config.telethon_web_public_url,
        config.telethon_web_host,
        config.telethon_web_port,
    )

    dp = Dispatcher()
    # Approved representatives of other factions may write only in General.
    # The middleware is fail-open until /set_general_topic is configured.
    dp.message.outer_middleware(ExternalMemberTopicMiddleware())
    dp.edited_message.outer_middleware(ExternalMemberTopicMiddleware())

    # Group-first router goes first so all ordinary game workflows stay inside
    # their configured forum topics. Private chat remains only as a secure
    # delivery channel for the one-time browser authorization link.
    dp.include_router(group_router)
    dp.include_router(router)
    dp["db"] = db
    dp["config"] = config
    dp["telethon"] = telethon
    dp["telethon_web"] = telethon_web

    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(bot)
    finally:
        await telethon_web.stop()
        await telethon.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

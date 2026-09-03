from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.access_control import ExternalMemberTopicMiddleware, TopicMirrorMiddleware
from app.config import load_config
from app.db import Database
from app.group_handlers import router as group_router
from app.handlers import router as legacy_router
from app.multitask_handlers import router as multitask_router, startup_announcements
from app.housekeeping import configure_housekeeping, housekeeping_loop
from app.telethon_manager import TelethonManager
from app.telethon_web import TelethonWebAuth


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    db = Database(config.db_path)
    await db.init()
    configure_housekeeping(db)

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
    logging.info("Web/Telethon auth listening on %s:%s; public=%s", config.telethon_web_host, config.telethon_web_port, config.telethon_web_public_url)

    dp = Dispatcher()
    # Moderation must run before mirroring so a forbidden external message is never copied.
    dp.message.outer_middleware(ExternalMemberTopicMiddleware())
    dp.message.outer_middleware(TopicMirrorMiddleware())
    dp.edited_message.outer_middleware(ExternalMemberTopicMiddleware())

    # Existing stable modules first, then v7 modules, then legacy private handlers.
    dp.include_router(group_router)
    dp.include_router(multitask_router)
    dp.include_router(legacy_router)
    dp["db"] = db
    dp["config"] = config
    dp["telethon"] = telethon
    dp["telethon_web"] = telethon_web

    await bot.delete_webhook(drop_pending_updates=False)
    announce_task = asyncio.create_task(startup_announcements(bot, db, config, telethon))
    housekeeping_task = asyncio.create_task(housekeeping_loop(bot, 30))
    try:
        await dp.start_polling(bot)
    finally:
        announce_task.cancel()
        housekeeping_task.cancel()
        await telethon_web.stop()
        await telethon.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

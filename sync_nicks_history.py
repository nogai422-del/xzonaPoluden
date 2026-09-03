"""Optional console fallback for the integrated nickname history import.

Normal use: run bot.py and use
Admin -> Nicks -> Import old nicks.
This script reuses the Telethon session already saved by the bot.
"""
from __future__ import annotations

import asyncio

from app.config import load_config
from app.db import Database
from app.telethon_manager import TelethonManager


async def main() -> None:
    config = load_config()
    db = Database(config.db_path)
    await db.init()
    telethon = TelethonManager(config, db)
    await telethon.initialize()
    try:
        if not await telethon.is_connected():
            raise RuntimeError(
                "Telethon is not connected. Open the bot -> Admin -> Telethon and connect the account first."
            )
        result = await telethon.sync_nicks_history()
        print("Nickname history sync complete.")
        print(f"Messages scanned: {result.scanned}")
        print(f"Players found: {result.found}")
        print(f"Imported/updated: {result.imported}")
        print(f"Conflicts: {result.conflicts}")
        print(f"Ignored: {result.invalid}")
    finally:
        await telethon.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

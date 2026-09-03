from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: set[int]
    owner_id: int | None
    db_path: Path


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    raw_admins = os.getenv("ADMIN_IDS", "")
    admin_ids = {
        int(part.strip())
        for part in raw_admins.split(",")
        if part.strip().isdigit()
    }

    owner_raw = os.getenv("OWNER_ID", "").strip()
    owner_id = int(owner_raw) if owner_raw.isdigit() else None
    if owner_id is not None:
        admin_ids.add(owner_id)

    db_path = Path(os.getenv("DB_PATH", "bot.db")).expanduser()
    return Config(bot_token=token, admin_ids=admin_ids, owner_id=owner_id, db_path=db_path)

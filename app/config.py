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
    telethon_web_host: str
    telethon_web_port: int
    telethon_web_public_url: str
    telethon_web_ticket_ttl: int


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

    web_host = os.getenv("TELETHON_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    raw_port = os.getenv("TELETHON_WEB_PORT", "8088").strip()
    web_port = int(raw_port) if raw_port.isdigit() else 8088
    default_public = f"http://127.0.0.1:{web_port}"
    web_public_url = os.getenv("TELETHON_WEB_PUBLIC_URL", default_public).strip().rstrip("/") or default_public
    raw_ttl = os.getenv("TELETHON_WEB_TICKET_TTL", "900").strip()
    web_ticket_ttl = int(raw_ttl) if raw_ttl.isdigit() else 900

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        owner_id=owner_id,
        db_path=db_path,
        telethon_web_host=web_host,
        telethon_web_port=web_port,
        telethon_web_public_url=web_public_url,
        telethon_web_ticket_ttl=web_ticket_ttl,
    )

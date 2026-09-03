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
    announce_on_start: bool
    temp_message_ttl: int
    telethon_member_sync_interval: int


def _public_url(port: int) -> str:
    explicit = os.getenv("TELETHON_WEB_PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    # Bothost commonly exposes a project domain through an environment variable.
    # Users may also define DOMAIN manually if their project does not inject it.
    domain = os.getenv("DOMAIN", "").strip().strip("/")
    if domain:
        if domain.startswith("http://") or domain.startswith("https://"):
            return domain.rstrip("/")
        return f"https://{domain}"
    return f"http://127.0.0.1:{port}"


def load_config() -> Config:
    token = ""
    for key in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN", "API_TOKEN"):
        value = os.getenv(key, "").strip()
        if value:
            token = value
            break
    if not token:
        raise RuntimeError("Telegram bot token is not set (BOT_TOKEN / TELEGRAM_BOT_TOKEN / TOKEN / API_TOKEN).")

    raw_admins = os.getenv("ADMIN_IDS", "")
    admin_ids = {int(part.strip()) for part in raw_admins.split(",") if part.strip().isdigit()}

    owner_raw = os.getenv("OWNER_ID", "").strip()
    owner_id = int(owner_raw) if owner_raw.isdigit() else None
    if owner_id is not None:
        admin_ids.add(owner_id)

    raw_db = os.getenv("DB_PATH", "").strip()
    if raw_db:
        db_path = Path(raw_db).expanduser()
    elif Path("/app/data").is_dir():
        db_path = Path("/app/data/bot.db")
    else:
        db_path = Path("bot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    platform_port = os.getenv("PORT", "").strip()
    raw_port = platform_port or os.getenv("TELETHON_WEB_PORT", "8080").strip()
    web_port = int(raw_port) if raw_port.isdigit() else 8080
    if not 1 <= web_port <= 65535:
        web_port = 8080
    default_host = "0.0.0.0" if platform_port else "127.0.0.1"
    web_host = os.getenv("TELETHON_WEB_HOST", default_host).strip() or default_host
    web_public_url = _public_url(web_port)

    raw_ttl = os.getenv("TELETHON_WEB_TICKET_TTL", "900").strip()
    web_ticket_ttl = int(raw_ttl) if raw_ttl.isdigit() else 900
    announce_on_start = os.getenv("ANNOUNCE_ON_START", "1").strip().lower() not in {"0","false","no","off"}
    raw_temp_ttl = os.getenv("TEMP_MESSAGE_TTL", "90").strip()
    temp_message_ttl = int(raw_temp_ttl) if raw_temp_ttl.isdigit() else 180
    temp_message_ttl = max(30, min(temp_message_ttl, 3600))

    raw_member_sync = os.getenv("TELETHON_MEMBER_SYNC_INTERVAL", "3600").strip()
    telethon_member_sync_interval = int(raw_member_sync) if raw_member_sync.isdigit() else 3600
    telethon_member_sync_interval = max(0, telethon_member_sync_interval)

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        owner_id=owner_id,
        db_path=db_path,
        telethon_web_host=web_host,
        telethon_web_port=web_port,
        telethon_web_public_url=web_public_url,
        telethon_web_ticket_ttl=web_ticket_ttl,
        announce_on_start=announce_on_start,
        temp_message_ttl=temp_message_ttl,
        telethon_member_sync_interval=telethon_member_sync_interval,
    )

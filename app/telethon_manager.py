from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl import functions
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from .config import Config
from .db import Database
from .nicks import extract_nickname
from .roles import parse_profile
from .security import SecretStore


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class PendingLogin:
    api_id: int
    api_hash: str
    phone: str
    phone_code_hash: str
    client: TelegramClient


@dataclass(slots=True)
class SyncResult:
    scanned: int = 0
    found: int = 0
    imported: int = 0
    conflicts: int = 0
    invalid: int = 0


class TelethonManager:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.secrets = SecretStore(config.bot_token)
        self.client: TelegramClient | None = None
        self.pending: PendingLogin | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.phone: str | None = None

    async def initialize(self) -> None:
        auth = await self.db.get_telethon_auth()
        if not auth:
            return
        try:
            api_id = int(auth["api_id"])
        except (ValueError, TypeError):
            self.last_error = "Некорректный API ID в базе."
            return

        api_hash = self.secrets.decrypt(auth.get("api_hash_enc"))
        session_string = self.secrets.decrypt(auth.get("session_enc"))
        if not api_hash or not session_string:
            self.last_error = "Не удалось расшифровать сохранённую Telethon-сессию."
            return

        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                self.last_error = "Сохранённая Telethon-сессия больше не авторизована."
                return
            self.client = client
            self.phone = auth.get("phone") or None
            self.last_error = None
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            self.last_error = f"{type(exc).__name__}: {exc}"

    async def is_connected(self) -> bool:
        if not self.client:
            return False
        try:
            if not self.client.is_connected():
                await self.client.connect()
            return bool(await self.client.is_user_authorized())
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def masked_phone(self) -> str:
        if not self.phone:
            return "—"
        raw = self.phone
        if len(raw) <= 5:
            return raw[:2] + "***"
        return raw[:3] + "***" + raw[-3:]

    async def begin_login(self, api_id: int, api_hash: str, phone: str) -> None:
        async with self._lock:
            await self._close_pending()
            client = TelegramClient(StringSession(), api_id, api_hash)
            await client.connect()
            try:
                sent = await client.send_code_request(phone)
            except FloodWaitError as exc:
                await client.disconnect()
                raise RuntimeError(f"Telegram просит подождать {exc.seconds} сек. перед новым кодом.") from exc
            except Exception:
                await client.disconnect()
                raise
            self.pending = PendingLogin(
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                client=client,
            )

    async def submit_code(self, code: str) -> str:
        if not self.pending:
            raise RuntimeError("Нет активного подключения. Начните настройку заново.")
        try:
            await self.pending.client.sign_in(
                phone=self.pending.phone,
                code=code,
                phone_code_hash=self.pending.phone_code_hash,
            )
        except SessionPasswordNeededError:
            return "password"
        except PhoneCodeInvalidError as exc:
            raise RuntimeError("Неверный код Telegram.") from exc
        except PhoneCodeExpiredError as exc:
            raise RuntimeError("Код Telegram истёк. Начните подключение заново.") from exc
        await self._finalize_pending()
        return "connected"

    async def submit_password(self, password: str) -> None:
        if not self.pending:
            raise RuntimeError("Нет активного подключения. Начните настройку заново.")
        await self.pending.client.sign_in(password=password)
        await self._finalize_pending()

    async def _finalize_pending(self) -> None:
        if not self.pending:
            return
        if not await self.pending.client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована.")
        session_string = self.pending.client.session.save()
        await self.db.set_telethon_auth(
            api_id=self.pending.api_id,
            api_hash_enc=self.secrets.encrypt(self.pending.api_hash),
            phone=self.pending.phone,
            session_enc=self.secrets.encrypt(session_string),
        )
        if self.client and self.client is not self.pending.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = self.pending.client
        self.phone = self.pending.phone
        self.pending = None
        self.last_error = None

    async def _close_pending(self) -> None:
        if self.pending:
            try:
                await self.pending.client.disconnect()
            except Exception:
                pass
            self.pending = None

    async def cancel_pending(self) -> None:
        async with self._lock:
            await self._close_pending()

    async def disconnect(self, clear_saved: bool = False) -> None:
        async with self._lock:
            await self._close_pending()
            if self.client:
                try:
                    await self.client.disconnect()
                finally:
                    self.client = None
            if clear_saved:
                await self.db.clear_telethon_auth()
                self.phone = None
            self.last_error = None

    async def shutdown(self) -> None:
        await self.disconnect(clear_saved=False)

    async def sync_nicks_history(self) -> SyncResult:
        if not await self.is_connected() or not self.client:
            raise RuntimeError("Telethon не подключён.")
        topic = await self.db.get_nicks_topic()
        if not topic:
            raise RuntimeError("Сначала назначьте тему «Ники игроков» командой /set_nicks_topic внутри темы.")

        chat_id, thread_id = topic
        result = SyncResult()
        latest_by_user: dict[int, tuple[str | None, str, object]] = {}

        # Loading dialogs helps Telethon resolve a Bot API -100... chat id on a fresh session.
        await self.client.get_dialogs(limit=None)
        entity = await self.client.get_entity(chat_id)

        async for msg in self.client.iter_messages(entity, reply_to=thread_id, reverse=True):
            result.scanned += 1
            profile = parse_profile(msg.raw_text, allow_legacy=True)
            if not profile or profile.position_code == "__invalid__" or not msg.sender_id:
                result.invalid += 1
                continue
            sender = await msg.get_sender()
            if sender is not None and getattr(sender, "bot", False):
                result.invalid += 1
                continue
            username = getattr(sender, "username", None) if sender else None
            first_name = getattr(sender, "first_name", "") if sender else ""
            last_name = getattr(sender, "last_name", "") if sender else ""
            full_name = " ".join(part for part in (first_name, last_name) if part).strip() or str(msg.sender_id)
            latest_by_user[int(msg.sender_id)] = (username, full_name, profile)

        result.found = len(latest_by_user)
        for telegram_id, (username, full_name, profile) in latest_by_user.items():
            nickname = profile.nickname
            if await self.db.nickname_exists_for_other(telegram_id, nickname):
                result.conflicts += 1
                continue
            await self.db.upsert_player(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                game_nickname=nickname,
            )
            # Historical one-line messages remain valid nick records but have no approved role yet.
            # Any two-line role becomes a pending request unless that exact role is already approved.
            if profile.position_code and profile.position_label:
                current = await self.db.get_player(telegram_id)
                same = bool(
                    current
                    and current.position_status == "approved"
                    and current.position_code == profile.position_code
                    and current.faction_code == profile.faction_code
                )
                if not same:
                    await self.db.create_role_request(
                        telegram_id,
                        profile.position_code,
                        profile.faction_code,
                        profile.position_label,
                    )
            result.imported += 1

        await self.db.set_nicks_history_imported(utc_now(), result.imported)
        return result

    async def send_message(self, target: str, text: str) -> None:
        if not await self.is_connected() or not self.client:
            raise RuntimeError("Telethon не подключён.")
        normalized: str | int = target.strip()
        if normalized.lstrip("-").isdigit():
            normalized = int(normalized)
        entity = await self.client.get_entity(normalized)
        await self.client.send_message(entity, text, parse_mode="html")

    async def list_forum_topics(self, chat_id: int) -> list[tuple[str, int]]:
        """Return forum topic titles and root message ids for a supergroup."""
        if not await self.is_connected() or not self.client:
            raise RuntimeError("Telethon не подключён.")
        await self.client.get_dialogs(limit=None)
        entity = await self.client.get_entity(chat_id)
        result = await self.client(functions.channels.GetForumTopicsRequest(
            channel=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q="",
        ))
        topics = []
        for topic in getattr(result, "topics", []):
            title = str(getattr(topic, "title", "") or "").strip()
            tid = int(getattr(topic, "id", 0) or 0)
            if title and tid:
                topics.append((title, tid))
        return topics

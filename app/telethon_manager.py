from __future__ import annotations

import asyncio
import time
import secrets
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

RESTORE_TIMEOUT = 8
STATUS_TIMEOUT = 5
LOGIN_TIMEOUT = 20


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class PendingLogin:
    api_id: int
    api_hash: str
    phone: str
    phone_code_hash: str
    client: TelegramClient
    method: str = 'phone'


@dataclass(slots=True)
class SyncResult:
    scanned: int = 0
    found: int = 0
    imported: int = 0
    conflicts: int = 0
    invalid: int = 0


@dataclass(slots=True)
class MemberSyncResult:
    chat_id: int
    scanned: int = 0
    active: int = 0
    added: int = 0
    updated: int = 0
    left: int = 0


class TelethonManager:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.secrets = SecretStore(config.bot_token)
        self.client: TelegramClient | None = None
        self.pending: PendingLogin | None = None
        self._lock = asyncio.Lock()
        self._member_sync_lock = asyncio.Lock()
        self.last_error: str | None = None
        self.phone: str | None = None
        self._authorized = False
        self._next_restore_at = 0.0
        self._qr_task: asyncio.Task | None = None
        self._qr_id: str | None = None
        self._qr_state = 'idle'
        self._qr_url = ''
        self._qr_expires = 0.0
        self._qr_error = ''

    @property
    def connected(self) -> bool:
        """Cached status: health checks must never perform Telegram RPCs."""
        return bool(self.client and self._authorized and self.client.is_connected())

    async def initialize(self) -> None:
        self._next_restore_at = time.monotonic() + 30
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

        client = None
        try:
            client = TelegramClient(StringSession(session_string), api_id, api_hash)
            async def restore():
                await client.connect()
                return await client.is_user_authorized()
            if not await asyncio.wait_for(restore(), timeout=RESTORE_TIMEOUT):
                await asyncio.wait_for(client.disconnect(), timeout=2)
                self.last_error = "Сохранённая Telethon-сессия больше не авторизована."
                return
            self.client = client
            self._authorized = True
            self.phone = auth.get("phone") or None
            self.last_error = None
        except Exception as exc:
            try:
                if client:
                    await asyncio.wait_for(client.disconnect(), timeout=2)
            except Exception:
                pass
            self._authorized = False
            self.last_error = f"{type(exc).__name__}: {exc}"

    async def is_connected(self) -> bool:
        async with self._lock:
            if not self.client and time.monotonic() >= self._next_restore_at:
                await self.initialize()
            if not self.client:
                return False
            try:
                async def check():
                    if not self.client.is_connected():
                        await self.client.connect()
                    return bool(await self.client.is_user_authorized())
                self._authorized = await asyncio.wait_for(check(), timeout=STATUS_TIMEOUT)
                self.last_error = None if self._authorized else "Сессия отозвана. Подключите Telethon заново."
                return self._authorized
            except Exception as exc:
                self._authorized = False
                self.last_error = ("Telegram не отвечает. Повторите подключение позже."
                                   if isinstance(exc, asyncio.TimeoutError)
                                   else f"{type(exc).__name__}: {exc}")
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
            try:
                async def request_code():
                    await client.connect()
                    return await client.send_code_request(phone)
                sent = await asyncio.wait_for(request_code(), timeout=LOGIN_TIMEOUT)
            except FloodWaitError as exc:
                await asyncio.wait_for(client.disconnect(), timeout=2)
                raise RuntimeError(f"Telegram просит подождать {exc.seconds} сек. перед новым кодом.") from exc
            except Exception:
                await asyncio.wait_for(client.disconnect(), timeout=2)
                raise
            self.pending = PendingLogin(
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                client=client,
            )

    async def begin_qr_login(self, api_id: int, api_hash: str, *, expected_id: str | None = None) -> str:
        async with self._lock:
            if expected_id is not None and expected_id != self._qr_id:
                raise RuntimeError('Окно входа устарело. Откройте новую ссылку из бота.')
            await self._close_pending()
            client = TelegramClient(StringSession(), api_id, api_hash)
            try:
                async def request_qr():
                    await client.connect()
                    return await client.qr_login()
                qr = await asyncio.wait_for(request_qr(), timeout=LOGIN_TIMEOUT)
            except BaseException:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=2)
                except Exception:
                    pass
                raise
            pending = PendingLogin(api_id, api_hash, '', '', client, method='qr')
            self.pending = pending
            self._qr_id = secrets.token_urlsafe(18)
            self._qr_url = qr.url
            self._qr_expires = qr.expires.timestamp()
            self._qr_state, self._qr_error = 'waiting', ''
            self._qr_task = asyncio.create_task(self._wait_qr(qr, pending))
            # QRLogin.wait must register its update handler before showing the QR.
            await asyncio.sleep(0)
            return self._qr_id

    async def refresh_qr_login(self, login_id: str) -> str:
        pending = self.pending
        if not pending or pending.method != 'qr' or login_id != self._qr_id:
            raise RuntimeError('Начните вход по QR заново.')
        return await self.begin_qr_login(pending.api_id, pending.api_hash, expected_id=login_id)

    def qr_status(self, login_id: str) -> dict:
        if not login_id or login_id != self._qr_id:
            return {'state': 'expired', 'error': 'Начните вход по QR заново.'}
        return dict(state=self._qr_state, url=self._qr_url,
                    expires=self._qr_expires, error=self._qr_error)

    async def _wait_qr(self, qr, pending: PendingLogin) -> None:
        try:
            user = await qr.wait()
            async with self._lock:
                if self.pending is not pending:
                    return
                pending.phone = getattr(user, 'phone', None) or ''
                await self._finalize_pending()
                self._qr_state = 'connected'
        except asyncio.CancelledError:
            raise
        except SessionPasswordNeededError:
            if self.pending is pending:
                self._qr_state = 'password'
        except asyncio.TimeoutError:
            if self.pending is pending:
                self._qr_state = 'expired'
                self._qr_error = 'QR-код истёк. Нажмите «Обновить QR-код».'
        except Exception:
            if self.pending is pending:
                self._qr_state = 'error'
                self._qr_error = 'Не удалось завершить вход. Обновите QR-код и повторите.'

    async def submit_code(self, code: str) -> str:
        async with self._lock:
            return await self._submit_code_locked(code)

    async def _submit_code_locked(self, code: str) -> str:
        if not self.pending:
            raise RuntimeError("Нет активного подключения. Начните настройку заново.")
        if self.pending.method != 'phone':
            raise RuntimeError('Текущий вход ожидает сканирования QR-кода.')
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

    async def submit_password(self, password: str, *, qr_id: str | None = None) -> None:
        async with self._lock:
            if not self.pending:
                raise RuntimeError("Нет активного подключения. Начните настройку заново.")
            if qr_id is not None and (qr_id != self._qr_id or self.pending.method != 'qr'):
                raise RuntimeError('Окно входа устарело. Начните вход заново.')
            if self.pending.method == 'qr' and (qr_id != self._qr_id or self._qr_state != 'password'):
                raise RuntimeError('Окно входа устарело. Начните вход по QR заново.')
            is_qr = self.pending.method == 'qr'
            user = await asyncio.wait_for(self.pending.client.sign_in(password=password), timeout=LOGIN_TIMEOUT)
            if is_qr:
                self.pending.phone = getattr(user, 'phone', None) or ''
            await self._finalize_pending()
            if is_qr:
                self._qr_state = 'connected'

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
        self._authorized = True
        self.phone = self.pending.phone
        self.pending = None
        self.last_error = None

    async def _close_pending(self) -> None:
        if self._qr_task:
            self._qr_task.cancel()
            await asyncio.gather(self._qr_task, return_exceptions=True)
            self._qr_task = None
        self._qr_id = None
        self._qr_state = 'idle'
        self._qr_url = ''
        if self.pending:
            try:
                await asyncio.wait_for(self.pending.client.disconnect(), timeout=2)
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
                    self._authorized = False
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

    async def sync_group_members(self, chat_id: int | None = None) -> MemberSyncResult:
        if self._member_sync_lock.locked():
            raise RuntimeError("Синхронизация участников уже выполняется.")
        async with self._member_sync_lock:
            return await self._sync_group_members_unlocked(chat_id)

    async def _sync_group_members_unlocked(self, chat_id: int | None = None) -> MemberSyncResult:
        """Synchronize current members of the manually selected main group.

        This does not create fake XZONA players for users who have no nickname.
        It stores Telegram membership separately and refreshes identity data of
        already registered players by Telegram ID.
        """
        if not await self.is_connected() or not self.client:
            raise RuntimeError("Telethon не подключён.")
        if chat_id is None:
            chat_id = await self.db.get_primary_chat_id()
        if chat_id is None:
            raise RuntimeError(
                "Сначала вручную привяжите хотя бы один раздел группы, например /set_general_topic."
            )
        await self.client.get_dialogs(limit=None)
        entity = await self.client.get_entity(chat_id)
        members: list[dict] = []
        scanned = 0
        try:
            async for user in self.client.iter_participants(entity):
                scanned += 1
                if getattr(user, "bot", False):
                    continue
                uid = int(getattr(user, "id", 0) or 0)
                if uid <= 0:
                    continue
                first_name = str(getattr(user, "first_name", "") or "").strip()
                last_name = str(getattr(user, "last_name", "") or "").strip()
                full_name = " ".join(x for x in (first_name, last_name) if x).strip()
                if not full_name:
                    full_name = "Удалённый аккаунт" if getattr(user, "deleted", False) else str(uid)
                members.append({
                    "telegram_id": uid,
                    "username": getattr(user, "username", None),
                    "full_name": full_name,
                    "is_deleted": bool(getattr(user, "deleted", False)),
                })
        except FloodWaitError as exc:
            raise RuntimeError(f"Telegram просит подождать {exc.seconds} сек. перед синхронизацией участников.") from exc
        except Exception as exc:
            raise RuntimeError(f"Не удалось получить участников через Telethon: {type(exc).__name__}: {exc}") from exc
        if not members:
            raise RuntimeError("Telethon не вернул ни одного участника. Снимок не применён, чтобы не пометить всех вышедшими.")
        stats = await self.db.sync_group_members(int(chat_id), members)
        self.last_error = None
        return MemberSyncResult(
            chat_id=int(chat_id),
            scanned=scanned,
            active=int(stats["active"]),
            added=int(stats["added"]),
            updated=int(stats["updated"]),
            left=int(stats["left"]),
        )

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

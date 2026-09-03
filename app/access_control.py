from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from .config import Config
from .db import Database
from .roles import is_external_position, parse_profile


class ExternalMemberTopicMiddleware(BaseMiddleware):
    """Keep approved external leaders/deputies out of working-topic discussions.

    They may read everything, but their messages are accepted only in the configured
    General topic. The nickname-registry topic is a deliberate one-message exception
    so a representative can register/update their profile.

    Safety behavior: if General is not configured yet, moderation is disabled rather
    than deleting messages from the wrong place.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        if event.chat.type not in {"group", "supergroup"} or not event.from_user or event.from_user.is_bot:
            return await handler(event, data)

        db: Database | None = data.get("db")
        config: Config | None = data.get("config")
        if db is None or config is None:
            return await handler(event, data)
        if event.from_user.id in config.admin_ids:
            return await handler(event, data)

        player = await db.get_player(event.from_user.id)
        if not player or player.position_status != "approved" or not is_external_position(player.position_code):
            return await handler(event, data)

        general = await db.get_general_topic()
        if not general:
            return await handler(event, data)

        current = (event.chat.id, int(event.message_thread_id or 0))
        nicks = await db.get_nicks_topic()
        if current == general:
            return await handler(event, data)
        if nicks and current == nicks:
            # One narrow exception: an external representative may update their own
            # two-line profile in the nickname registry. Ordinary chatter is still removed.
            profile = parse_profile(event.text or event.caption, allow_legacy=False)
            if profile and profile.position_code not in (None, "__invalid__"):
                return await handler(event, data)

        try:
            await event.delete()
        except Exception:
            # Missing delete rights should never crash the bot.
            pass
        return None

class TopicMirrorMiddleware(BaseMiddleware):
    """Copy new messages from configured source topics into News/Info destinations.

    The bot must be present in the source chat and receive its messages. This is
    intentionally independent from Telethon so continuous mirroring stays simple
    and reliable on hosting.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in {"group", "supergroup"} and event.from_user and not event.from_user.is_bot:
            text = (event.text or event.caption or "").strip()
            if not text.startswith("/"):
                db: Database | None = data.get("db")
                bot = data.get("bot")
                if db is not None and bot is not None:
                    sources = await db.mirror_sources_for(event.chat.id, int(event.message_thread_id or 0))
                    for src in sources:
                        if src["dest_chat_id"] == event.chat.id and int(src["dest_thread_id"] or 0) == int(event.message_thread_id or 0):
                            continue
                        try:
                            kwargs = {"message_thread_id": int(src["dest_thread_id"])} if int(src["dest_thread_id"] or 0) else {}
                            await bot.copy_message(
                                chat_id=int(src["dest_chat_id"]),
                                from_chat_id=event.chat.id,
                                message_id=event.message_id,
                                **kwargs,
                            )
                        except Exception:
                            pass
        return await handler(event, data)

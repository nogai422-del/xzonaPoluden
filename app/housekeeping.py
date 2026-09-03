from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.types import CallbackQuery, Message

from .db import Database

_TASKS: set[asyncio.Task[Any]] = set()
_DB: Database | None = None


def configure_housekeeping(db: Database) -> None:
    global _DB
    _DB = db


def _track(task: asyncio.Task[Any]) -> None:
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _expiry_iso(delay: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(delay)))).isoformat(timespec="seconds")


async def _register(chat_id: int, message_id: int, delay: int) -> None:
    if _DB is None:
        return
    with suppress(Exception):
        await _DB.add_ephemeral_message(chat_id, message_id, _expiry_iso(delay))


async def _forget(chat_id: int, message_id: int) -> None:
    if _DB is None:
        return
    with suppress(Exception):
        await _DB.remove_ephemeral_message(chat_id, message_id)


async def _delete_after(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(max(1, int(delay)))
    with suppress(Exception):
        await bot.delete_message(chat_id, message_id)
    await _forget(chat_id, message_id)


def schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: int = 90) -> None:
    # Persist first so a Bothost restart/redeploy cannot leave the message behind forever.
    _track(asyncio.create_task(_register(chat_id, message_id, delay)))
    _track(asyncio.create_task(_delete_after(bot, chat_id, message_id, delay)))


def _same_topic_kwargs(message: Message) -> dict[str, int]:
    if getattr(message, "is_topic_message", False) and message.message_thread_id is not None:
        return {"message_thread_id": int(message.message_thread_id)}
    return {}


async def topic_answer(message: Message, text: str, **kwargs: Any) -> Message:
    send_kwargs: dict[str, Any] = _same_topic_kwargs(message)
    send_kwargs.update(kwargs)
    return await message.bot.send_message(message.chat.id, text, **send_kwargs)


async def temp_answer(message: Message, text: str, *, ttl: int = 90, **kwargs: Any) -> Message:
    sent = await topic_answer(message, text, **kwargs)
    schedule_delete(message.bot, sent.chat.id, sent.message_id, ttl)
    return sent


async def temp_callback_message(callback: CallbackQuery, text: str, *, ttl: int = 90, **kwargs: Any) -> Message | None:
    if not isinstance(callback.message, Message):
        return None
    sent = await topic_answer(callback.message, text, **kwargs)
    schedule_delete(callback.bot, sent.chat.id, sent.message_id, ttl)
    return sent


async def temp_bot_message(bot: Bot, chat_id: int, text: str, *, ttl: int = 90, **kwargs: Any) -> Message:
    sent = await bot.send_message(chat_id, text, **kwargs)
    schedule_delete(bot, sent.chat.id, sent.message_id, ttl)
    return sent


async def delete_incoming_later(message: Message, delay: int = 1) -> None:
    schedule_delete(message.bot, message.chat.id, message.message_id, delay)


async def cleanup_due_messages(bot: Bot, *, limit: int = 500) -> int:
    if _DB is None:
        return 0
    rows = await _DB.list_due_ephemeral_messages(datetime.now(timezone.utc).isoformat(timespec="seconds"), limit=limit)
    cleaned = 0
    for chat_id, message_id in rows:
        # Telegram returns an error if the message is already gone; that is still a successful cleanup state.
        with suppress(Exception):
            await bot.delete_message(chat_id, message_id)
        await _forget(chat_id, message_id)
        cleaned += 1
    return cleaned


async def cleanup_all_tracked_messages(bot: Bot, *, limit: int = 5000) -> int:
    """Admin/manual emergency cleanup of all transient bot messages tracked in DB.

    Persistent topic instructions/cards are never registered here and therefore stay intact.
    """
    if _DB is None:
        return 0
    rows = await _DB.list_all_ephemeral_messages(limit=limit)
    cleaned = 0
    for chat_id, message_id in rows:
        with suppress(Exception):
            await bot.delete_message(chat_id, message_id)
        await _forget(chat_id, message_id)
        cleaned += 1
    return cleaned


async def housekeeping_loop(bot: Bot, interval: int = 30) -> None:
    # First sweep handles transient messages whose in-memory timers died during a redeploy.
    with suppress(Exception):
        await cleanup_due_messages(bot, limit=2000)
    while True:
        await asyncio.sleep(max(10, int(interval)))
        with suppress(Exception):
            await cleanup_due_messages(bot, limit=1000)

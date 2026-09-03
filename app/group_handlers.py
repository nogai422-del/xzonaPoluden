from __future__ import annotations

import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .db import Database, MarketOrder as DbMarketOrder, MarketOrderItem, RoleRequest, StorageItem
from .keyboards import (
    group_admin_back,
    group_admin_menu,
    group_market_admin_menu,
    group_market_cart_keyboard,
    group_market_comment_keyboard,
    group_market_panel,
    group_nicks_admin_menu,
    group_players_keyboard,
    group_quantity_keyboard,
    group_recent_names_keyboard,
    group_role_request_review_keyboard,
    group_role_requests_keyboard,
    group_storage_comment_keyboard,
    group_storage_confirm_keyboard,
    group_storage_panel,
    group_telethon_menu,
    item_keyboard,
    market_order_status_keyboard,
    storage_items_keyboard,
)
from .roles import has_position_permission, is_external_position, parse_position, position_display
from .states import GroupAddItem, GroupMarketOrder, GroupMarketSettings
from .telethon_manager import TelethonManager

router = Router(name="group_first")
GROUP_TYPES = {"group", "supergroup"}


def is_admin(user_id: int, config: Config) -> bool:
    return user_id in config.admin_ids


async def has_permission(user_id: int, permission: str, db: Database, config: Config) -> bool:
    if is_admin(user_id, config):
        return True
    player = await db.get_player(user_id)
    if not player:
        return False
    if player.position_status != "approved" and player.position_code is not None:
        return False
    return has_position_permission(player.position_code, permission)


async def require_permission_callback(
    callback: CallbackQuery, permission: str, db: Database, config: Config, message: str = "Недостаточно прав"
) -> bool:
    if await has_permission(callback.from_user.id, permission, db, config):
        return True
    await callback.answer(message, show_alert=True)
    return False


async def can_manage_roles(user_id: int, db: Database, config: Config) -> bool:
    return await has_permission(user_id, "roles.manage", db, config)


def can_manage_telethon(user_id: int, config: Config) -> bool:
    if config.owner_id is not None:
        return user_id == config.owner_id
    return is_admin(user_id, config)


def fmt_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def storage_item_text(item: StorageItem) -> str:
    status = "🟡 На хранении" if item.status == "stored" else "🟢 Выдано"
    lines = [
        f"<b>📦 Предмет #{item.id}</b>",
        "",
        f"👤 Владелец: <b>{escape(item.player_nickname)}</b>",
        f"🎒 Предмет: <b>{escape(item.item_name)}</b>",
        f"🔢 Количество: <b>{item.quantity}</b>",
        f"📝 Комментарий: {escape(item.comment) if item.comment else '—'}",
        f"📌 Статус: {status}",
        f"📥 Принято: {fmt_dt(item.accepted_at)}",
    ]
    if item.status == "issued":
        lines.append(f"📤 Выдано: {fmt_dt(item.issued_at)}")
    return "\n".join(lines)


WORKFLOW_LABELS = {
    "pending": "🟡 Ожидает Торговца",
    "accepted": "🔵 Принят Торговцем",
    "assembled": "🟣 Заказ собран",
    "issued": "✅ Выдан игроку",
    "rejected": "❌ Отклонён",
}


def merchant_display(target: str | None) -> str:
    if not target:
        return "—"
    value = target.strip()
    if value.isdigit():
        return f'<a href="tg://user?id={value}">Торговец ГП</a>'
    return escape(value)


def market_order_group_text(order: DbMarketOrder, items: list[MarketOrderItem]) -> str:
    username = f"@{escape(order.requester_username)}" if order.requester_username else "—"
    delivery = {
        "created": "⏳ ещё не отправлялось",
        "sent": "✅ Торговец уведомлён",
        "failed": "⚠️ личное уведомление не доставлено; заказ остаётся в теме",
    }.get(order.status, escape(order.status))
    lines = [
        f"<b>🛒 ЗАКАЗ ГП #{order.id}</b>",
        "",
        f"👤 Игрок: <b>{escape(order.requester_nickname)}</b>",
        f"Telegram: {username}",
        f"ID: <code>{order.requester_id}</code>",
        f"👨‍💼 Торговец: {merchant_display(order.merchant_target)}",
        "",
        "<b>📦 Позиции:</b>",
    ]
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}. {escape(item.item_name)} × <b>{item.quantity}</b>")
    lines.extend(
        [
            "",
            f"📝 Комментарий: {escape(order.comment) if order.comment else '—'}",
            f"📌 Статус заказа: <b>{WORKFLOW_LABELS.get(order.workflow_status, escape(order.workflow_status))}</b>",
            f"📨 Доставка Торговцу: {delivery}",
            f"🕓 Создан: {fmt_dt(order.created_at)}",
        ]
    )
    return "\n".join(lines)


def cart_text(items: list[dict], comment: str | None = None) -> str:
    lines = ["<b>🛒 Новый заказ ГП</b>", "", "<b>Позиции:</b>"]
    if not items:
        lines.append("Пока пусто.")
    else:
        for idx, item in enumerate(items, 1):
            lines.append(f"{idx}. {escape(str(item['name']))} × <b>{int(item['quantity'])}</b>")
    lines.extend(["", f"📝 Комментарий: {escape(comment) if comment else '—'}"])
    return "\n".join(lines)


async def safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


def topic_tuple_from_message(message: Message) -> tuple[int, int] | None:
    if not message.is_topic_message or message.message_thread_id is None:
        return None
    return message.chat.id, message.message_thread_id


def topic_tuple_from_callback(callback: CallbackQuery) -> tuple[int, int] | None:
    message = callback.message
    if not isinstance(message, Message):
        return None
    return topic_tuple_from_message(message)


async def flow_matches_message(message: Message, state: FSMContext) -> bool:
    data = await state.get_data()
    return (
        data.get("flow_chat_id") == message.chat.id
        and data.get("flow_thread_id") == message.message_thread_id
    )


async def flow_matches_callback(callback: CallbackQuery, state: FSMContext) -> bool:
    if not isinstance(callback.message, Message):
        return False
    data = await state.get_data()
    return (
        data.get("flow_chat_id") == callback.message.chat.id
        and data.get("flow_thread_id") == callback.message.message_thread_id
    )


async def require_configured_topic(callback: CallbackQuery, configured: tuple[int, int] | None, label: str) -> bool:
    current = topic_tuple_from_callback(callback)
    if not configured or current != configured:
        await callback.answer(f"Эта кнопка работает только в настроенной теме «{label}».", show_alert=True)
        return False
    return True


async def merchant_authorized(
    user_id: int, username: str | None, target: str | None, db: Database, config: Config
) -> bool:
    if is_admin(user_id, config):
        return True
    player = await db.get_player(user_id)
    if player and player.position_status == "approved" and is_external_position(player.position_code):
        return False
    if await has_permission(user_id, "market.manage", db, config):
        return True
    if not target:
        return False
    target = target.strip()
    if target.lstrip("-").isdigit():
        return user_id == int(target)
    if target.startswith("@") and username:
        return username.lower() == target[1:].lower()
    return False


async def send_merchant_notification(
    bot: Bot,
    telethon: TelethonManager,
    target: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> tuple[str, int | None]:
    target = target.strip()
    bot_error: Exception | None = None
    if target.lstrip("-").isdigit():
        try:
            sent = await bot.send_message(int(target), text, reply_markup=reply_markup)
            return "bot", sent.message_id
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            bot_error = exc

    if await telethon.is_connected():
        try:
            extra = "\n\n<i>Статус заказа меняется кнопками в теме «Рынок ГП».</i>" if reply_markup else ""
            await telethon.send_message(target, text + extra)
            return "telethon", None
        except Exception as exc:
            if bot_error:
                raise RuntimeError(f"Bot API: {bot_error}; Telethon: {exc}") from exc
            raise

    if bot_error:
        raise RuntimeError(
            "Торговец не открыл личный чат с ботом, а Telethon не подключён. "
            "Заказ всё равно опубликован в теме Рынок ГП."
        ) from bot_error
    raise RuntimeError("Для отправки Торговцу по @username подключите Telethon.")


# ---------------------------------------------------------------------------
# Topic binding and permanent group panels
# ---------------------------------------------------------------------------

@router.message(Command("set_general_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_general_topic_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await message.answer("Недостаточно прав.")
        return
    # General is normally a forum topic. If Telegram reports no thread id, store 0.
    thread_id = int(message.message_thread_id or 0)
    await db.set_general_topic(message.chat.id, thread_id)
    await message.answer(
        "✅ Эта беседа назначена как <b>General</b>.\n\n"
        "Представители других группировок после подтверждения роли смогут писать только здесь. "
        "Остальные темы они смогут читать, но их сообщения там бот будет удалять."
    )


@router.message(Command("set_storage_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_storage_topic_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        await message.answer("Недостаточно прав.")
        return
    topic = topic_tuple_from_message(message)
    if not topic:
        await message.answer("Отправьте /set_storage_topic прямо внутри темы «Хранилище».")
        return
    await db.set_storage_topic(*topic)
    await message.answer(
        "<b>📦 ХРАНИЛИЩЕ ГРУППИРОВКИ</b>\n\n"
        "Администраторы принимают и выдают предметы прямо здесь.\n"
        "Актуальный список открывается кнопкой «📋 На хранении».",
        reply_markup=group_storage_panel(),
    )


@router.message(Command("storage_panel"), F.chat.type.in_(GROUP_TYPES))
async def storage_panel_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.view", db, config):
        return
    configured = await db.get_storage_topic()
    if topic_tuple_from_message(message) != configured:
        await message.answer("Эта команда должна быть отправлена в настроенной теме Хранилища.")
        return
    items_count, players_count = await db.storage_stats()
    await message.answer(
        "<b>📦 ХРАНИЛИЩЕ ГРУППИРОВКИ</b>\n\n"
        f"На хранении: <b>{items_count}</b>\nИгроков с имуществом: <b>{players_count}</b>",
        reply_markup=group_storage_panel(),
    )


@router.message(Command("set_market_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_market_topic_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.manage", db, config):
        await message.answer("Недостаточно прав.")
        return
    topic = topic_tuple_from_message(message)
    if not topic:
        await message.answer("Отправьте /set_market_topic прямо внутри темы «Рынок ГП».")
        return
    await db.set_market_topic(*topic)
    await message.answer(
        "<b>🛒 РЫНОК ГП</b>\n\n"
        "Заказы формируются прямо в этой теме.\n"
        "Игрок нажимает «Новый заказ», добавляет позиции и отправляет его Торговцу ГП.",
        reply_markup=group_market_panel(),
    )


@router.message(Command("market_panel"), F.chat.type.in_(GROUP_TYPES))
async def market_panel_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.create", db, config):
        return
    configured = await db.get_market_topic()
    if topic_tuple_from_message(message) != configured:
        await message.answer("Эта команда должна быть отправлена в настроенной теме Рынка ГП.")
        return
    await message.answer("<b>🛒 РЫНОК ГП</b>\n\nСоздавайте заказы кнопкой ниже.", reply_markup=group_market_panel())


@router.message(Command("set_nicks_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_nicks_topic_group(message: Message, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await message.answer("Недостаточно прав.")
        return
    topic = topic_tuple_from_message(message)
    if not topic:
        await message.answer("Отправьте /set_nicks_topic прямо внутри темы «Ники игроков».")
        return
    await db.set_nicks_topic(*topic)
    connected = await telethon.is_connected()
    me = await bot.get_me()
    await message.answer(
        "<b>👥 НИКИ ИГРОКОВ</b>\n\n"
        "✅ Эта тема назначена реестром участников.\n"
        "Формат сообщения: <code>Ник\nДолжность</code>.\n"
        "Все должности активируются только после подтверждения руководства.\n\n"
        + ("📚 Старые сообщения можно импортировать кнопкой ниже." if connected else "📚 Для старых сообщений один раз подключите Telethon."),
        reply_markup=group_nicks_admin_menu(
            connected=connected,
            bot_username=me.username,
            can_manage=can_manage_telethon(message.from_user.id, config),
            topic_ready=True,
        ),
    )


@router.message(Command("nicks_status"), F.chat.type.in_(GROUP_TYPES))
async def nicks_status_group(message: Message, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        return
    topic = await db.get_nicks_topic()
    imported_at, imported_count = await db.get_nicks_history_import_status()
    connected = await telethon.is_connected()
    count = await db.count_players()
    pending_roles = await db.count_pending_role_requests()
    me = await bot.get_me()
    await message.answer(
        "<b>👥 Ники игроков</b>\n\n"
        f"Тема: {'✅ настроена' if topic else '⚠️ не настроена'}\n"
        f"Игроков в базе: <b>{count}</b>\n"
        f"Ожидают подтверждения роли: <b>{pending_roles}</b>\n"
        f"Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n"
        f"Старая история: {'✅ ' + str(imported_count) + ' записей, ' + escape(imported_at) if imported_at else '⚠️ ещё не импортирована'}",
        reply_markup=group_nicks_admin_menu(
            connected=connected,
            bot_username=me.username,
            can_manage=can_manage_telethon(message.from_user.id, config),
            topic_ready=topic is not None,
        ),
    )


# ---------------------------------------------------------------------------
# Group admin dashboard. Telethon secrets still go through private chat only.
# ---------------------------------------------------------------------------

async def group_admin_text(db: Database, telethon: TelethonManager) -> str:
    storage = await db.get_storage_topic()
    nicks = await db.get_nicks_topic()
    market = await db.get_market_topic()
    general = await db.get_general_topic()
    merchant = await db.get_market_merchant_target()
    pending_roles = await db.count_pending_role_requests()
    connected = await telethon.is_connected()
    return (
        "<b>⚙️ УПРАВЛЕНИЕ ГРУППОЙ</b>\n\n"
        f"💬 General: {'✅' if general else '⚠️ не назначено'}\n"
        f"📦 Хранилище: {'✅' if storage else '⚠️ не назначено'}\n"
        f"👥 Ники игроков: {'✅' if nicks else '⚠️ не назначено'}\n"
        f"🎖 Запросов должностей: <b>{pending_roles}</b>\n"
        f"🛒 Рынок ГП: {'✅' if market else '⚠️ не назначено'}\n"
        f"👤 Торговец ГП: <code>{escape(merchant) if merchant else 'не назначен'}</code>\n"
        f"🔐 Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n\n"
        "Для ограничения внешних лидеров/заместителей отправьте <code>/set_general_topic</code> в теме General."
    )


@router.message(Command("set_role"), F.chat.type.in_(GROUP_TYPES))
async def set_role_command(message: Message, db: Database, config: Config, bot: Bot):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await message.answer("Недостаточно прав.")
        return
    source = message.reply_to_message
    if not source or not source.from_user or source.from_user.is_bot:
        await message.answer(
            "Ответьте этой командой на сообщение участника:\n"
            "<code>/set_role Кладовщик</code>\n"
            "или <code>/set_role Лидер Долга</code>."
        )
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите должность после команды: <code>/set_role Рядовой</code>.")
        return
    parsed = parse_position(parts[1])
    if not parsed:
        await message.answer("Неизвестная должность. Проверьте написание.")
        return
    position_code, faction_code, label = parsed
    player = await db.get_player(source.from_user.id)
    if not player:
        await message.answer("Игрок ещё не найден в реестре «Ники игроков».")
        return
    await db.set_player_role(source.from_user.id, position_code, faction_code, message.from_user.id)
    await message.answer(f"✅ <b>{escape(player.game_nickname)}</b> назначен: <b>{escape(label)}</b>.")
    try:
        await bot.send_message(source.from_user.id, f"✅ Вам назначена должность: <b>{escape(label)}</b>.")
    except Exception:
        pass


@router.message(Command("clear_role"), F.chat.type.in_(GROUP_TYPES))
async def clear_role_command(message: Message, db: Database, config: Config):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await message.answer("Недостаточно прав.")
        return
    source = message.reply_to_message
    if not source or not source.from_user or source.from_user.is_bot:
        await message.answer("Ответьте <code>/clear_role</code> на сообщение участника.")
        return
    player = await db.get_player(source.from_user.id)
    if not player:
        await message.answer("Игрок не найден в базе.")
        return
    await db.clear_player_role(source.from_user.id, message.from_user.id)
    await message.answer(f"✅ Должность <b>{escape(player.game_nickname)}</b> снята.")


@router.message(Command("admin"), F.chat.type.in_(GROUP_TYPES))
async def group_admin(message: Message, db: Database, config: Config, telethon: TelethonManager):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await message.answer("Недостаточно прав.")
        return
    await message.answer(await group_admin_text(db, telethon), reply_markup=group_admin_menu())


@router.callback_query(F.data == "gadmin:home", F.message.chat.type.in_(GROUP_TYPES))
async def group_admin_home(callback: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.message.edit_text(await group_admin_text(db, telethon), reply_markup=group_admin_menu())
    await callback.answer()


@router.callback_query(F.data == "gadmin:storage", F.message.chat.type.in_(GROUP_TYPES))
async def group_admin_storage(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    topic = await db.get_storage_topic()
    count, players = await db.storage_stats()
    text = (
        "<b>📦 Хранилище</b>\n\n"
        f"Тема: {'✅ настроена' if topic else '⚠️ не настроена'}\n"
        f"На хранении: <b>{count}</b>\nИгроков с имуществом: <b>{players}</b>\n\n"
        "Для привязки откройте тему Хранилище и отправьте <code>/set_storage_topic</code>."
    )
    await callback.message.edit_text(text, reply_markup=group_admin_back())
    await callback.answer()


@router.callback_query(F.data == "gadmin:nicks", F.message.chat.type.in_(GROUP_TYPES))
async def group_admin_nicks(callback: CallbackQuery, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    topic = await db.get_nicks_topic()
    imported_at, imported_count = await db.get_nicks_history_import_status()
    connected = await telethon.is_connected()
    count = await db.count_players()
    pending_roles = await db.count_pending_role_requests()
    me = await bot.get_me()
    text = (
        "<b>👥 Ники игроков</b>\n\n"
        f"Тема: {'✅ настроена' if topic else '⚠️ не настроена'}\n"
        f"Игроков в базе: <b>{count}</b>\n"
        f"Ожидают подтверждения роли: <b>{pending_roles}</b>\n"
        f"Старый импорт: {'✅ ' + str(imported_count) + ' записей, ' + escape(imported_at) if imported_at else '⚠️ не выполнялся'}\n"
        f"Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n\n"
        "Для привязки темы отправьте <code>/set_nicks_topic</code> внутри «Ники игроков»."
    )
    await callback.message.edit_text(
        text,
        reply_markup=group_nicks_admin_menu(
            connected=connected,
            bot_username=me.username,
            can_manage=can_manage_telethon(callback.from_user.id, config),
            topic_ready=topic is not None,
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "gadmin:roles", F.message.chat.type.in_(GROUP_TYPES))
async def group_admin_roles(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    requests = await db.list_pending_role_requests(limit=20)
    pending_total = await db.count_pending_role_requests()
    text = (
        "<b>🎖 РОЛИ И ДОСТУП</b>\n\n"
        f"Ожидают подтверждения: <b>{pending_total}</b>\n\n"
        "Любая заявленная должность, включая Рядового, требует подтверждения Лидером, "
        "Заместителем лидера или техническим администратором."
    )
    if not requests:
        text += "\n\n✅ Новых запросов нет."
    await callback.message.edit_text(text, reply_markup=group_role_requests_keyboard(requests))
    await callback.answer()


@router.callback_query(F.data.startswith("grole:view:"), F.message.chat.type.in_(GROUP_TYPES))
async def group_role_request_view(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    request_id = int(callback.data.rsplit(":", 1)[1])
    req = await db.get_role_request(request_id)
    if not req:
        await callback.answer("Запрос не найден", show_alert=True)
        return
    player = await db.get_player(req.telegram_id)
    current = position_display(player.position_code, player.faction_code) if player else "—"
    text = (
        f"<b>🎖 Запрос должности #{req.id}</b>\n\n"
        f"👤 Игрок: <b>{escape(req.player_nickname)}</b>\n"
        f"Telegram ID: <code>{req.telegram_id}</code>\n"
        f"Текущая подтверждённая должность: <b>{escape(current)}</b>\n"
        f"Запрошено: <b>{escape(req.requested_label)}</b>\n"
        f"Статус: <b>{escape(req.status)}</b>\n"
        f"Создан: {fmt_dt(req.requested_at)}"
    )
    markup = group_role_request_review_keyboard(req.id) if req.status == "pending" else group_role_requests_keyboard([])
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^grole:(approve|reject):\d+$"), F.message.chat.type.in_(GROUP_TYPES))
async def group_role_request_review(callback: CallbackQuery, db: Database, config: Config, bot: Bot):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, action, raw_id = callback.data.split(":", 2)
    req = await db.review_role_request(int(raw_id), callback.from_user.id, action == "approve")
    if not req:
        await callback.answer("Запрос уже обработан или не найден", show_alert=True)
        return
    if action == "approve":
        result = f"✅ Подтверждено: <b>{escape(req.player_nickname)}</b> — <b>{escape(req.requested_label)}</b>."
        try:
            await bot.send_message(
                req.telegram_id,
                f"✅ Ваша должность подтверждена: <b>{escape(req.requested_label)}</b>."
            )
        except Exception:
            pass
    else:
        result = f"❌ Запрос <b>{escape(req.player_nickname)}</b> на должность <b>{escape(req.requested_label)}</b> отклонён."
        try:
            await bot.send_message(
                req.telegram_id,
                f"❌ Запрос на должность <b>{escape(req.requested_label)}</b> отклонён руководством."
            )
        except Exception:
            pass
    requests = await db.list_pending_role_requests(limit=20)
    await callback.message.edit_text(result, reply_markup=group_role_requests_keyboard(requests))
    await callback.answer("Готово")


@router.callback_query(F.data == "gadmin:telethon", F.message.chat.type.in_(GROUP_TYPES))
async def group_admin_telethon(callback: CallbackQuery, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    connected = await telethon.is_connected()
    topic = await db.get_nicks_topic()
    me = await bot.get_me()
    text = (
        "<b>🔐 Telethon</b>\n\n"
        f"Статус: {'🟢 подключён' if connected else '🔴 не подключён'}\n"
        f"Аккаунт: <code>{escape(telethon.masked_phone())}</code>\n\n"
        "Через Telethon бот один раз дочитывает старые ники и при необходимости отправляет заказы Торговцу.\n\n"
        "API HASH, код входа и 2FA никогда не вводятся в общей группе. Подключение открывается в личном чате только владельцу."
    )
    await callback.message.edit_text(
        text,
        reply_markup=group_telethon_menu(
            connected=connected,
            bot_username=me.username,
            can_manage=can_manage_telethon(callback.from_user.id, config),
            can_sync=topic is not None,
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "gtelethon:disconnect", F.message.chat.type.in_(GROUP_TYPES))
async def group_telethon_disconnect(callback: CallbackQuery, config: Config, bot: Bot, telethon: TelethonManager):
    if not can_manage_telethon(callback.from_user.id, config):
        await callback.answer("Только владелец может отключить Telethon.", show_alert=True)
        return
    await telethon.disconnect(clear_saved=True)
    me = await bot.get_me()
    await callback.message.edit_text(
        "🔴 Telethon отключён, сохранённая сессия удалена.",
        reply_markup=group_telethon_menu(
            connected=False,
            bot_username=me.username,
            can_manage=True,
            can_sync=False,
        ),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Storage flow entirely inside the configured forum topic
# ---------------------------------------------------------------------------

async def show_group_players(callback: CallbackQuery, db: Database, page: int, *, edit: bool) -> None:
    page_size = 10
    total = await db.count_players()
    players = await db.list_players(limit=page_size, offset=page * page_size)
    if not players:
        await callback.answer("В базе пока нет игроков. Сначала заполните тему Ники игроков.", show_alert=True)
        return
    text = "👤 <b>Выберите владельца предмета:</b>"
    markup = group_players_keyboard(players, page, total, page_size)
    if edit:
        await callback.message.edit_text(text, reply_markup=markup)
    else:
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "gstorage:add", F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_add(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await require_permission_callback(callback, "storage.manage", db, config, "Принимать предметы может Кладовщик, Лидер или Заместитель."):
        return
    topic = await db.get_storage_topic()
    if not await require_configured_topic(callback, topic, "Хранилище"):
        return
    await state.clear()
    await state.update_data(flow_chat_id=topic[0], flow_thread_id=topic[1])
    await show_group_players(callback, db, 0, edit=False)


@router.callback_query(F.data.startswith("gstorage:players_page:"), F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_players_page(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await require_permission_callback(callback, "storage.manage", db, config):
        return
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела. Начните приём предмета заново.", show_alert=True)
        return
    page = int(callback.data.rsplit(":", 1)[1])
    await show_group_players(callback, db, page, edit=True)


@router.callback_query(F.data.startswith("gstorage:player:"), F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_choose_player(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await require_permission_callback(callback, "storage.manage", db, config):
        return
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        return
    player_id = int(callback.data.rsplit(":", 1)[1])
    player = await db.get_player(player_id)
    if not player:
        await callback.answer("Игрок не найден", show_alert=True)
        return
    recent = await db.recent_item_names()
    await state.update_data(player_id=player_id, player_nickname=player.game_nickname, recent_names=recent)
    await state.set_state(GroupAddItem.name)
    markup = group_recent_names_keyboard(recent)
    if markup is None:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")]])
    await callback.message.edit_text(
        f"👤 Владелец: <b>{escape(player.game_nickname)}</b>\n\n"
        "🎒 Напишите название предмета вручную" + (" или выберите недавний:" if recent else ":"),
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gstorage:recent:"), F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_recent(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config) or not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела или недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    names = data.get("recent_names", [])
    idx = int(callback.data.rsplit(":", 1)[1])
    if not 0 <= idx < len(names):
        await callback.answer("Список устарел.", show_alert=True)
        return
    name = names[idx]
    await state.update_data(item_name=name)
    await state.set_state(GroupAddItem.quantity)
    await callback.message.edit_text(
        f"🎒 Предмет: <b>{escape(name)}</b>\n\n🔢 Выберите количество или введите своё:",
        reply_markup=group_quantity_keyboard("gstorage"),
    )
    await callback.answer()


@router.message(GroupAddItem.name, F.chat.type.in_(GROUP_TYPES))
async def group_storage_name(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await message.answer("Продолжите добавление предмета в теме Хранилище.")
        return
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await message.answer("Название должно быть от 1 до 80 символов.")
        return
    await state.update_data(item_name=name)
    await state.set_state(GroupAddItem.quantity)
    await safe_delete(message)
    await message.answer(
        f"🎒 Предмет: <b>{escape(name)}</b>\n\n🔢 Выберите количество или введите своё:",
        reply_markup=group_quantity_keyboard("gstorage"),
    )


@router.callback_query(GroupAddItem.quantity, F.data.startswith("gstorage:qty:"), F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_qty_button(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config) or not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела или недостаточно прав.", show_alert=True)
        return
    value = callback.data.rsplit(":", 1)[1]
    if value == "custom":
        await callback.message.edit_text("🔢 Введите количество от 1 до 9999:")
        await callback.answer()
        return
    await state.update_data(quantity=int(value))
    await state.set_state(GroupAddItem.comment)
    await callback.message.edit_text("📝 Добавьте комментарий или пропустите:", reply_markup=group_storage_comment_keyboard())
    await callback.answer()


@router.message(GroupAddItem.quantity, F.chat.type.in_(GROUP_TYPES))
async def group_storage_qty_text(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await message.answer("Продолжите добавление предмета в теме Хранилище.")
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await message.answer("Введите целое количество от 1 до 9999.")
        return
    await state.update_data(quantity=int(raw))
    await state.set_state(GroupAddItem.comment)
    await safe_delete(message)
    await message.answer("📝 Добавьте комментарий или пропустите:", reply_markup=group_storage_comment_keyboard())


async def show_group_storage_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer(
        "<b>📦 Новый предмет</b>\n\n"
        f"👤 Владелец: <b>{escape(data['player_nickname'])}</b>\n"
        f"🎒 Предмет: <b>{escape(data['item_name'])}</b>\n"
        f"🔢 Количество: <b>{data['quantity']}</b>\n"
        f"📝 Комментарий: {escape(data['comment']) if data.get('comment') else '—'}",
        reply_markup=group_storage_confirm_keyboard(),
    )


@router.callback_query(GroupAddItem.comment, F.data == "gstorage:comment_skip", F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_comment_skip(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config) or not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела или недостаточно прав.", show_alert=True)
        return
    await state.update_data(comment=None)
    await state.set_state(GroupAddItem.confirm)
    data = await state.get_data()
    await callback.message.edit_text(
        "<b>📦 Новый предмет</b>\n\n"
        f"👤 Владелец: <b>{escape(data['player_nickname'])}</b>\n"
        f"🎒 Предмет: <b>{escape(data['item_name'])}</b>\n"
        f"🔢 Количество: <b>{data['quantity']}</b>\n"
        "📝 Комментарий: —",
        reply_markup=group_storage_confirm_keyboard(),
    )
    await callback.answer()


@router.message(GroupAddItem.comment, F.chat.type.in_(GROUP_TYPES))
async def group_storage_comment(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await message.answer("Продолжите добавление предмета в теме Хранилище.")
        return
    comment = (message.text or "").strip()
    if len(comment) > 500:
        await message.answer("Комментарий слишком длинный. Максимум 500 символов.")
        return
    await state.update_data(comment=comment or None)
    await state.set_state(GroupAddItem.confirm)
    await safe_delete(message)
    await show_group_storage_confirm(message, state)


@router.callback_query(GroupAddItem.confirm, F.data == "gstorage:confirm", F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_confirm(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config) or not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела или недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    item_id = await db.add_storage_item(
        player_id=data["player_id"],
        item_name=data["item_name"],
        quantity=data["quantity"],
        comment=data.get("comment"),
        accepted_by=callback.from_user.id,
    )
    await state.clear()
    item = await db.get_storage_item(item_id)
    await callback.message.edit_text(
        "✅ <b>Принято на хранение</b>\n\n" + storage_item_text(item),
        reply_markup=item_keyboard(item.id, True),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data.in_({"gstorage:list", "gstorage:history"}), F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_list(callback: CallbackQuery, db: Database, config: Config):
    if not await require_permission_callback(callback, "storage.view", db, config, "Этот раздел доступен участникам Полдня."):
        return
    topic = await db.get_storage_topic()
    if not await require_configured_topic(callback, topic, "Хранилище"):
        return
    status = "stored" if callback.data == "gstorage:list" else "issued"
    items = await db.list_storage_items(status=status, limit=30)
    if not items:
        await callback.answer("Здесь пока пусто.", show_alert=True)
        return
    title = "📋 <b>Сейчас на хранении</b>" if status == "stored" else "📜 <b>Последние выдачи</b>"
    await callback.message.answer(title + "\n\nНажмите на предмет, чтобы открыть карточку.", reply_markup=storage_items_keyboard(items))
    await callback.answer()


# ---------------------------------------------------------------------------
# Market GP flow entirely inside the configured forum topic
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "gmarket:new", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_new(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await require_permission_callback(callback, "market.create", db, config, "Заказы ГП доступны только участникам Полдня."):
        return
    topic = await db.get_market_topic()
    if not await require_configured_topic(callback, topic, "Рынок ГП"):
        return
    player = await db.get_player(callback.from_user.id)
    if not player:
        await callback.answer("Ваш ник ещё не найден. Сначала напишите его в теме «Ники игроков».", show_alert=True)
        return
    target = await db.get_market_merchant_target()
    if not target:
        await callback.answer("Администратор ещё не назначил Торговца ГП.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        flow_chat_id=topic[0],
        flow_thread_id=topic[1],
        market_items=[],
        market_comment=None,
    )
    await state.set_state(GroupMarketOrder.item_name)
    await callback.message.answer(
        f"🛒 Новый заказ от <b>{escape(player.game_nickname)}</b>\n\n🎒 Напишите название первой позиции:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")]]),
    )
    await callback.answer()


@router.message(GroupMarketOrder.item_name, F.chat.type.in_(GROUP_TYPES))
async def group_market_item_name(message: Message, state: FSMContext):
    if not await flow_matches_message(message, state):
        await message.answer("Продолжите оформление заказа в теме Рынок ГП.")
        return
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await message.answer("Название должно быть от 1 до 80 символов.")
        return
    await state.update_data(market_pending_name=name)
    await state.set_state(GroupMarketOrder.quantity)
    await safe_delete(message)
    await message.answer(
        f"🎒 <b>{escape(name)}</b>\n\n🔢 Выберите количество или введите своё:",
        reply_markup=group_quantity_keyboard("gmarket"),
    )


async def finish_market_quantity(message: Message, state: FSMContext, quantity: int) -> None:
    data = await state.get_data()
    items = list(data.get("market_items", []))
    items.append({"name": data["market_pending_name"], "quantity": quantity})
    await state.update_data(market_items=items)
    await state.set_state(None)
    await message.answer(cart_text(items, data.get("market_comment")), reply_markup=group_market_cart_keyboard(True))


@router.callback_query(GroupMarketOrder.quantity, F.data.startswith("gmarket:qty:"), F.message.chat.type.in_(GROUP_TYPES))
async def group_market_qty_button(callback: CallbackQuery, state: FSMContext):
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    value = callback.data.rsplit(":", 1)[1]
    if value == "custom":
        await callback.message.edit_text("🔢 Введите количество от 1 до 9999:")
        await callback.answer()
        return
    data = await state.get_data()
    items = list(data.get("market_items", []))
    items.append({"name": data["market_pending_name"], "quantity": int(value)})
    await state.update_data(market_items=items)
    await state.set_state(None)
    await callback.message.edit_text(cart_text(items, data.get("market_comment")), reply_markup=group_market_cart_keyboard(True))
    await callback.answer()


@router.message(GroupMarketOrder.quantity, F.chat.type.in_(GROUP_TYPES))
async def group_market_qty_text(message: Message, state: FSMContext):
    if not await flow_matches_message(message, state):
        await message.answer("Продолжите оформление заказа в теме Рынок ГП.")
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await message.answer("Введите целое количество от 1 до 9999.")
        return
    data = await state.get_data()
    items = list(data.get("market_items", []))
    items.append({"name": data["market_pending_name"], "quantity": int(raw)})
    await state.update_data(market_items=items)
    await state.set_state(None)
    await safe_delete(message)
    await message.answer(cart_text(items, data.get("market_comment")), reply_markup=group_market_cart_keyboard(True))


@router.callback_query(F.data == "gmarket:add_more", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_add_more(callback: CallbackQuery, state: FSMContext):
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия заказа устарела. Начните новый заказ.", show_alert=True)
        return
    await state.set_state(GroupMarketOrder.item_name)
    await callback.message.answer(
        "🎒 Напишите название следующей позиции:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")]]),
    )
    await callback.answer()


@router.callback_query(F.data == "gmarket:comment", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_comment_start(callback: CallbackQuery, state: FSMContext):
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия заказа устарела.", show_alert=True)
        return
    await state.set_state(GroupMarketOrder.comment)
    await callback.message.answer("📝 Напишите комментарий к заказу:", reply_markup=group_market_comment_keyboard())
    await callback.answer()


@router.message(GroupMarketOrder.comment, F.chat.type.in_(GROUP_TYPES))
async def group_market_comment_text(message: Message, state: FSMContext):
    if not await flow_matches_message(message, state):
        await message.answer("Продолжите оформление заказа в теме Рынок ГП.")
        return
    comment = (message.text or "").strip()
    if len(comment) > 500:
        await message.answer("Комментарий слишком длинный. Максимум 500 символов.")
        return
    await state.update_data(market_comment=comment or None)
    await state.set_state(None)
    data = await state.get_data()
    await safe_delete(message)
    await message.answer(cart_text(data.get("market_items", []), comment or None), reply_markup=group_market_cart_keyboard(True))


@router.callback_query(GroupMarketOrder.comment, F.data == "gmarket:comment_skip", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_comment_skip(callback: CallbackQuery, state: FSMContext):
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия заказа устарела.", show_alert=True)
        return
    await state.update_data(market_comment=None)
    await state.set_state(None)
    data = await state.get_data()
    await callback.message.edit_text(cart_text(data.get("market_items", []), None), reply_markup=group_market_cart_keyboard(True))
    await callback.answer()


@router.callback_query(F.data == "gmarket:submit", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_submit(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
    bot: Bot,
    telethon: TelethonManager,
    config: Config,
):
    if not await require_permission_callback(callback, "market.create", db, config, "Заказы ГП доступны только участникам Полдня."):
        return
    topic = await db.get_market_topic()
    if not await require_configured_topic(callback, topic, "Рынок ГП"):
        return
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия заказа устарела. Начните новый заказ.", show_alert=True)
        return
    data = await state.get_data()
    items_data = data.get("market_items", [])
    if not items_data:
        await callback.answer("В заказе нет позиций.", show_alert=True)
        return
    target = await db.get_market_merchant_target()
    if not target:
        await callback.answer("Торговец ГП не настроен.", show_alert=True)
        return
    player = await db.get_player(callback.from_user.id)
    if not player:
        await callback.answer("Профиль игрока не найден.", show_alert=True)
        return

    order_id = await db.create_market_order(
        requester_id=callback.from_user.id,
        items=[(str(x["name"]), int(x["quantity"])) for x in items_data],
        comment=data.get("market_comment"),
        merchant_target=target,
    )
    await db.set_market_order_topic_message(
        order_id,
        callback.message.chat.id,
        callback.message.message_thread_id,
        callback.message.message_id,
    )
    loaded = await db.get_market_order(order_id)
    if not loaded:
        await callback.answer("Не удалось сохранить заказ.", show_alert=True)
        return
    order, order_items = loaded
    await callback.answer("Заказ сформирован")

    # First make the group card authoritative. Even if private delivery fails,
    # the merchant can work with the order directly in this topic.
    await callback.message.edit_text(
        market_order_group_text(order, order_items),
        reply_markup=market_order_status_keyboard(order_id, order.workflow_status),
    )

    try:
        method, merchant_message_id = await send_merchant_notification(
            bot,
            telethon,
            target,
            market_order_group_text(order, order_items),
            market_order_status_keyboard(order_id, order.workflow_status),
        )
        await db.mark_market_order_sent(order_id, method)
        if merchant_message_id is not None:
            await db.set_market_order_merchant_message(order_id, merchant_message_id)
    except Exception:
        await db.mark_market_order_failed(order_id)

    await state.clear()
    loaded = await db.get_market_order(order_id)
    order, order_items = loaded
    await refresh_order_cards(bot, order, order_items)


@router.callback_query(F.data == "gmarket:mine", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_mine(callback: CallbackQuery, db: Database, config: Config):
    if not await require_permission_callback(callback, "market.create", db, config, "Заказы ГП доступны только участникам Полдня."):
        return
    topic = await db.get_market_topic()
    if not await require_configured_topic(callback, topic, "Рынок ГП"):
        return
    orders = await db.list_market_orders(requester_id=callback.from_user.id, limit=10)
    if not orders:
        await callback.answer("У вас пока нет заказов.", show_alert=True)
        return
    lines = ["<b>📋 Мои последние заказы</b>", ""]
    for order in orders:
        lines.append(f"{WORKFLOW_LABELS.get(order.workflow_status, '•')} <b>#{order.id}</b> — {fmt_dt(order.created_at)}")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ---------------------------------------------------------------------------
# Merchant assignment and order workflow
# ---------------------------------------------------------------------------

@router.message(Command("set_gp_merchant"), F.chat.type.in_(GROUP_TYPES))
async def set_gp_merchant_command(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.manage", db, config):
        await message.answer("Недостаточно прав.")
        return
    target = ""
    if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        target = str(message.reply_to_message.from_user.id)
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            target = parts[1].strip()
    if target.startswith("https://t.me/"):
        target = "@" + target.split("https://t.me/", 1)[1].strip("/").split("/", 1)[0]
    if not (re.fullmatch(r"-?\d+", target) or re.fullmatch(r"@[A-Za-z0-9_]{5,32}", target)):
        await message.answer(
            "Назначение Торговца ГП:\n"
            "• ответьте командой <code>/set_gp_merchant</code> на сообщение Торговца; или\n"
            "• <code>/set_gp_merchant @username</code>; или\n"
            "• <code>/set_gp_merchant 123456789</code>."
        )
        return
    await db.set_market_merchant_target(target)
    await message.answer(f"✅ Торговец ГП назначен: <code>{escape(target)}</code>")


@router.callback_query(F.data == "gadmin:market", F.message.chat.type.in_(GROUP_TYPES))
async def group_admin_market(callback: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not await has_permission(callback.from_user.id, "market.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    target = await db.get_market_merchant_target()
    topic = await db.get_market_topic()
    connected = await telethon.is_connected()
    await callback.message.edit_text(
        "<b>🛒 Настройки Рынка ГП</b>\n\n"
        f"Тема: {'✅ настроена' if topic else '⚠️ не настроена'}\n"
        f"Торговец: <code>{escape(target) if target else 'не назначен'}</code>\n"
        f"Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n\n"
        "Проще всего назначить Торговца: ответьте на его сообщение командой <code>/set_gp_merchant</code>.\n"
        "Для привязки темы отправьте <code>/set_market_topic</code> внутри «Рынок ГП».",
        reply_markup=group_market_admin_menu(bool(target)),
    )
    await callback.answer()


@router.callback_query(F.data == "gmarket_settings:merchant", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_merchant_start(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "market.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.update_data(flow_chat_id=callback.message.chat.id, flow_thread_id=callback.message.message_thread_id)
    await state.set_state(GroupMarketSettings.merchant_target)
    await callback.message.answer(
        "👤 Напишите <code>@username</code> или числовой Telegram ID Торговца ГП.\n\n"
        "Ещё удобнее: отмените это действие и ответьте командой <code>/set_gp_merchant</code> на любое сообщение самого Торговца.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")]]),
    )
    await callback.answer()


@router.message(GroupMarketSettings.merchant_target, F.chat.type.in_(GROUP_TYPES))
async def group_market_merchant_save(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await message.answer("Продолжите настройку в той же теме.")
        return
    raw = (message.text or "").strip()
    if raw.startswith("https://t.me/"):
        raw = "@" + raw.split("https://t.me/", 1)[1].strip("/").split("/", 1)[0]
    if not (re.fullmatch(r"-?\d+", raw) or re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw)):
        await message.answer("Введите @username или числовой Telegram ID.")
        return
    await db.set_market_merchant_target(raw)
    await state.clear()
    await safe_delete(message)
    await message.answer(f"✅ Торговец ГП назначен: <code>{escape(raw)}</code>")


@router.callback_query(F.data == "gmarket_settings:test", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_test(callback: CallbackQuery, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not await has_permission(callback.from_user.id, "market.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    target = await db.get_market_merchant_target()
    if not target:
        await callback.answer("Сначала назначьте Торговца ГП.", show_alert=True)
        return
    await callback.answer("Проверяю доставку…")
    try:
        method, _ = await send_merchant_notification(
            bot,
            telethon,
            target,
            "🧪 <b>Тест связи XZONA Group Bot</b>\n\nДоставка заказов Рынка ГП настроена.",
        )
        await callback.message.answer(f"✅ Тест доставлен через {'Telegram Bot' if method == 'bot' else 'Telethon'}.")
    except Exception as exc:
        await callback.message.answer(f"⚠️ Личная доставка не прошла: <code>{escape(str(exc)[:500])}</code>\nЗаказы всё равно будут видны в теме Рынок ГП.")


ALLOWED_TRANSITIONS = {
    "pending": {"accepted", "rejected"},
    "accepted": {"assembled", "rejected"},
    "assembled": {"issued"},
    "issued": set(),
    "rejected": set(),
}


async def refresh_order_cards(bot: Bot, order: DbMarketOrder, items: list[MarketOrderItem]) -> None:
    text = market_order_group_text(order, items)
    markup = market_order_status_keyboard(order.id, order.workflow_status)
    if order.topic_chat_id and order.topic_message_id:
        try:
            await bot.edit_message_text(
                chat_id=order.topic_chat_id,
                message_id=order.topic_message_id,
                text=text,
                reply_markup=markup,
            )
        except Exception:
            pass
    if order.merchant_message_id and order.merchant_target and order.merchant_target.lstrip("-").isdigit():
        try:
            await bot.edit_message_text(
                chat_id=int(order.merchant_target),
                message_id=order.merchant_message_id,
                text=text,
                reply_markup=markup,
            )
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^gorder:(accepted|assembled|issued|rejected):\d+$"))
async def group_order_status(callback: CallbackQuery, db: Database, config: Config, bot: Bot):
    _, new_status, raw_order_id = callback.data.split(":", 2)
    order_id = int(raw_order_id)
    loaded = await db.get_market_order(order_id)
    if not loaded:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    order, items = loaded
    if not await merchant_authorized(callback.from_user.id, callback.from_user.username, order.merchant_target, db, config):
        await callback.answer("Менять статус может Торговец ГП или администратор.", show_alert=True)
        return
    if new_status not in ALLOWED_TRANSITIONS.get(order.workflow_status, set()):
        await callback.answer("Этот переход статуса уже недоступен.", show_alert=True)
        return
    await db.set_market_workflow_status(order_id, new_status)
    loaded = await db.get_market_order(order_id)
    order, items = loaded
    await refresh_order_cards(bot, order, items)
    await callback.answer(WORKFLOW_LABELS.get(new_status, "Статус изменён"))


# ---------------------------------------------------------------------------
# Shared cancel for all group-first workflows
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "gflow:cancel", F.message.chat.type.in_(GROUP_TYPES))
async def group_flow_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Операция отменена.")
    except Exception:
        pass
    await callback.answer()


# Optional text shortcuts if admins/users still have an old reply keyboard.
@router.message(F.text == "📦 Хранилище", F.chat.type.in_(GROUP_TYPES))
async def group_storage_text_shortcut(message: Message, db: Database):
    if topic_tuple_from_message(message) != await db.get_storage_topic():
        return
    count, players = await db.storage_stats()
    await message.answer(
        f"<b>📦 Хранилище</b>\n\nНа хранении: <b>{count}</b>\nИгроков: <b>{players}</b>",
        reply_markup=group_storage_panel(),
    )


@router.message(F.text == "🛒 Рынок ГП", F.chat.type.in_(GROUP_TYPES))
async def group_market_text_shortcut(message: Message, db: Database):
    if topic_tuple_from_message(message) != await db.get_market_topic():
        return
    await message.answer("<b>🛒 Рынок ГП</b>", reply_markup=group_market_panel())

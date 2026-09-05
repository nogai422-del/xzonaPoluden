from __future__ import annotations

import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .db import Database, MarketOrder as DbMarketOrder, MarketOrderItem, RoleCapacityFullError, RoleRequest, StorageItem
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
    group_role_admin_keyboard,
    group_role_player_keyboard,
    group_role_players_keyboard,
    group_role_request_review_keyboard,
    group_role_requests_keyboard,
    group_role_search_results_keyboard,
    group_external_factions_keyboard,
    group_external_role_keyboard,
    group_storage_comment_keyboard,
    group_storage_confirm_keyboard,
    group_storage_panel,
    group_telethon_menu,
    item_keyboard,
    market_order_status_keyboard,
    storage_items_keyboard,
)
from .roles import FACTIONS, INTERNAL_POSITION_ORDER, POSITIONS, ROLE_CAPACITIES, has_position_permission, is_external_position, parse_position, position_display
from .states import GroupAddItem, GroupMarketOrder, GroupMarketSettings, GroupRoleAdmin
from .telethon_manager import TelethonManager
from .telethon_web import TelethonWebAuth
from .housekeeping import temp_answer, temp_callback_message, temp_bot_message, delete_incoming_later, schedule_delete, topic_answer

router = Router(name="group_first")
GROUP_TYPES = {"group", "supergroup"}
ADMIN_CHAT_TYPES = {"group", "supergroup", "private"}


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


async def available_internal_role_codes(db: Database, *, exclude_user_id: int | None = None) -> list[str]:
    result: list[str] = []
    for code in INTERNAL_POSITION_ORDER:
        if await db.position_slot_available(code, exclude_telegram_id=exclude_user_id):
            result.append(code)
    return result


def capacity_full_text(position_code: str) -> str:
    label = POSITIONS.get(position_code).label if position_code in POSITIONS else position_code
    capacity = ROLE_CAPACITIES.get(position_code)
    if capacity is None:
        return f"Должность «{label}» сейчас недоступна."
    if capacity == 1:
        return f"Должность «{label}» уже занята."
    return f"Все места должности «{label}» уже заняты ({capacity}/{capacity})."


def user_mention(user_id: int, label: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{escape(label)}</a>'


async def notify_role_result(
    bot: Bot, db: Database, *, user_id: int, nickname: str, label: str | None,
    actor_id: int | None = None, action: str = "assigned", ttl: int = 1800,
) -> None:
    """Notify in the Nicks topic even when Telegram forbids bot DMs.

    The topic notice is transient to avoid clutter, while a private copy is attempted
    when the player has previously opened the bot.
    """
    topic = await db.get_nicks_topic()
    if action == "assigned":
        text = f"✅ {user_mention(user_id, nickname)}, вам назначена должность: <b>{escape(label or '—')}</b>."
        private_text = f"✅ Вам назначена должность: <b>{escape(label or '—')}</b>."
    elif action == "approved":
        text = f"✅ {user_mention(user_id, nickname)}, Лидер/Заместитель подтвердил вашу должность: <b>{escape(label or '—')}</b>."
        private_text = f"✅ Ваша должность подтверждена: <b>{escape(label or '—')}</b>."
    elif action == "rejected":
        text = f"❌ {user_mention(user_id, nickname)}, заявка на должность <b>{escape(label or '—')}</b> отклонена руководством."
        private_text = f"❌ Запрос на должность <b>{escape(label or '—')}</b> отклонён руководством."
    else:
        text = f"ℹ️ {user_mention(user_id, nickname)}, ваша должность снята руководством."
        private_text = "ℹ️ Ваша должность снята руководством."
    if actor_id:
        text += f"\nРешение: {user_mention(actor_id, 'руководство')}."
    if topic:
        try:
            await temp_bot_message(bot, topic[0], text, ttl=ttl, message_thread_id=topic[1])
        except Exception:
            pass
    try:
        await bot.send_message(user_id, private_text)
    except Exception:
        pass


async def finalize_role_request_topic_card(bot: Bot, req: RoleRequest, text: str) -> None:
    if not req.notification_chat_id or not req.notification_message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=req.notification_chat_id,
            message_id=req.notification_message_id,
            text=text,
            reply_markup=None,
        )
        schedule_delete(bot, req.notification_chat_id, req.notification_message_id, 180)
    except Exception:
        pass


async def refresh_nicks_announcement(bot: Bot, db: Database) -> None:
    try:
        from .multitask_handlers import announce_topic
        await announce_topic(bot, db, "nicks", force=True)
    except Exception:
        pass


async def group_admin_text(db: Database, telethon: TelethonManager) -> str:
    """Render the unified admin dashboard for both private chat and the group.

    Keep this function dependency-light: the /admin command must remain usable even
    when some optional modules/topics are not configured yet.
    """
    total = await db.count_players()
    unassigned = await db.count_players_without_role()
    pending = await db.count_pending_role_requests()
    stored_count, stored_players = await db.storage_stats()
    topics = await db.list_topics()
    connected = await telethon.is_connected()
    merchant = await db.get_market_merchant_target()

    return (
        "<b>⚙️ УПРАВЛЕНИЕ ГРУППИРОВКОЙ</b>\n\n"
        f"👥 Игроков: <b>{total}</b>\n"
        f"⚠️ Без должности: <b>{unassigned}</b>\n"
        f"⏳ Заявок на должность: <b>{pending}</b>\n"
        f"🎒 На хранении: <b>{stored_count}</b> поз. / <b>{stored_players}</b> игроков\n"
        f"🧩 Разделов привязано: <b>{len(topics)}/12</b>\n"
        f"🪙 Торговец ГП: <b>{escape(merchant) if merchant else 'не назначен'}</b>\n"
        f"🔐 Telethon: <b>{'🟢 подключён' if connected else '🔴 не подключён'}</b>\n\n"
        "Выберите раздел управления ниже."
    )


async def role_admin_summary(db: Database) -> tuple[str, int, int]:
    total = await db.count_players()
    unassigned = await db.count_players_without_role()
    pending = await db.count_pending_role_requests()
    counts = await db.position_counts()
    leader_used = counts.get("leader", 0)
    deputy_used = counts.get("deputy_leader", 0)
    text = (
        "<b>🎖 РОЛИ И ДОСТУП</b>\n\n"
        f"👥 Игроков: <b>{total}</b>\n"
        f"⚠️ Без подтверждённой должности: <b>{unassigned}</b>\n"
        f"⏳ Заявок: <b>{pending}</b>\n\n"
        "<b>Ограниченные должности:</b>\n"
        f"👑 Лидер: <b>{leader_used}/1</b>\n"
        f"⭐ Заместитель лидера: <b>{deputy_used}/5</b>\n\n"
        "Остальные внутренние должности не ограничены по количеству. "
        "Для старых ников удобнее открыть «Без должности» и назначать роли кнопками."
    )
    return text, unassigned, pending


async def render_role_player_message(message, user_id: int, db: Database) -> bool:
    player = await db.get_player(user_id)
    if not player:
        return False
    pending = await db.get_pending_role_request_for_user(user_id)
    current = position_display(player.position_code, player.faction_code) if player.position_code and player.position_status == "approved" else "Не назначена"
    tg = f"@{escape(player.username)}" if player.username else f"<code>{player.telegram_id}</code>"
    text = (
        f"<b>👤 {escape(player.game_nickname)}</b>\n\n"
        f"Telegram: {tg}\n"
        f"Текущая должность: <b>{escape(current)}</b>\n"
        f"Заявка: <b>{escape(pending.requested_label) if pending else '—'}</b>\n\n"
        "Выберите новую должность кнопкой ниже. Ограниченные должности автоматически скрываются, когда свободных мест нет."
    )
    available = await available_internal_role_codes(db, exclude_user_id=user_id)
    await message.edit_text(
        text,
        reply_markup=group_role_player_keyboard(
            user_id,
            available_internal_codes=available,
            current_position_code=player.position_code if player.position_status == "approved" else None,
            has_role=bool(player.position_code and player.position_status == "approved"),
        ),
    )
    return True


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


async def flow_edit_from_message(
    message: Message, state: FSMContext, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """Keep one bot workflow message per user flow instead of stacking prompts."""
    data = await state.get_data()
    flow_message_id = data.get("flow_message_id")
    if flow_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(flow_message_id),
                text=text,
                reply_markup=reply_markup,
            )
            return None
        except Exception:
            pass
    sent = await topic_answer(message, text, reply_markup=reply_markup)
    await state.update_data(flow_message_id=sent.message_id)
    return sent


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
        await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    thread_id = int(message.message_thread_id or 0)
    from .multitask_handlers import remove_old_topic_panel
    await remove_old_topic_panel(message.bot, db, "general", message.chat.id, thread_id)
    await db.set_general_topic(message.chat.id, thread_id)
    await db.set_topic("general", message.chat.id, thread_id)
    await db.set_setting("primary_chat_id", str(message.chat.id))
    await db.audit(message.from_user.id, "topic.set", f"general={message.chat.id}/{thread_id}")
    from .multitask_handlers import announce_topic
    await announce_topic(message.bot, db, "general", force=True)
    await temp_answer(message, "✅ General привязан. Постоянная инструкция обновлена.", ttl=45)
    await delete_incoming_later(message)

@router.message(Command("set_storage_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_storage_topic_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    topic = topic_tuple_from_message(message)
    if not topic:
        await temp_answer(message, "Отправьте /set_storage_topic прямо внутри темы «Снаряжение группировки».", ttl=90)
        return
    from .multitask_handlers import remove_old_topic_panel
    await remove_old_topic_panel(message.bot, db, "storage", topic[0], topic[1])
    await db.set_storage_topic(*topic)
    await db.set_topic("storage", *topic)
    await db.set_setting("primary_chat_id", str(message.chat.id))
    await db.audit(message.from_user.id, "topic.set", f"storage={topic[0]}/{topic[1]}")
    from .multitask_handlers import announce_topic
    await announce_topic(message.bot, db, "storage", force=True)
    await temp_answer(message, "✅ Раздел снаряжения привязан. Панель обновлена без дублей.", ttl=45)
    await delete_incoming_later(message)

@router.message(Command("storage_panel"), F.chat.type.in_(GROUP_TYPES))
async def storage_panel_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.view", db, config):
        return
    configured = await db.get_storage_topic()
    if topic_tuple_from_message(message) != configured:
        await temp_answer(message, "Эта команда должна быть отправлена в настроенной теме Хранилища.", ttl=60)
        return
    from .multitask_handlers import announce_topic
    await announce_topic(message.bot, db, "storage", force=True)
    await temp_answer(message, "✅ Панель хранилища обновлена выше.", ttl=30)
    await delete_incoming_later(message)

@router.message(Command("set_market_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_market_topic_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.manage", db, config):
        await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    topic = topic_tuple_from_message(message)
    if not topic:
        await temp_answer(message, "Отправьте /set_market_topic прямо внутри темы «Рынок ГП».", ttl=90)
        return
    from .multitask_handlers import remove_old_topic_panel
    if topic == await db.get_topic('trader'):
        await temp_answer(message, 'Рынок ГП и Торговец Локи должны быть в разных темах.', ttl=60)
        return
    await remove_old_topic_panel(message.bot, db, "market", topic[0], topic[1])
    await db.set_market_topic(*topic)
    await db.set_topic("market", *topic)
    await db.set_setting("primary_chat_id", str(message.chat.id))
    await db.audit(message.from_user.id, "topic.set", f"market={topic[0]}/{topic[1]}")
    from .multitask_handlers import announce_topic
    await announce_topic(message.bot, db, "market", force=True)
    await temp_answer(message, "✅ Рынок ГП привязан. Панель обновлена без дублей.", ttl=45)
    await delete_incoming_later(message)

@router.message(Command("market_panel"), F.chat.type.in_(GROUP_TYPES))
async def market_panel_group(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.create", db, config):
        return
    configured = await db.get_market_topic()
    if topic_tuple_from_message(message) != configured:
        await temp_answer(message, "Эта команда должна быть отправлена в настроенной теме Рынка ГП.", ttl=60)
        return
    from .multitask_handlers import announce_topic
    await announce_topic(message.bot, db, "market", force=True)
    await temp_answer(message, "✅ Панель Рынка ГП обновлена выше.", ttl=30)
    await delete_incoming_later(message)

@router.message(Command("set_nicks_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_nicks_topic_group(message: Message, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    topic = topic_tuple_from_message(message)
    if not topic:
        await temp_answer(message, "Отправьте /set_nicks_topic прямо внутри темы «Ники игроков».", ttl=90)
        return
    from .multitask_handlers import remove_old_topic_panel
    await remove_old_topic_panel(message.bot, db, "nicks", topic[0], topic[1])
    await db.set_nicks_topic(*topic)
    await db.set_topic("nicks", *topic)
    await db.set_setting("primary_chat_id", str(message.chat.id))
    await db.audit(message.from_user.id, "topic.set", f"nicks={topic[0]}/{topic[1]}")
    from .multitask_handlers import announce_topic
    await announce_topic(message.bot, db, "nicks", force=True)
    connected = await telethon.is_connected()
    extra = " Старую историю можно импортировать через /admin." if connected else " Для старой истории подключите Telethon через /admin."
    await temp_answer(message, "✅ Реестр ников привязан." + extra, ttl=60)
    await delete_incoming_later(message)

@router.message(Command("nicks_status"), F.chat.type.in_(GROUP_TYPES))
async def nicks_status_group(message: Message, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        return
    topic = await db.get_nicks_topic()
    imported_at, imported_count = await db.get_nicks_history_import_status()
    connected = await telethon.is_connected()
    count = await db.count_players()
    pending_roles = await db.count_pending_role_requests()
    text = (
        "<b>👥 Ники игроков</b>\n\n"
        f"Тема: {'✅ настроена' if topic else '⚠️ не настроена'}\n"
        f"Игроков в базе: <b>{count}</b>\n"
        f"Ожидают подтверждения роли: <b>{pending_roles}</b>\n"
        f"Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n"
        f"Старая история: {'✅ ' + str(imported_count) + ' записей, ' + escape(imported_at) if imported_at else '⚠️ ещё не импортирована'}"
    )
    await temp_answer(message, text, ttl=120)
    await delete_incoming_later(message)

@router.message(Command("set_role"), F.chat.type.in_(GROUP_TYPES))
async def set_role_command(message: Message, db: Database, config: Config, bot: Bot):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    source = message.reply_to_message
    if not source or not source.from_user or source.from_user.is_bot:
        await temp_answer(
            message,
            "Ответьте этой командой на сообщение участника:\n"
            "<code>/set_role Кладовщик</code>\n"
            "или <code>/set_role Лидер Долга</code>.",
            ttl=60,
        )
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await temp_answer(message, "Укажите должность после команды: <code>/set_role Рядовой</code>.", ttl=60)
        return
    parsed = parse_position(parts[1])
    if not parsed:
        await temp_answer(message, "Неизвестная должность. Проверьте написание.", ttl=60)
        return
    position_code, faction_code, label = parsed
    player = await db.get_player(source.from_user.id)
    if not player:
        await temp_answer(message, "Игрок ещё не найден в реестре «Ники игроков».", ttl=60)
        return
    try:
        changed = await db.set_player_role(source.from_user.id, position_code, faction_code, message.from_user.id)
    except RoleCapacityFullError:
        await temp_answer(message, f"⛔ {escape(capacity_full_text(position_code))}", ttl=60)
        await delete_incoming_later(message)
        return
    if not changed:
        await temp_answer(message, "Игрок не найден в базе.", ttl=60)
        return
    await db.audit(message.from_user.id, "role.set", f"user={source.from_user.id} role={position_code} faction={faction_code or '-'}")
    await refresh_nicks_announcement(bot, db)
    await temp_answer(message, f"✅ <b>{escape(player.game_nickname)}</b> назначен: <b>{escape(label)}</b>.", ttl=60)
    await notify_role_result(
        bot, db, user_id=source.from_user.id, nickname=player.game_nickname, label=label,
        actor_id=message.from_user.id, action="assigned"
    )
    await delete_incoming_later(message)


@router.message(Command("clear_role"), F.chat.type.in_(GROUP_TYPES))
async def clear_role_command(message: Message, db: Database, config: Config, bot: Bot):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    source = message.reply_to_message
    if not source or not source.from_user or source.from_user.is_bot:
        await temp_answer(message, "Ответьте <code>/clear_role</code> на сообщение участника.", ttl=60)
        return
    player = await db.get_player(source.from_user.id)
    if not player:
        await temp_answer(message, "Игрок не найден в базе.", ttl=60)
        return
    await db.clear_player_role(source.from_user.id, message.from_user.id)
    await db.audit(message.from_user.id, "role.clear", f"user={source.from_user.id}")
    await refresh_nicks_announcement(message.bot, db)
    await temp_answer(message, f"✅ Должность <b>{escape(player.game_nickname)}</b> снята.", ttl=60)
    await notify_role_result(
        bot, db, user_id=source.from_user.id, nickname=player.game_nickname, label=None,
        actor_id=message.from_user.id, action="cleared"
    )
    await delete_incoming_later(message)


@router.message(Command("settings"), F.chat.type == "private")
@router.message(Command("admin"), F.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_admin(message: Message, db: Database, config: Config, telethon: TelethonManager):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        if message.chat.type == "private":
            await topic_answer(message, "Недостаточно прав. Проверьте OWNER_ID/ADMIN_IDS и команду /myid.")
        else:
            await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    if message.chat.type in GROUP_TYPES:
        await db.set_setting("primary_chat_id", str(message.chat.id))
        await temp_answer(message, await group_admin_text(db, telethon), reply_markup=group_admin_menu(), ttl=900)
        await delete_incoming_later(message)
    else:
        # In private chat the settings panel is intentionally persistent: it is the
        # owner's control console and should not disappear after the cleanup TTL.
        await topic_answer(message, await group_admin_text(db, telethon), reply_markup=group_admin_menu())


@router.callback_query(F.data == "gadmin:home", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_admin_home(callback: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.message.edit_text(await group_admin_text(db, telethon), reply_markup=group_admin_menu())
    await callback.answer()


@router.callback_query(F.data == "gadmin:storage", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_admin_storage(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    topic = await db.get_storage_topic()
    count, players = await db.storage_stats()
    text = (
        "<b>🎒 Снаряжение группировки</b>\n\n"
        f"Тема: {'✅ настроена' if topic else '⚠️ не настроена'}\n"
        f"На хранении: <b>{count}</b>\nИгроков с имуществом: <b>{players}</b>\n\n"
        "Для привязки откройте тему «Снаряжение группировки» и отправьте <code>/set_storage_topic</code>."
    )
    await callback.message.edit_text(text, reply_markup=group_admin_back())
    await callback.answer()


@router.callback_query(F.data == "gadmin:nicks", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
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


@router.callback_query(F.data == "gadmin:roles", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_admin_roles(callback: CallbackQuery, db: Database, config: Config, state: FSMContext):
    await state.clear()
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    text, unassigned, pending = await role_admin_summary(db)
    await callback.message.edit_text(
        text,
        reply_markup=group_role_admin_keyboard(unassigned_count=unassigned, pending_count=pending),
    )
    await callback.answer()


@router.callback_query(F.data == "grole:requests", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_requests(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    requests = await db.list_pending_role_requests(limit=20)
    pending_total = await db.count_pending_role_requests()
    text = f"<b>⏳ Заявки на должности</b>\n\nОжидают подтверждения: <b>{pending_total}</b>."
    if not requests:
        text += "\n\n✅ Новых запросов нет."
    await callback.message.edit_text(text, reply_markup=group_role_requests_keyboard(requests))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^grole:(unassigned|all):\d+$"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_players_list(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, mode, raw_page = callback.data.split(":", 2)
    page = max(0, int(raw_page))
    page_size = 10
    if mode == "unassigned":
        total = await db.count_players_without_role()
        players = await db.list_players_without_role(page_size, page * page_size)
        title = "👤 Игроки без должности"
    else:
        total = await db.count_players()
        players = await db.list_players(page_size, page * page_size)
        title = "👥 Все игроки"
    pages = max(1, (total + page_size - 1) // page_size)
    text = f"<b>{title}</b>\n\nВсего: <b>{total}</b>\nСтраница: <b>{min(page + 1, pages)}/{pages}</b>"
    if not players:
        text += "\n\n✅ Список пуст."
    await callback.message.edit_text(text, reply_markup=group_role_players_keyboard(players, page=page, total=total, mode=mode, page_size=page_size))
    await callback.answer()


@router.callback_query(F.data.startswith("grole:player:"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_player_view(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    if not await render_role_player_message(callback.message, user_id, db):
        await callback.answer("Игрок не найден", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.regexp(r"^grole:set:\d+:[a-z_]+$"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_set_direct(callback: CallbackQuery, db: Database, config: Config, bot: Bot):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, _, raw_user_id, position_code = callback.data.split(":", 3)
    user_id = int(raw_user_id)
    if position_code not in INTERNAL_POSITION_ORDER:
        await callback.answer("Неизвестная должность", show_alert=True)
        return
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("Игрок не найден", show_alert=True)
        return
    try:
        await db.set_player_role(user_id, position_code, None, callback.from_user.id)
    except RoleCapacityFullError:
        await callback.answer(capacity_full_text(position_code), show_alert=True)
        return
    label = position_display(position_code)
    await db.audit(callback.from_user.id, "role.set", f"user={user_id} role={position_code}")
    await refresh_nicks_announcement(bot, db)
    await notify_role_result(
        bot, db, user_id=user_id, nickname=player.game_nickname, label=label,
        actor_id=callback.from_user.id, action="assigned"
    )
    await render_role_player_message(callback.message, user_id, db)
    await callback.answer(f"Назначено: {label}")


@router.callback_query(F.data.startswith("grole:clear:"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_clear_direct(callback: CallbackQuery, db: Database, config: Config, bot: Bot):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("Игрок не найден", show_alert=True)
        return
    await db.clear_player_role(user_id, callback.from_user.id)
    await db.audit(callback.from_user.id, "role.clear", f"user={user_id}")
    await refresh_nicks_announcement(bot, db)
    await notify_role_result(
        bot, db, user_id=user_id, nickname=player.game_nickname, label=None,
        actor_id=callback.from_user.id, action="cleared"
    )
    await render_role_player_message(callback.message, user_id, db)
    await callback.answer("Должность снята")


@router.callback_query(F.data.startswith("grole:ext:"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_external_factions(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("Игрок не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>🌐 Внешняя группировка</b>\n\nИгрок: <b>{escape(player.game_nickname)}</b>\nВыберите группировку:",
        reply_markup=group_external_factions_keyboard(user_id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^grole:extf:\d+:[a-z_]+$"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_external_choose(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, _, raw_user_id, faction_code = callback.data.split(":", 3)
    user_id = int(raw_user_id)
    if faction_code not in FACTIONS:
        await callback.answer("Группировка не найдена", show_alert=True)
        return
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("Игрок не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>🌐 {escape(FACTIONS[faction_code])}</b>\n\nИгрок: <b>{escape(player.game_nickname)}</b>\nВыберите должность:",
        reply_markup=group_external_role_keyboard(user_id, faction_code),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^grole:setext:\d+:external_(leader|deputy):[a-z_]+$"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_external_set(callback: CallbackQuery, db: Database, config: Config, bot: Bot):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, _, raw_user_id, position_code, faction_code = callback.data.split(":", 4)
    user_id = int(raw_user_id)
    if position_code not in {"external_leader", "external_deputy"} or faction_code not in FACTIONS:
        await callback.answer("Некорректная роль", show_alert=True)
        return
    player = await db.get_player(user_id)
    if not player:
        await callback.answer("Игрок не найден", show_alert=True)
        return
    await db.set_player_role(user_id, position_code, faction_code, callback.from_user.id)
    label = position_display(position_code, faction_code)
    await db.audit(callback.from_user.id, "role.set", f"user={user_id} role={position_code} faction={faction_code}")
    await refresh_nicks_announcement(bot, db)
    await notify_role_result(
        bot, db, user_id=user_id, nickname=player.game_nickname, label=label,
        actor_id=callback.from_user.id, action="assigned"
    )
    await render_role_player_message(callback.message, user_id, db)
    await callback.answer(f"Назначено: {label}")


@router.callback_query(F.data == "grole:search", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_search_start(callback: CallbackQuery, db: Database, config: Config, state: FSMContext):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(GroupRoleAdmin.search)
    await callback.message.edit_text(
        "<b>🔎 Поиск игрока</b>\n\nНапишите ник, @username, имя или Telegram ID. Ваш поисковый запрос будет удалён после обработки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gadmin:roles")]]),
    )
    await callback.answer()


@router.message(GroupRoleAdmin.search, F.chat.type.in_(ADMIN_CHAT_TYPES), ~F.text.startswith("/"))
async def group_role_search_input(message: Message, db: Database, config: Config, state: FSMContext):
    if not message.from_user or not await can_manage_roles(message.from_user.id, db, config):
        await state.clear()
        return
    query = (message.text or "").strip().lstrip("@")
    if len(query) < 1:
        await temp_answer(message, "Введите хотя бы один символ.", ttl=30)
        return
    players = await db.search_players(query, limit=20)
    await state.clear()
    await delete_incoming_later(message)
    text = f"<b>🔎 Результаты поиска</b>\n\nЗапрос: <b>{escape(query)}</b>\nНайдено: <b>{len(players)}</b>"
    if not players:
        text += "\n\nНичего не найдено."
    await temp_answer(message, text, reply_markup=group_role_search_results_keyboard(players), ttl=900)


@router.callback_query(F.data.startswith("grole:view:"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
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


@router.callback_query(F.data.regexp(r"^grole:(approve|reject):\d+$"), F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_role_request_review(callback: CallbackQuery, db: Database, config: Config, bot: Bot):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, action, raw_id = callback.data.split(":", 2)
    try:
        req = await db.review_role_request(int(raw_id), callback.from_user.id, action == "approve")
    except RoleCapacityFullError as exc:
        await callback.answer(capacity_full_text(exc.position_code), show_alert=True)
        return
    if not req:
        await callback.answer("Запрос уже обработан или не найден", show_alert=True)
        return
    if action == "approve":
        result = f"✅ Подтверждено: <b>{escape(req.player_nickname)}</b> — <b>{escape(req.requested_label)}</b>."
        await notify_role_result(
            bot, db, user_id=req.telegram_id, nickname=req.player_nickname, label=req.requested_label,
            actor_id=callback.from_user.id, action="approved"
        )
    else:
        result = f"❌ Запрос <b>{escape(req.player_nickname)}</b> на должность <b>{escape(req.requested_label)}</b> отклонён."
        await notify_role_result(
            bot, db, user_id=req.telegram_id, nickname=req.player_nickname, label=req.requested_label,
            actor_id=callback.from_user.id, action="rejected"
        )
    is_topic_request_card = bool(
        req.notification_chat_id
        and req.notification_message_id
        and callback.message.chat.id == req.notification_chat_id
        and callback.message.message_id == req.notification_message_id
    )
    await finalize_role_request_topic_card(bot, req, result)
    if action == "approve":
        await db.audit(callback.from_user.id, "role.approve", f"request={req.id} user={req.telegram_id} role={req.requested_position_code}")
        await refresh_nicks_announcement(bot, db)
    else:
        await db.audit(callback.from_user.id, "role.reject", f"request={req.id} user={req.telegram_id} role={req.requested_position_code}")
    if not is_topic_request_card:
        requests = await db.list_pending_role_requests(limit=20)
        await callback.message.edit_text(result, reply_markup=group_role_requests_keyboard(requests))
    await callback.answer("Готово")


async def _telethon_admin_view(db: Database, config: Config, bot: Bot, telethon: TelethonManager, user_id: int):
    connected = await telethon.is_connected()
    nicks_topic = await db.get_nicks_topic()
    primary_chat_id = await db.get_primary_chat_id()
    me = await bot.get_me()
    lines = [
        "<b>🔐 Telethon</b>",
        "",
        f"Статус: {'🟢 подключён' if connected else '🔴 не подключён'}",
        f"Аккаунт: <code>{escape(telethon.masked_phone())}</code>",
        "",
    ]
    if primary_chat_id is not None:
        stats = await db.group_members_stats(primary_chat_id)
        lines += [
            f"👥 Основная группа: <code>{primary_chat_id}</code>",
            f"Участников сейчас: <b>{stats['active']}</b>",
            f"Из них зарегистрировали ник: <b>{stats['registered']}</b>",
            f"Последняя синхронизация: <code>{escape(str(stats['last_sync'] or 'ещё не выполнялась'))}</code>",
        ]
    else:
        lines += [
            "👥 Основная группа: <b>не выбрана</b>",
            "Сначала вручную привяжите любой раздел этой группы, например <code>/set_general_topic</code>.",
        ]
    interval = int(getattr(config, "telethon_member_sync_interval", 0) or 0)
    lines += [
        "",
        "Telethon используется для синхронизации состава группы и импорта старых ников.",
        (f"Автосинхронизация участников: каждые <b>{max(1, interval // 60)}</b> мин." if interval > 0 else "Автосинхронизация участников: <b>выключена</b>."),
        "",
        "Разделы бот больше не определяет автоматически — их назначает руководство вручную.",
        "API HASH, код входа и 2FA вводятся только в отдельном браузерном окне.",
    ]
    if not connected and telethon.last_error:
        lines += ["", f"⚠️ {escape(telethon.last_error[:300])}"]
    markup = group_telethon_menu(
        connected=connected,
        bot_username=me.username,
        can_manage=can_manage_telethon(user_id, config),
        can_sync=nicks_topic is not None,
        can_sync_members=primary_chat_id is not None,
    )
    return "\n".join(lines), markup


@router.callback_query(F.data == "gadmin:telethon", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_admin_telethon(callback: CallbackQuery, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not await can_manage_roles(callback.from_user.id, db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    text, markup = await _telethon_admin_view(db, config, bot, telethon, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "gtelethon:sync_members", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_telethon_sync_members(callback: CallbackQuery, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not await can_manage_roles(callback.from_user.id, db, config):
        return await callback.answer("Недостаточно прав", show_alert=True)
    if not await telethon.is_connected():
        return await callback.answer("Сначала подключите Telethon.", show_alert=True)
    chat_id = await db.get_primary_chat_id()
    if chat_id is None:
        return await callback.answer("Сначала вручную привяжите хотя бы один раздел группы.", show_alert=True)
    await callback.answer("Синхронизирую участников…")
    try:
        result = await telethon.sync_group_members(chat_id)
    except Exception as exc:
        text, markup = await _telethon_admin_view(db, config, bot, telethon, callback.from_user.id)
        text += f"\n\n⚠️ Ошибка синхронизации: <code>{escape(str(exc)[:500])}</code>"
        await callback.message.edit_text(text, reply_markup=markup)
        return
    await db.audit(
        callback.from_user.id,
        "telethon.members_sync",
        f"chat={result.chat_id} active={result.active} added={result.added} updated={result.updated} left={result.left}",
    )
    text, markup = await _telethon_admin_view(db, config, bot, telethon, callback.from_user.id)
    text += (
        "\n\n✅ <b>Синхронизация завершена</b>\n"
        f"Сейчас в группе: <b>{result.active}</b>\n"
        f"Новых: <b>{result.added}</b> · обновлено: <b>{result.updated}</b> · вышли: <b>{result.left}</b>"
    )
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "gtelethon:members", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_telethon_members(callback: CallbackQuery, db: Database, config: Config):
    if not await can_manage_roles(callback.from_user.id, db, config):
        return await callback.answer("Недостаточно прав", show_alert=True)
    chat_id = await db.get_primary_chat_id()
    if chat_id is None:
        return await callback.answer("Сначала вручную привяжите хотя бы один раздел группы.", show_alert=True)
    rows = await db.list_group_members(chat_id, status="active", limit=60)
    if not rows:
        return await callback.answer("Состав ещё не синхронизирован.", show_alert=True)
    lines = ["<b>👥 Состав группы по Telethon</b>", ""]
    for row in rows:
        username = f"@{escape(row['username'])}" if row.get("username") else f"<code>{row['telegram_id']}</code>"
        if row.get("game_nickname"):
            lines.append(f"✅ <b>{escape(row['game_nickname'])}</b> — {username}")
        else:
            lines.append(f"⚠️ {escape(row.get('full_name') or str(row['telegram_id']))} — {username} — ник не зарегистрирован")
    if len(rows) >= 60:
        lines.append("\n<i>Показаны первые 60 участников.</i>")
    await temp_callback_message(callback, "\n".join(lines), ttl=max(120, config.temp_message_ttl))
    await callback.answer()


@router.callback_query(F.data == "gtelethon:web_auth", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_telethon_web_auth(
    callback: CallbackQuery,
    config: Config,
    bot: Bot,
    telethon: TelethonManager,
    telethon_web: TelethonWebAuth,
):
    if not can_manage_telethon(callback.from_user.id, config):
        await callback.answer("Окно авторизации доступно только владельцу.", show_alert=True)
        return
    if await telethon.is_connected():
        await callback.answer("Telethon уже подключён.", show_alert=True)
        return
    url = telethon_web.create_login_url(callback.from_user.id)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🪟 Открыть авторизацию Telethon", url=url)]]
    )
    try:
        await bot.send_message(
            callback.from_user.id,
            "🔐 <b>Одноразовое окно авторизации Telethon</b>\n\n"
            "Ссылка действует ограниченное время. Введите API ID и API HASH, затем выберите вход по QR-коду "
            "или по телефону. Если включён облачный пароль, появится форма 2FA.\n\n"
            "После успешного входа вернитесь в админ-панель. Там можно синхронизировать участников и отдельно импортировать старые ники.",
            reply_markup=markup,
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        await callback.answer(
            "Не могу отправить защищённую ссылку. Один раз откройте личный чат с ботом и нажмите /start, затем повторите.",
            show_alert=True,
        )
        return
    await callback.answer("Одноразовая ссылка отправлена вам в личный чат.", show_alert=True)


@router.callback_query(F.data == "gtelethon:disconnect", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_telethon_disconnect(callback: CallbackQuery, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not can_manage_telethon(callback.from_user.id, config):
        await callback.answer("Только владелец может отключить Telethon.", show_alert=True)
        return
    await telethon.disconnect(clear_saved=True)
    text, markup = await _telethon_admin_view(db, config, bot, telethon, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer("Telethon отключён")


# ---------------------------------------------------------------------------
# Storage flow entirely inside the configured forum topic
# ---------------------------------------------------------------------------

async def show_group_players(callback: CallbackQuery, db: Database, state: FSMContext, page: int, *, edit: bool) -> None:
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
        sent = await topic_answer(callback.message, text, reply_markup=markup)
        await state.update_data(flow_message_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data == "gstorage:add", F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_add(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await require_permission_callback(callback, "storage.manage", db, config, "Принимать предметы может Кладовщик, Лидер или Заместитель."):
        return
    topic = await db.get_storage_topic()
    if not await require_configured_topic(callback, topic, "Снаряжение группировки"):
        return
    await state.clear()
    await state.update_data(flow_chat_id=topic[0], flow_thread_id=topic[1])
    await show_group_players(callback, db, state, 0, edit=False)


@router.callback_query(F.data.startswith("gstorage:players_page:"), F.message.chat.type.in_(GROUP_TYPES))
async def group_storage_players_page(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await require_permission_callback(callback, "storage.manage", db, config):
        return
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия устарела. Начните приём предмета заново.", show_alert=True)
        return
    page = int(callback.data.rsplit(":", 1)[1])
    await show_group_players(callback, db, state, page, edit=True)


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


@router.message(GroupAddItem.name, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def group_storage_name(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await temp_answer(message, "Продолжите добавление предмета в теме «Снаряжение группировки».", ttl=60)
        return
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await temp_answer(message, "Название должно быть от 1 до 80 символов.", ttl=60)
        return
    await state.update_data(item_name=name)
    await state.set_state(GroupAddItem.quantity)
    await safe_delete(message)
    await flow_edit_from_message(
        message, state,
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


@router.message(GroupAddItem.quantity, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def group_storage_qty_text(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await temp_answer(message, "Продолжите добавление предмета в теме «Снаряжение группировки».", ttl=60)
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await temp_answer(message, "Введите целое количество от 1 до 9999.", ttl=60)
        return
    await state.update_data(quantity=int(raw))
    await state.set_state(GroupAddItem.comment)
    await safe_delete(message)
    await flow_edit_from_message(message, state, "📝 Добавьте комментарий или пропустите:", reply_markup=group_storage_comment_keyboard())


async def show_group_storage_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await flow_edit_from_message(
        message, state,
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


@router.message(GroupAddItem.comment, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def group_storage_comment(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "storage.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await temp_answer(message, "Продолжите добавление предмета в теме «Снаряжение группировки».", ttl=60)
        return
    comment = (message.text or "").strip()
    if len(comment) > 500:
        await temp_answer(message, "Комментарий слишком длинный. Максимум 500 символов.", ttl=60)
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
    if not await require_configured_topic(callback, topic, "Снаряжение группировки"):
        return
    status = "stored" if callback.data == "gstorage:list" else "issued"
    items = await db.list_storage_items(status=status, limit=30)
    if not items:
        await callback.answer("Здесь пока пусто.", show_alert=True)
        return
    title = "📋 <b>Сейчас на хранении</b>" if status == "stored" else "📜 <b>Последние выдачи</b>"
    await temp_callback_message(callback, title + "\n\nНажмите на предмет, чтобы открыть карточку.", reply_markup=storage_items_keyboard(items), ttl=config.temp_message_ttl)
    await callback.answer()


# ---------------------------------------------------------------------------
# Market GP flow entirely inside the configured forum topic
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "gmarket:new", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_new(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await require_permission_callback(callback, "market.create", db, config, "Заказы ГП доступны только участникам Полдня."):
        return
    topic = await db.get_topic('trader')
    if not await require_configured_topic(callback, topic, "Торговец Локи"):
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
    sent = await topic_answer(callback.message, 
        f"🛒 Новый заказ от <b>{escape(player.game_nickname)}</b>\n\n🎒 Напишите название первой позиции:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")]]),
    )
    await state.update_data(flow_message_id=sent.message_id)
    await callback.answer()


@router.message(GroupMarketOrder.item_name, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def group_market_item_name(message: Message, state: FSMContext, db: Database):
    if not await flow_matches_message(message, state):
        await temp_answer(message, "Продолжите оформление заказа в теме Торговец Локи.", ttl=60)
        return
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await temp_answer(message, "Название должно быть от 1 до 80 символов.", ttl=60)
        return
    name = (await db.catalogue_save(name))["name"]
    await state.update_data(market_pending_name=name)
    await state.set_state(GroupMarketOrder.quantity)
    await safe_delete(message)
    await flow_edit_from_message(
        message, state,
        f"🎒 <b>{escape(name)}</b>\n\n🔢 Выберите количество или введите своё:",
        reply_markup=group_quantity_keyboard("gmarket"),
    )


async def finish_market_quantity(message: Message, state: FSMContext, quantity: int) -> None:
    data = await state.get_data()
    items = list(data.get("market_items", []))
    items.append({"name": data["market_pending_name"], "quantity": quantity})
    await state.update_data(market_items=items)
    await state.set_state(None)
    await flow_edit_from_message(message, state, cart_text(items, data.get("market_comment")), reply_markup=group_market_cart_keyboard(True))


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


@router.message(GroupMarketOrder.quantity, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def group_market_qty_text(message: Message, state: FSMContext):
    if not await flow_matches_message(message, state):
        await temp_answer(message, "Продолжите оформление заказа в теме Торговец Локи.", ttl=60)
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await temp_answer(message, "Введите целое количество от 1 до 9999.", ttl=60)
        return
    data = await state.get_data()
    items = list(data.get("market_items", []))
    items.append({"name": data["market_pending_name"], "quantity": int(raw)})
    await state.update_data(market_items=items)
    await state.set_state(None)
    await safe_delete(message)
    await flow_edit_from_message(message, state, cart_text(items, data.get("market_comment")), reply_markup=group_market_cart_keyboard(True))


@router.callback_query(F.data == "gmarket:add_more", F.message.chat.type.in_(GROUP_TYPES))
async def group_market_add_more(callback: CallbackQuery, state: FSMContext):
    if not await flow_matches_callback(callback, state):
        await callback.answer("Сессия заказа устарела. Начните новый заказ.", show_alert=True)
        return
    if len((await state.get_data()).get('market_items', [])) >= 20:
        return await callback.answer('В одном заказе максимум 20 позиций.', show_alert=True)
    await state.set_state(GroupMarketOrder.item_name)
    await callback.message.edit_text(
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
    await callback.message.edit_text("📝 Напишите комментарий к заказу:", reply_markup=group_market_comment_keyboard())
    await callback.answer()


@router.message(GroupMarketOrder.comment, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def group_market_comment_text(message: Message, state: FSMContext):
    if not await flow_matches_message(message, state):
        await temp_answer(message, "Продолжите оформление заказа в теме Торговец Локи.", ttl=60)
        return
    comment = (message.text or "").strip()
    if len(comment) > 500:
        await temp_answer(message, "Комментарий слишком длинный. Максимум 500 символов.", ttl=60)
        return
    await state.update_data(market_comment=comment or None)
    await state.set_state(None)
    data = await state.get_data()
    await safe_delete(message)
    await flow_edit_from_message(message, state, cart_text(data.get("market_items", []), comment or None), reply_markup=group_market_cart_keyboard(True))


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
    topic = await db.get_topic('trader')
    if not await require_configured_topic(callback, topic, "Торговец Локи"):
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
    await state.clear()
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
    topic = await db.get_topic('trader')
    if not await require_configured_topic(callback, topic, "Торговец Локи"):
        return
    orders = await db.list_market_orders(requester_id=callback.from_user.id, limit=10)
    if not orders:
        await callback.answer("У вас пока нет заказов.", show_alert=True)
        return
    lines = ["<b>📋 Мои последние заказы</b>", ""]
    for order in orders:
        lines.append(f"{WORKFLOW_LABELS.get(order.workflow_status, '•')} <b>#{order.id}</b> — {fmt_dt(order.created_at)}")
    buttons = [[InlineKeyboardButton(text=f'Заказ #{order.id}',callback_data=f'gmarket:view:{order.id}')] for order in orders]
    await temp_callback_message(callback, "\n".join(lines), ttl=config.temp_message_ttl,reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data == 'gmarket:queue')
async def trader_queue(callback: CallbackQuery, db: Database, config: Config):
    if not await require_configured_topic(callback,await db.get_topic('trader'),'Торговец Локи'):
        return
    target = await db.get_market_merchant_target()
    if not await merchant_authorized(callback.from_user.id,callback.from_user.username,target,db,config):
        return await callback.answer('Очередь доступна торговцу и руководству.',show_alert=True)
    rows = await db.community_rows("SELECT id FROM market_orders WHERE workflow_status IN ('pending','accepted','assembled') ORDER BY id LIMIT 30")
    buttons = [[InlineKeyboardButton(text=f'Заказ #{r["id"]}',callback_data=f'gmarket:view:{r["id"]}')] for r in rows]
    await temp_callback_message(callback,'<b>📋 Очередь торговца — первые 30 активных заказов</b>' if rows else 'Активных заказов нет.',reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),ttl=300)
    await callback.answer()


@router.callback_query(F.data.regexp(r'^gmarket:view:\d+$'))
async def trader_order_view(callback: CallbackQuery, db: Database, config: Config):
    if not await require_configured_topic(callback,await db.get_topic('trader'),'Торговец Локи'):
        return
    loaded = await db.get_market_order(int(callback.data.rsplit(':',1)[1]))
    if not loaded:
        return await callback.answer('Заказ не найден.',show_alert=True)
    order,items = loaded
    manage = await merchant_authorized(callback.from_user.id,callback.from_user.username,order.merchant_target,db,config)
    if callback.from_user.id != order.requester_id and not manage:
        return await callback.answer('Это чужой заказ.',show_alert=True)
    await temp_callback_message(callback,market_order_group_text(order,items),reply_markup=market_order_status_keyboard(order.id,order.workflow_status) if manage else None,ttl=300)
    await callback.answer()


# ---------------------------------------------------------------------------
# Merchant assignment and order workflow
# ---------------------------------------------------------------------------

@router.message(Command("set_gp_merchant"), F.chat.type.in_(GROUP_TYPES))
async def set_gp_merchant_command(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.manage", db, config):
        await temp_answer(message, "Недостаточно прав.", ttl=45)
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
        await temp_answer(
            message,
            "Назначение Торговца ГП:\n"
            "• ответьте командой <code>/set_gp_merchant</code> на сообщение Торговца; или\n"
            "• <code>/set_gp_merchant @username</code>; или\n"
            "• <code>/set_gp_merchant 123456789</code>.",
            ttl=120,
        )
        return
    await db.set_market_merchant_target(target)
    await db.audit(message.from_user.id, "market.merchant", target)
    await temp_answer(message, f"✅ Торговец ГП назначен: <code>{escape(target)}</code>", ttl=60)
    await delete_incoming_later(message)


@router.callback_query(F.data == "gadmin:market", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_admin_market(callback: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not await has_permission(callback.from_user.id, "market.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    target = await db.get_market_merchant_target()
    topic = await db.get_topic('trader')
    connected = await telethon.is_connected()
    await callback.message.edit_text(
        "<b>🛒 Торговец Локи — настройки заказов</b>\n\n"
        f"Тема: {'✅ настроена' if topic else '⚠️ не настроена'}\n"
        f"Торговец: <code>{escape(target) if target else 'не назначен'}</code>\n"
        f"Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n\n"
        "Проще всего назначить Торговца: ответьте на его сообщение командой <code>/set_gp_merchant</code>.\n"
        "Для привязки темы отправьте <code>/set_trader_topic</code> внутри «Торговец Локи».",
        reply_markup=group_market_admin_menu(bool(target)),
    )
    await callback.answer()


@router.callback_query(F.data == "gmarket_settings:merchant", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_market_merchant_start(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "market.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.update_data(flow_chat_id=callback.message.chat.id, flow_thread_id=callback.message.message_thread_id)
    await state.set_state(GroupMarketSettings.merchant_target)
    await temp_callback_message(
        callback,
        "👤 Напишите <code>@username</code> или числовой Telegram ID Торговца ГП.\n\n"
        "Ещё удобнее: отмените это действие и ответьте командой <code>/set_gp_merchant</code> на любое сообщение самого Торговца.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")]]),
        ttl=300,
    )
    await callback.answer()


@router.message(GroupMarketSettings.merchant_target, F.chat.type.in_(ADMIN_CHAT_TYPES), ~F.text.startswith("/"))
async def group_market_merchant_save(message: Message, db: Database, state: FSMContext, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "market.manage", db, config):
        return
    if not await flow_matches_message(message, state):
        await temp_answer(message, "Продолжите настройку в той же теме.", ttl=60)
        return
    raw = (message.text or "").strip()
    if raw.startswith("https://t.me/"):
        raw = "@" + raw.split("https://t.me/", 1)[1].strip("/").split("/", 1)[0]
    if not (re.fullmatch(r"-?\d+", raw) or re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw)):
        await temp_answer(message, "Введите @username или числовой Telegram ID.", ttl=60)
        return
    await db.set_market_merchant_target(raw)
    await db.audit(message.from_user.id, "market.merchant", raw)
    await state.clear()
    await safe_delete(message)
    await temp_answer(message, f"✅ Торговец ГП назначен: <code>{escape(raw)}</code>", ttl=60)


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
            "🧪 <b>Тест связи XZONA Group Bot</b>\n\nДоставка заказов Торговца Локи настроена.",
        )
        await temp_callback_message(callback, f"✅ Тест доставлен через {'Telegram Bot' if method == 'bot' else 'Telethon'}.", ttl=60)
    except Exception as exc:
        await temp_callback_message(callback, f"⚠️ Личная доставка не прошла: <code>{escape(str(exc)[:500])}</code>\nЗаказы всё равно будут видны в теме Торговец Локи.", ttl=120)


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
    try:
        await db.advance_order(order_id, new_status, callback.from_user.id)
    except ValueError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    loaded = await db.get_market_order(order_id)
    order, items = loaded
    await refresh_order_cards(bot, order, items)
    if new_status == "assembled":
        try:
            from .multitask_handlers import publish_delivery_card
            await publish_delivery_card(bot, db, order_id)
        except Exception:
            pass
    if new_status in {"issued", "rejected"}:
        ref = await db.get_market_delivery_ref(order_id)
        if ref:
            try:
                from .multitask_handlers import delivery_text
                await bot.edit_message_text(chat_id=ref[0], message_id=ref[2], text=delivery_text(order, items), reply_markup=None)
            except Exception:
                pass
    await callback.answer(WORKFLOW_LABELS.get(new_status, "Статус изменён"))


# ---------------------------------------------------------------------------
# Shared cancel for all group-first workflows
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "gflow:cancel", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def group_flow_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Операция отменена.")
        schedule_delete(callback.bot, callback.message.chat.id, callback.message.message_id, 20)
    except Exception:
        pass
    await callback.answer()


# Optional text shortcuts if admins/users still have an old reply keyboard.
@router.message(F.text == "🎒 Снаряжение группировки", F.chat.type.in_(GROUP_TYPES))
async def group_storage_text_shortcut(message: Message, db: Database, config: Config):
    if topic_tuple_from_message(message) != await db.get_storage_topic():
        return
    count, players = await db.storage_stats()
    await temp_answer(
        message, f"<b>🎒 Снаряжение группировки</b>\n\nНа хранении: <b>{count}</b>\nИгроков: <b>{players}</b>", reply_markup=group_storage_panel(), ttl=config.temp_message_ttl
    )


@router.message(F.text == "🛒 Рынок ГП", F.chat.type.in_(GROUP_TYPES))
async def group_market_text_shortcut(message: Message, db: Database, config: Config):
    if topic_tuple_from_message(message) != await db.get_market_topic():
        return
    await temp_answer(message, "<b>🛒 Рынок ГП</b>", reply_markup=group_market_panel(), ttl=config.temp_message_ttl)

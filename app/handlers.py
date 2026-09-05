from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config
from .housekeeping import temp_answer, topic_answer
from .db import Database, MarketOrder as DbMarketOrder, MarketOrderItem, StorageItem
from .keyboards import (
    admin_menu,
    group_role_request_review_keyboard,
    confirm_add_keyboard,
    delete_confirm_keyboard,
    item_keyboard,
    issue_confirm_keyboard,
    main_menu,
    market_cart_keyboard,
    market_comment_skip_keyboard,
    market_menu,
    market_settings_keyboard,
    market_topic_panel,
    nicks_admin_menu,
    players_keyboard,
    recent_names_keyboard,
    skip_comment_keyboard,
    storage_items_keyboard,
    storage_menu,
    telethon_menu,
)
from .nicks import extract_nickname
from .roles import INTERNAL_POSITION_ORDER, POSITIONS, ROLE_CAPACITIES, allowed_position_lines, has_position_permission, parse_profile, position_display
from .states import AddItem, EditItem, MarketOrder, MarketSettings, RegisterPlayer, TelethonSetup
from .telethon_manager import TelethonManager
from .telethon_web import TelethonWebAuth

router = Router()


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


def can_manage_telethon(user_id: int, config: Config) -> bool:
    if config.owner_id is not None:
        return user_id == config.owner_id
    return is_admin(user_id, config)


def fmt_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def item_text(item: StorageItem) -> str:
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


def market_order_text(order: DbMarketOrder, items: list[MarketOrderItem]) -> str:
    username = f"@{escape(order.requester_username)}" if order.requester_username else "—"
    status_map = {"created": "🟡 Создан", "sent": "🟢 Отправлен", "failed": "🔴 Ошибка отправки"}
    lines = [
        f"<b>🛒 ЗАКАЗ ГП #{order.id}</b>",
        "",
        f"👤 Игрок: <b>{escape(order.requester_nickname)}</b>",
        f"Telegram: {username}",
        f"ID: <code>{order.requester_id}</code>",
        "",
        "<b>📦 Позиции:</b>",
    ]
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}. {escape(item.item_name)} × <b>{item.quantity}</b>")
    lines.extend(
        [
            "",
            f"📝 Комментарий: {escape(order.comment) if order.comment else '—'}",
            f"📌 Статус: {status_map.get(order.status, escape(order.status))}",
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


async def ensure_private(message: Message) -> bool:
    if message.chat.type == "private":
        return True
    await topic_answer(message, "Эта операция доступна только в личном чате с ботом.")
    return False


async def ensure_registered(message: Message, db: Database, state: FSMContext, after_register: str | None = None) -> bool:
    player = await db.get_player(message.from_user.id)
    if player:
        return True
    await state.set_state(RegisterPlayer.nickname)
    if after_register:
        await state.update_data(after_register=after_register)
    await topic_answer(message, "👤 Сначала укажите ваш <b>ник в игре</b>:")
    return False


@dataclass(slots=True)
class NickSyncOutcome:
    handled: bool
    error: str | None = None
    notice: str | None = None
    request_id: int | None = None


async def publish_role_request_card(bot: Bot, db: Database, request_id: int) -> None:
    req = await db.get_role_request(request_id)
    if not req or req.status != "pending":
        return
    # Repeated edits/imports must not create duplicate approval cards.
    if req.notification_chat_id and req.notification_message_id:
        return
    topic = await db.get_nicks_topic()
    if not topic:
        return
    text = (
        f"<b>🎖 Новая заявка на должность #{req.id}</b>\n\n"
        f"👤 Игрок: <a href=\"tg://user?id={req.telegram_id}\"><b>{escape(req.player_nickname)}</b></a>\n"
        f"Запрошено: <b>{escape(req.requested_label)}</b>\n\n"
        "Лидер или Заместитель может принять решение прямо здесь:"
    )
    try:
        sent = await bot.send_message(
            topic[0], text, message_thread_id=topic[1],
            reply_markup=group_role_request_review_keyboard(req.id),
        )
        await db.set_role_request_notification(req.id, sent.chat.id, topic[1], sent.message_id)
    except Exception:
        pass


async def available_position_lines_for_user(db: Database, user_id: int) -> list[str]:
    lines: list[str] = []
    for code in INTERNAL_POSITION_ORDER:
        if not await db.position_slot_available(code, exclude_telegram_id=user_id):
            continue
        capacity = ROLE_CAPACITIES.get(code)
        if capacity is None:
            lines.append(POSITIONS[code].label)
        else:
            used = await db.position_count(code, exclude_telegram_id=user_id)
            lines.append(f"{POSITIONS[code].label} (свободно {capacity - used}/{capacity})")
    lines.extend(["Лидер <группировки>", "Заместитель <группировки>"] )
    return lines


async def sync_nick_message(message: Message, db: Database) -> NickSyncOutcome:
    topic = await db.get_nicks_topic()
    if not topic or not message.is_topic_message or message.message_thread_id is None:
        return NickSyncOutcome(False)
    if (message.chat.id, message.message_thread_id) != topic:
        return NickSyncOutcome(False)
    if not message.from_user or message.from_user.is_bot:
        return NickSyncOutcome(True)

    raw = message.text or message.caption
    if raw and raw.lstrip().startswith("/"):
        return NickSyncOutcome(True)

    profile = parse_profile(raw, allow_legacy=False)
    if not profile:
        allowed = "\n".join(f"• {item}" for item in await available_position_lines_for_user(db, message.from_user.id))
        return NickSyncOutcome(
            True,
            "Напишите данные двумя строками:\n<code>ИгровойНик\nДолжность</code>\n\n"
            f"Доступные варианты должности:\n{allowed}",
        )
    if profile.position_code == "__invalid__":
        allowed = "\n".join(f"• {item}" for item in await available_position_lines_for_user(db, message.from_user.id))
        return NickSyncOutcome(
            True,
            f"Неизвестная должность: <b>{escape(profile.position_label or '—')}</b>.\n\n"
            f"Используйте один из вариантов:\n{allowed}",
        )
    if await db.nickname_exists_for_other(message.from_user.id, profile.nickname):
        return NickSyncOutcome(True, f"Ник «{escape(profile.nickname)}» уже привязан к другому Telegram-пользователю.")

    await db.upsert_player(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        game_nickname=profile.nickname,
    )
    current = await db.get_player(message.from_user.id)

    same_approved = bool(
        current
        and current.position_status == "approved"
        and current.position_code == profile.position_code
        and current.faction_code == profile.faction_code
    )
    if same_approved:
        return NickSyncOutcome(
            True,
            notice=(
                f"✅ Данные обновлены.\n👤 <b>{escape(profile.nickname)}</b>\n"
                f"🎖 <b>{escape(position_display(profile.position_code, profile.faction_code))}</b> — уже подтверждено."
            ),
        )

    if not await db.position_slot_available(profile.position_code, exclude_telegram_id=message.from_user.id):
        return NickSyncOutcome(
            True,
            error=(
                f"⛔ Должность <b>{escape(position_display(profile.position_code, profile.faction_code))}</b> сейчас полностью занята.\n"
                "Посмотрите актуальный список свободных должностей в закреплённой инструкции этой темы."
            ),
        )

    request_id = await db.create_role_request(
        message.from_user.id,
        profile.position_code,
        profile.faction_code,
        profile.position_label or position_display(profile.position_code, profile.faction_code),
    )
    return NickSyncOutcome(
        True,
        notice=(
            f"✅ Ник сохранён: <b>{escape(profile.nickname)}</b>\n"
            f"🎖 Запрошена должность: <b>{escape(position_display(profile.position_code, profile.faction_code))}</b>\n"
            "⏳ Должность ожидает подтверждения Лидера/Заместителя."
        ),
        request_id=request_id,
    )


async def send_to_merchant(bot: Bot, telethon: TelethonManager, target: str, text: str) -> str:
    target = target.strip()
    bot_error: Exception | None = None

    # Bot API can reliably address a private user only by numeric ID and only
    # after that user has started/interacted with the bot.
    if target.lstrip("-").isdigit():
        try:
            await bot.send_message(int(target), text)
            return "bot"
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            bot_error = exc

    if await telethon.is_connected():
        try:
            await telethon.send_message(target, text)
            return "telethon"
        except Exception as exc:
            if bot_error:
                raise RuntimeError(f"Bot API: {bot_error}; Telethon: {exc}") from exc
            raise

    if bot_error:
        raise RuntimeError(
            "Бот не смог написать Торговцу ГП. Торговец должен сначала открыть бота и нажать /start, "
            "либо подключите Telethon."
        ) from bot_error
    raise RuntimeError("Для отправки по @username подключите Telethon в админ-меню.")


# ---------------------------------------------------------------------------
# Topic setup and nickname sync
# ---------------------------------------------------------------------------

@router.message(Command("set_nicks_topic"))
async def set_nicks_topic(message: Message, db: Database, config: Config):
    if not is_admin(message.from_user.id, config):
        await topic_answer(message, "Недостаточно прав.")
        return
    if not message.is_topic_message or message.message_thread_id is None:
        await topic_answer(message, "Отправьте эту команду прямо внутри темы «Ники игроков».")
        return
    await db.set_nicks_topic(message.chat.id, message.message_thread_id)
    await topic_answer(message, 
        "✅ Эта тема назначена источником ников.\n\n"
        "Новые сообщения игроков будут сохраняться автоматически.\n\n"
        "📚 Старые ники импортируются без отдельного скрипта: /admin в группе → 👥 Ники игроков → 🔄 Импортировать старые ники."
    )


@router.message(Command("set_market_topic"))
async def set_market_topic(message: Message, db: Database, config: Config, bot: Bot):
    if not is_admin(message.from_user.id, config):
        await topic_answer(message, "Недостаточно прав.")
        return
    if not message.is_topic_message or message.message_thread_id is None:
        await topic_answer(message, "Отправьте эту команду прямо внутри темы «Рынок ГП».")
        return
    await db.set_market_topic(message.chat.id, message.message_thread_id)
    me = await bot.get_me()
    await topic_answer(message, 
        "✅ Эта тема назначена как <b>Рынок ГП</b>.\n\n"
        "Заказы оформляются прямо в этой теме и отправляются Торговцу ГП.",
        reply_markup=market_topic_panel(me.username),
    )


@router.message(Command("nicks_status"))
async def nicks_status(message: Message, db: Database, config: Config, telethon: TelethonManager):
    if not is_admin(message.from_user.id, config):
        await topic_answer(message, "Недостаточно прав.")
        return
    topic = await db.get_nicks_topic()
    if not topic:
        await topic_answer(message, "Тема с никами ещё не настроена. Откройте «Ники игроков» и отправьте /set_nicks_topic.")
        return
    chat_id, thread_id = topic
    imported_at, imported_count = await db.get_nicks_history_import_status()
    players_count = await db.count_players()
    connected = await telethon.is_connected()
    history_line = (
        f"✅ Старая история импортирована: <b>{imported_count}</b> игроков/обновлений\n🕓 {escape(imported_at)}"
        if imported_at
        else "⚠️ Старая история ещё не импортирована."
    )
    await topic_answer(message, 
        "✅ Источник ников настроен.\n"
        f"Chat ID: <code>{chat_id}</code>\n"
        f"Topic ID: <code>{thread_id}</code>\n"
        f"👥 Игроков в базе: <b>{players_count}</b>\n"
        f"🔐 Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n\n"
        f"{history_line}"
    )


@router.message(Command("import_nick"))
async def import_old_nick(message: Message, db: Database, config: Config):
    if not is_admin(message.from_user.id, config):
        await topic_answer(message, "Недостаточно прав.")
        return
    source = message.reply_to_message
    if not source:
        await topic_answer(message, "Ответьте командой /import_nick на старое сообщение игрока с его ником.")
        return
    outcome = await sync_nick_message(source, db)
    if not outcome.handled:
        await topic_answer(message, "Это сообщение не из настроенной темы «Ники игроков».")
    elif outcome.error:
        await topic_answer(message, f"⚠️ {outcome.error}")
    else:
        nickname = extract_nickname(source.text or source.caption)
        await topic_answer(message, outcome.notice or f"✅ Импортирован ник: <b>{escape(nickname or '—')}</b>")


@router.edited_message()
async def sync_edited_nickname(message: Message, db: Database):
    outcome = await sync_nick_message(message, db)
    if outcome.handled and outcome.error:
        await temp_answer(message, f"⚠️ {outcome.error}", ttl=90)
    elif outcome.handled and outcome.notice:
        await temp_answer(message, outcome.notice, ttl=120)
        if outcome.request_id:
            await publish_role_request_card(message.bot, db, outcome.request_id)


# ---------------------------------------------------------------------------
# Start, registration and main menu
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def start(
    message: Message,
    db: Database,
    state: FSMContext,
    config: Config,
    telethon_web: TelethonWebAuth,
):
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        payload = parts[1].strip().lower()

    # Compatibility with links created by older versions: instead of asking
    # for secrets in chat, issue a short-lived browser link.
    if payload == "telethon":
        if not can_manage_telethon(message.from_user.id, config):
            await topic_answer(message, "Настройка Telethon доступна только владельцу бота.")
            return
        if message.chat.type != "private":
            await topic_answer(message, "Откройте личный чат с ботом и нажмите /start.")
            return
        url = telethon_web.create_login_url(message.from_user.id)
        await topic_answer(message, 
            "🔐 <b>Telethon</b>\n\nСекретные данные теперь вводятся в отдельном браузерном окне.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🪟 Открыть окно авторизации", url=url)]]
            ),
        )
        return

    await state.clear()
    player = await db.get_player(message.from_user.id)
    name = f"<b>{escape(player.game_nickname)}</b>" if player else "участник"
    await topic_answer(message, 
        f"🎮 Привет, {name}.\n\n"
        "Рабочие функции XZONA Bot выполняются <b>внутри тем игровой группы</b>.\n"
        "• Ник и должность — в теме «Ники игроков»\n"
        "• Снаряжение — в теме «Снаряжение группировки»\n"
        "• Заказы — в теме «Рынок ГП»\n\n"
        "Личный чат нужен только для получения защищённой ссылки на окно авторизации Telethon и для личных уведомлений."
    )


@router.message(Command("myid"))
async def my_id(message: Message):
    await topic_answer(message, f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("my_role"))
async def my_role(message: Message, db: Database):
    player = await db.get_player(message.from_user.id)
    if not player:
        await topic_answer(message, "Ваш профиль ещё не найден. Напишите ник и должность в теме «Ники игроков».")
        return
    pending = await db.get_pending_role_request_for_user(message.from_user.id)
    current = position_display(player.position_code, player.faction_code)
    lines = [
        f"👤 <b>{escape(player.game_nickname)}</b>",
        f"🎖 Подтверждённая должность: <b>{escape(current)}</b>",
    ]
    if pending:
        lines.append(f"⏳ Ожидает подтверждения: <b>{escape(pending.requested_label)}</b>")
    elif player.position_status != "approved":
        lines.append("⚠️ Должность ещё не назначена.")
    await topic_answer(message, "\n".join(lines))


@router.message(Command("cancel"))
async def cancel_any(message: Message, state: FSMContext, telethon: TelethonManager, config: Config):
    await state.clear()
    if can_manage_telethon(message.from_user.id, config):
        await telethon.cancel_pending()
    if message.chat.type == "private":
        await topic_answer(message, "❌ Операция отменена.", reply_markup=main_menu(is_admin(message.from_user.id, config)))
    else:
        await topic_answer(message, "❌ Операция отменена.")


@router.message(RegisterPlayer.nickname)
async def register_nickname(message: Message, db: Database, state: FSMContext, config: Config):
    nickname = (message.text or "").strip()
    if len(nickname) < 2 or len(nickname) > 40:
        await topic_answer(message, "Ник должен быть длиной от 2 до 40 символов. Попробуйте ещё раз:")
        return
    if await db.nickname_exists_for_other(message.from_user.id, nickname):
        await topic_answer(message, "Такой игровой ник уже зарегистрирован. Введите другой:")
        return
    data = await state.get_data()
    await db.upsert_player(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        game_nickname=nickname,
    )
    await state.clear()
    if data.get("after_register") == "market":
        await topic_answer(message, 
            f"✅ Ник сохранён: <b>{escape(nickname)}</b>\n\n🛒 <b>Рынок ГП</b>",
            reply_markup=market_menu(is_admin(message.from_user.id, config)),
        )
    else:
        await topic_answer(message, 
            f"✅ Ник сохранён: <b>{escape(nickname)}</b>",
            reply_markup=main_menu(is_admin(message.from_user.id, config)),
        )


@router.message(F.text == "👤 Мой профиль")
async def my_profile(message: Message, db: Database, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player:
        await state.set_state(RegisterPlayer.nickname)
        await topic_answer(message, "👤 Введите ваш игровой ник:")
        return
    items = await db.list_player_items(message.from_user.id)
    await topic_answer(message, 
        f"👤 <b>{escape(player.game_nickname)}</b>\n"
        f"Telegram: @{escape(player.username) if player.username else '—'}\n"
        f"🎖 Должность: <b>{escape(position_display(player.position_code, player.faction_code))}</b>"
        f"{' ✅' if player.position_status == 'approved' else ' ⏳'}\n"
        f"📦 Сейчас на хранении: <b>{len(items)}</b>\n\n"
        "Чтобы сменить ник: /nickname"
    )


@router.message(Command("nickname"))
async def change_nickname(message: Message, state: FSMContext):
    await state.set_state(RegisterPlayer.nickname)
    await topic_answer(message, "Введите новый игровой ник:")


# ---------------------------------------------------------------------------
# Admin menu and integrated Telethon setup
# ---------------------------------------------------------------------------

@router.message(F.text == "⚙️ Администрирование")
@router.message(Command("admin"))
async def admin_home_message(message: Message, config: Config):
    if not is_admin(message.from_user.id, config):
        await topic_answer(message, "Недостаточно прав.")
        return
    await topic_answer(message, "⚙️ <b>Администрирование</b>", reply_markup=admin_menu())


@router.callback_query(F.data == "admin:home")
async def admin_home_callback(callback: CallbackQuery, config: Config):
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.message.edit_text("⚙️ <b>Администрирование</b>", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:telethon")
async def admin_telethon(callback: CallbackQuery, config: Config, telethon: TelethonManager):
    if not can_manage_telethon(callback.from_user.id, config):
        await callback.answer("Настройка Telethon доступна только владельцу.", show_alert=True)
        return
    connected = await telethon.is_connected()
    text = (
        "<b>🔐 Telethon</b>\n\n"
        f"Статус: {'🟢 Подключён' if connected else '🔴 Не подключён'}\n"
        f"Аккаунт: <code>{escape(telethon.masked_phone())}</code>\n\n"
        "Telethon нужен для чтения старой истории темы «Ники игроков» и может использоваться "
        "для отправки заказа Торговцу ГП по @username."
    )
    if telethon.last_error and not connected:
        text += f"\n\n⚠️ Последняя ошибка: <code>{escape(telethon.last_error[:300])}</code>"
    await callback.message.edit_text(text, reply_markup=telethon_menu(connected))
    await callback.answer()


@router.callback_query(F.data == "telethon:setup")
async def telethon_setup_start(
    callback: CallbackQuery,
    state: FSMContext,
    config: Config,
    telethon_web: TelethonWebAuth,
):
    if not can_manage_telethon(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if callback.message.chat.type != "private":
        await callback.answer("Получите окно авторизации через /admin в группе.", show_alert=True)
        return
    await state.clear()
    url = telethon_web.create_login_url(callback.from_user.id)
    await topic_answer(callback.message, 
        "🔐 <b>Подключение Telethon</b>\n\nВведите API ID/API HASH, телефон, код и 2FA в браузерном окне:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🪟 Открыть окно авторизации", url=url)]]
        ),
    )
    await callback.answer()


@router.message(TelethonSetup.api_id)
async def telethon_api_id(message: Message, state: FSMContext, config: Config):
    if not can_manage_telethon(message.from_user.id, config) or not await ensure_private(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await topic_answer(message, "API ID должен состоять из цифр.")
        return
    await state.update_data(telethon_api_id=int(raw))
    await safe_delete(message)
    await state.set_state(TelethonSetup.api_hash)
    await topic_answer(message, "2/4. Введите <b>API HASH</b>:")


@router.message(TelethonSetup.api_hash)
async def telethon_api_hash(message: Message, state: FSMContext, config: Config):
    if not can_manage_telethon(message.from_user.id, config) or not await ensure_private(message):
        return
    api_hash = (message.text or "").strip()
    if len(api_hash) < 20:
        await topic_answer(message, "API HASH выглядит слишком коротким. Проверьте значение.")
        return
    await state.update_data(telethon_api_hash=api_hash)
    await safe_delete(message)
    await state.set_state(TelethonSetup.phone)
    await topic_answer(message, "3/4. Введите номер Telegram-аккаунта в международном формате, например <code>+79991234567</code>:")


@router.message(TelethonSetup.phone)
async def telethon_phone(message: Message, state: FSMContext, config: Config, telethon: TelethonManager):
    if not can_manage_telethon(message.from_user.id, config) or not await ensure_private(message):
        return
    phone = re.sub(r"[\s()\-]", "", (message.text or "").strip())
    if not re.fullmatch(r"\+\d{7,15}", phone):
        await topic_answer(message, "Введите номер в формате +79991234567.")
        return
    data = await state.get_data()
    await safe_delete(message)
    wait = await topic_answer(message, "⏳ Запрашиваю код у Telegram…")
    try:
        await telethon.begin_login(data["telethon_api_id"], data["telethon_api_hash"], phone)
    except Exception as exc:
        await state.clear()
        await wait.edit_text(f"❌ Не удалось запросить код: <code>{escape(str(exc)[:500])}</code>")
        return
    await state.set_state(TelethonSetup.code)
    await wait.edit_text(
        "4/4. Telegram отправил код входа. Введите его сюда.\n\n"
        "Например: <code>12345</code>\n"
        "После обработки сообщение с кодом будет удалено."
    )


@router.message(TelethonSetup.code)
async def telethon_code(message: Message, state: FSMContext, config: Config, telethon: TelethonManager):
    if not can_manage_telethon(message.from_user.id, config) or not await ensure_private(message):
        return
    code = re.sub(r"\D", "", (message.text or ""))
    await safe_delete(message)
    if not 3 <= len(code) <= 8:
        await topic_answer(message, "Код выглядит неверно. Введите только цифры из сообщения Telegram.")
        return
    try:
        result = await telethon.submit_code(code)
    except Exception as exc:
        await topic_answer(message, f"❌ {escape(str(exc)[:500])}")
        return
    if result == "password":
        await state.set_state(TelethonSetup.password)
        await topic_answer(message, 
            "🔑 На аккаунте включена двухэтапная аутентификация. Введите облачный пароль Telegram.\n"
            "Пароль не сохраняется и сообщение будет удалено."
        )
        return
    await state.clear()
    await topic_answer(message, "✅ <b>Telethon подключён.</b> Теперь можно импортировать старые ники.")


@router.message(TelethonSetup.password)
async def telethon_password(message: Message, state: FSMContext, config: Config, telethon: TelethonManager):
    if not can_manage_telethon(message.from_user.id, config) or not await ensure_private(message):
        return
    password = message.text or ""
    await safe_delete(message)
    try:
        await telethon.submit_password(password)
    except Exception as exc:
        await topic_answer(message, f"❌ Не удалось войти: <code>{escape(str(exc)[:500])}</code>")
        return
    await state.clear()
    await topic_answer(message, "✅ <b>Telethon подключён.</b> Облачный пароль не сохранён.")


@router.callback_query(F.data == "telethon:disconnect")
async def telethon_disconnect(callback: CallbackQuery, config: Config, telethon: TelethonManager):
    if not can_manage_telethon(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await telethon.disconnect(clear_saved=True)
    await callback.message.edit_text("🔴 Telethon отключён, сохранённая сессия удалена.", reply_markup=telethon_menu(False))
    await callback.answer()


@router.callback_query(F.data == "admin:nicks")
async def admin_nicks(callback: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    topic = await db.get_nicks_topic()
    imported_at, imported_count = await db.get_nicks_history_import_status()
    players_count = await db.count_players()
    connected = await telethon.is_connected()
    text = ["<b>👥 Ники игроков</b>", ""]
    if topic:
        text.append(f"✅ Тема настроена: <code>{topic[0]}</code> / <code>{topic[1]}</code>")
    else:
        text.append("⚠️ Тема не настроена. В теме «Ники игроков» отправьте /set_nicks_topic.")
    text.append(f"👥 Игроков в базе: <b>{players_count}</b>")
    if imported_at:
        text.append(f"📚 Последний импорт: <b>{imported_count}</b>, {escape(imported_at)}")
    else:
        text.append("📚 Старые сообщения ещё не импортировались.")
    text.append(f"🔐 Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}")
    await callback.message.edit_text("\n".join(text), reply_markup=nicks_admin_menu(connected and topic is not None))
    await callback.answer()


@router.callback_query(F.data == "telethon:sync_nicks")
async def telethon_sync_nicks(callback: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not await has_permission(callback.from_user.id, "roles.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if not await telethon.is_connected():
        await callback.answer("Сначала подключите Telethon.", show_alert=True)
        return
    await callback.answer("Импорт запущен")
    progress = await temp_answer(callback.message, "⏳ Читаю старую историю темы «Ники игроков»…", ttl=300)
    try:
        result = await telethon.sync_nicks_history()
    except Exception as exc:
        await progress.edit_text(f"❌ Ошибка импорта: <code>{escape(str(exc)[:700])}</code>")
        return
    pending = await db.list_pending_role_requests(limit=200)
    for req in pending:
        await publish_role_request_card(callback.bot, db, req.id)
    try:
        from .multitask_handlers import announce_topic
        await announce_topic(callback.bot, db, "nicks", force=True)
    except Exception:
        pass
    await progress.edit_text(
        "✅ <b>Импорт старых ников завершён</b>\n\n"
        f"Сообщений просмотрено: <b>{result.scanned}</b>\n"
        f"Игроков найдено: <b>{result.found}</b>\n"
        f"Добавлено/обновлено: <b>{result.imported}</b>\n"
        f"Конфликтов: <b>{result.conflicts}</b>\n"
        f"Пропущено: <b>{result.invalid}</b>\n"
        f"Заявок на должность: <b>{len(pending)}</b>"
    )


# ---------------------------------------------------------------------------
# Market GP
# ---------------------------------------------------------------------------

@router.message(F.text == "🛒 Торговец Локи")
async def market_home(message: Message, db: Database, state: FSMContext, config: Config, bot: Bot):
    topic = await db.get_topic('trader')
    if message.chat.type == "private":
        await state.clear()
        await topic_answer(message, "🛒 Заказы теперь оформляются прямо в настроенной теме «Торговец Локи» игровой группы.")
        return
    if topic and message.is_topic_message and (message.chat.id, message.message_thread_id) == topic:
        await topic_answer(message, "🛒 <b>Торговец Локи</b>\n\nИспользуйте панель темы для создания заказа.")
        return


@router.callback_query(F.data == "market:new")
async def market_new(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    await state.clear()
    await callback.answer("Оформляйте заказ в теме «Торговец Локи» игровой группы.", show_alert=True)
    return


@router.message(MarketOrder.item_name)
async def market_item_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await topic_answer(message, "Название должно быть от 1 до 80 символов.")
        return
    await state.update_data(market_pending_name=name)
    await state.set_state(MarketOrder.quantity)
    await topic_answer(message, f"🎒 <b>{escape(name)}</b>\n\n🔢 Введите количество:")


@router.message(MarketOrder.quantity)
async def market_item_quantity(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await topic_answer(message, "Введите целое количество от 1 до 9999.")
        return
    data = await state.get_data()
    items = list(data.get("market_items", []))
    items.append({"name": data["market_pending_name"], "quantity": int(raw)})
    await state.update_data(market_items=items)
    await state.set_state(None)
    await topic_answer(message, 
        cart_text(items, data.get("market_comment")),
        reply_markup=market_cart_keyboard(bool(items)),
    )


@router.callback_query(F.data == "market:add_more")
async def market_add_more(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "market_items" not in data:
        await state.update_data(market_items=[], market_comment=None)
    await state.set_state(MarketOrder.item_name)
    await topic_answer(callback.message, "🎒 Введите название следующей позиции:")
    await callback.answer()


@router.callback_query(F.data == "market:comment")
async def market_comment_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MarketOrder.comment)
    await topic_answer(callback.message, "📝 Введите комментарий к заказу:", reply_markup=market_comment_skip_keyboard())
    await callback.answer()


@router.message(MarketOrder.comment)
async def market_comment_save(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if len(comment) > 500:
        await topic_answer(message, "Комментарий слишком длинный. Максимум 500 символов.")
        return
    data = await state.get_data()
    await state.update_data(market_comment=comment or None)
    await state.set_state(None)
    items = data.get("market_items", [])
    await topic_answer(message, cart_text(items, comment or None), reply_markup=market_cart_keyboard(bool(items)))


@router.callback_query(F.data == "market:comment_skip")
async def market_comment_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(market_comment=None)
    await state.set_state(None)
    items = data.get("market_items", [])
    await topic_answer(callback.message, cart_text(items, None), reply_markup=market_cart_keyboard(bool(items)))
    await callback.answer()


@router.callback_query(F.data == "market:cancel")
async def market_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Заказ отменён.")
    await callback.answer()


@router.callback_query(F.data == "market:submit")
async def market_submit(callback: CallbackQuery, db: Database, state: FSMContext, bot: Bot, telethon: TelethonManager, config: Config):
    if not await has_permission(callback.from_user.id, "market.create", db, config):
        await callback.answer("Торговец Локи доступен только участникам Полдня.", show_alert=True)
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
    loaded = await db.get_market_order(order_id)
    if not loaded:
        await callback.answer("Не удалось создать заказ.", show_alert=True)
        return
    order, order_items = loaded
    text = market_order_text(order, order_items)
    await callback.answer("Отправляю заказ…")
    try:
        method = await send_to_merchant(bot, telethon, target, text)
        await db.mark_market_order_sent(order_id, method)
    except Exception as exc:
        await db.mark_market_order_failed(order_id)
        await state.clear()
        await callback.message.edit_text(
            f"🔴 <b>Заказ #{order_id} сохранён, но не отправлен.</b>\n\n"
            f"Ошибка: <code>{escape(str(exc)[:700])}</code>\n\n"
            "Администратор может исправить получателя/Telethon и создать заказ повторно."
        )
        return

    await state.clear()
    loaded = await db.get_market_order(order_id)
    order, order_items = loaded
    method_label = "Telegram Bot" if method == "bot" else "Telethon"
    await callback.message.edit_text(
        f"✅ <b>Заказ #{order_id} отправлен Торговцу ГП</b>\n"
        f"Способ: {method_label}\n\n" + market_order_text(order, order_items)
    )

    topic = await db.get_topic('trader')
    if topic:
        try:
            await bot.send_message(
                topic[0],
                f"✅ Заказ ГП <b>#{order_id}</b> от <b>{escape(player.game_nickname)}</b> сформирован и отправлен Торговцу ГП.",
                message_thread_id=topic[1],
            )
        except Exception:
            pass


@router.callback_query(F.data == "market:mine")
async def market_mine(callback: CallbackQuery, db: Database):
    orders = await db.list_market_orders(requester_id=callback.from_user.id, limit=10)
    if not orders:
        await callback.message.edit_text("📭 У вас пока нет заказов.")
        await callback.answer()
        return
    icons = {"sent": "🟢", "failed": "🔴", "created": "🟡"}
    lines = ["<b>📋 Мои последние заказы</b>", ""]
    for order in orders:
        lines.append(f"{icons.get(order.status, '•')} <b>#{order.id}</b> — {fmt_dt(order.created_at)}")
    await callback.message.edit_text("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin:market")
async def admin_market(callback: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    target = await db.get_market_merchant_target()
    topic = await db.get_topic('trader')
    connected = await telethon.is_connected()
    text = (
        "<b>🛒 Настройки Торговца Локи</b>\n\n"
        f"👤 Торговец: <code>{escape(target) if target else 'не настроен'}</code>\n"
        f"🔐 Telethon: {'🟢 подключён' if connected else '🔴 не подключён'}\n"
        f"💬 Тема Торговец Локи: {'✅ настроена' if topic else '⚠️ не настроена'}\n\n"
        "Получатель может быть числовым Telegram ID или @username.\n"
        "• ID: бот сначала попробует написать сам; если нельзя — использует Telethon.\n"
        "• @username: используется Telethon.\n\n"
        "Чтобы привязать тему, отправьте /set_trader_topic прямо внутри темы «Торговец Локи»."
    )
    await callback.message.edit_text(text, reply_markup=market_settings_keyboard(bool(target), connected))
    await callback.answer()


@router.callback_query(F.data == "market_settings:merchant")
async def market_merchant_start(callback: CallbackQuery, state: FSMContext, config: Config):
    await state.clear()
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer("Торговец настраивается в группе: /admin → Торговец Локи или ответом /set_gp_merchant на его сообщение.", show_alert=True)


@router.message(MarketSettings.merchant_target)
async def market_merchant_save(message: Message, db: Database, state: FSMContext, config: Config):
    if not is_admin(message.from_user.id, config) or not await ensure_private(message):
        return
    raw = (message.text or "").strip()
    if raw.startswith("https://t.me/"):
        raw = "@" + raw.split("https://t.me/", 1)[1].strip("/").split("/", 1)[0]
    valid = bool(re.fullmatch(r"-?\d+", raw) or re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw))
    if not valid:
        await topic_answer(message, "Введите @username или числовой Telegram ID.")
        return
    await db.set_market_merchant_target(raw)
    await state.clear()
    await topic_answer(message, f"✅ Торговец ГП настроен: <code>{escape(raw)}</code>")


@router.callback_query(F.data == "market_settings:test")
async def market_test(callback: CallbackQuery, db: Database, config: Config, bot: Bot, telethon: TelethonManager):
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    target = await db.get_market_merchant_target()
    if not target:
        await callback.answer("Сначала укажите Торговца ГП.", show_alert=True)
        return
    await callback.answer("Проверяю отправку…")
    try:
        method = await send_to_merchant(
            bot,
            telethon,
            target,
            "🧪 <b>Тест связи XZONA Group Bot</b>\n\nЕсли вы получили это сообщение, доставка заказов Торговца Локи настроена правильно.",
        )
    except Exception as exc:
        await topic_answer(callback.message, f"❌ Тест не прошёл: <code>{escape(str(exc)[:700])}</code>")
        return
    await topic_answer(callback.message, f"✅ Тестовое сообщение отправлено через {'Telegram Bot' if method == 'bot' else 'Telethon'}.")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@router.message(F.text == "📦 Хранилище")
async def storage(message: Message, db: Database, config: Config):
    if not await has_permission(message.from_user.id, "storage.view", db, config):
        await topic_answer(message, "⛔ Этот раздел недоступен для вашей должности.")
        return
    can_manage = await has_permission(message.from_user.id, "storage.manage", db, config)
    await topic_answer(message, "📦 <b>Хранилище</b>", reply_markup=storage_menu(can_manage))


@router.callback_query(F.data == "storage:add")
async def add_item_start(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddItem.player)
    await show_players(callback, db, page=0)


async def show_players(callback: CallbackQuery, db: Database, page: int):
    page_size = 10
    total = await db.count_players()
    players = await db.list_players(limit=page_size, offset=page * page_size)
    if not players:
        await callback.message.edit_text("Пока нет зарегистрированных игроков.")
        await callback.answer()
        return
    await callback.message.edit_text(
        "👤 <b>Выберите владельца предмета:</b>",
        reply_markup=players_keyboard(players, page, total, page_size),
    )
    await callback.answer()


@router.callback_query(AddItem.player, F.data.startswith("add:players_page:"))
async def add_players_page(callback: CallbackQuery, db: Database):
    page = int(callback.data.rsplit(":", 1)[1])
    await show_players(callback, db, page)


@router.callback_query(AddItem.player, F.data.startswith("add:player:"))
async def add_choose_player(callback: CallbackQuery, db: Database, state: FSMContext):
    player_id = int(callback.data.rsplit(":", 1)[1])
    player = await db.get_player(player_id)
    if not player:
        await callback.answer("Игрок не найден", show_alert=True)
        return
    await state.update_data(player_id=player_id, player_nickname=player.game_nickname)
    await state.set_state(AddItem.name)
    recent = await db.recent_item_names()
    await callback.message.edit_text(
        f"👤 Владелец: <b>{escape(player.game_nickname)}</b>\n\n"
        "🎒 <b>Введите название предмета вручную</b>" + (" или выберите недавний:" if recent else ":"),
        reply_markup=recent_names_keyboard(recent),
    )
    await state.update_data(recent_names=recent)
    await callback.answer()


@router.callback_query(AddItem.name, F.data.startswith("add:recent:"))
async def add_recent_name(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    names = data.get("recent_names", [])
    idx = int(callback.data.rsplit(":", 1)[1])
    if idx < 0 or idx >= len(names):
        await callback.answer("Список устарел", show_alert=True)
        return
    await state.update_data(item_name=names[idx])
    await state.set_state(AddItem.quantity)
    await callback.message.edit_text(f"🎒 Предмет: <b>{escape(names[idx])}</b>\n\n🔢 Введите количество:")
    await callback.answer()


@router.message(AddItem.name)
async def add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 1 or len(name) > 80:
        await topic_answer(message, "Название должно быть от 1 до 80 символов.")
        return
    await state.update_data(item_name=name)
    await state.set_state(AddItem.quantity)
    await topic_answer(message, "🔢 Введите количество:")


@router.message(AddItem.quantity)
async def add_quantity(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await topic_answer(message, "Введите целое количество от 1 до 9999:")
        return
    await state.update_data(quantity=int(raw))
    await state.set_state(AddItem.comment)
    await topic_answer(message, "📝 Добавьте комментарий или пропустите:", reply_markup=skip_comment_keyboard())


@router.callback_query(AddItem.comment, F.data == "add:skip_comment")
async def add_skip_comment(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comment=None)
    await state.set_state(AddItem.confirm)
    await show_add_confirm(callback.message, state)
    await callback.answer()


@router.message(AddItem.comment)
async def add_comment(message: Message, state: FSMContext):
    comment = (message.text or "").strip()
    if len(comment) > 500:
        await topic_answer(message, "Комментарий слишком длинный. Максимум 500 символов.")
        return
    await state.update_data(comment=comment or None)
    await state.set_state(AddItem.confirm)
    await show_add_confirm(message, state)


async def show_add_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    await topic_answer(message, 
        "<b>📦 Новый предмет</b>\n\n"
        f"👤 Владелец: <b>{escape(data['player_nickname'])}</b>\n"
        f"🎒 Предмет: <b>{escape(data['item_name'])}</b>\n"
        f"🔢 Количество: <b>{data['quantity']}</b>\n"
        f"📝 Комментарий: {escape(data['comment']) if data.get('comment') else '—'}",
        reply_markup=confirm_add_keyboard(),
    )


@router.callback_query(AddItem.confirm, F.data == "add:confirm")
async def add_confirm(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
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
        "✅ <b>Принято на хранение</b>\n\n" + item_text(item),
        reply_markup=item_keyboard(item.id, True),
    )
    await callback.answer("Сохранено")


@router.callback_query(F.data == "add:cancel")
async def add_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Добавление отменено.")
    await callback.answer()


@router.callback_query(F.data.in_({"storage:list", "storage:history"}))
async def storage_list(callback: CallbackQuery, db: Database, config: Config):
    if not await has_permission(callback.from_user.id, "storage.view", db, config):
        await callback.answer("Этот раздел недоступен для вашей должности.", show_alert=True)
        return
    status = "stored" if callback.data == "storage:list" else "issued"
    items = await db.list_storage_items(status=status, limit=20)
    if not items:
        await callback.message.edit_text("📭 Здесь пока пусто.")
        await callback.answer()
        return
    title = "📋 <b>Сейчас на хранении</b>" if status == "stored" else "📜 <b>Последние выдачи</b>"
    await callback.message.edit_text(title + "\n\nНажмите на предмет, чтобы открыть карточку.", reply_markup=storage_items_keyboard(items))
    await callback.answer()


@router.message(F.text.regexp(r"^/item_(\d+)$"))
async def item_command(message: Message, db: Database, config: Config):
    if not await has_permission(message.from_user.id, "storage.view", db, config):
        await topic_answer(message, "⛔ Этот раздел недоступен для вашей должности.")
        return
    try:
        item_id = int((message.text or "").split("_", 1)[1])
    except Exception:
        return
    item = await db.get_storage_item(item_id)
    if not item:
        await topic_answer(message, "Предмет не найден.")
        return
    await topic_answer(message, item_text(item), reply_markup=item_keyboard(item.id, await has_permission(message.from_user.id, "storage.manage", db, config), item.status == "issued"))


@router.callback_query(F.data.startswith("item:view:"))
async def item_view(callback: CallbackQuery, db: Database, config: Config):
    if not await has_permission(callback.from_user.id, "storage.view", db, config):
        await callback.answer("Этот раздел недоступен для вашей должности.", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await db.get_storage_item(item_id)
    if not item:
        await callback.answer("Предмет не найден", show_alert=True)
        return
    await callback.message.edit_text(item_text(item), reply_markup=item_keyboard(item.id, await has_permission(callback.from_user.id, "storage.manage", db, config), item.status == "issued"))
    await callback.answer()


@router.callback_query(F.data.startswith("item:issue:"))
async def item_issue(callback: CallbackQuery, db: Database, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await db.get_storage_item(item_id)
    if not item or item.status != "stored":
        await callback.answer("Предмет уже выдан или не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"📤 Вы действительно выдаёте <b>{escape(item.item_name)}</b> ×{item.quantity} игроку <b>{escape(item.player_nickname)}</b>?",
        reply_markup=issue_confirm_keyboard(item_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("item:issue_confirm:"))
async def item_issue_confirm(callback: CallbackQuery, db: Database, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    if not await db.issue_item(item_id, callback.from_user.id):
        await callback.answer("Не удалось выдать: предмет уже обработан", show_alert=True)
        return
    item = await db.get_storage_item(item_id)
    await callback.message.edit_text("🟢 <b>ПРЕДМЕТ ВЫДАН</b>\n\n" + item_text(item))
    await callback.answer("Выдано")


@router.callback_query(F.data.startswith("item:delete:"))
async def item_delete(callback: CallbackQuery, db: Database, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await db.get_storage_item(item_id)
    if not item:
        await callback.answer("Не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"🗑 Удалить запись <b>{escape(item.item_name)}</b> игрока <b>{escape(item.player_nickname)}</b>?",
        reply_markup=delete_confirm_keyboard(item_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("item:delete_confirm:"))
async def item_delete_confirm(callback: CallbackQuery, db: Database, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    ok = await db.delete_item(item_id)
    await callback.message.edit_text("🗑 Запись удалена." if ok else "Не удалось удалить запись.")
    await callback.answer()


@router.callback_query(F.data.startswith("item:edit_name:"))
async def edit_name_start(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(EditItem.name)
    await state.update_data(edit_item_id=int(callback.data.rsplit(":", 1)[1]))
    await topic_answer(callback.message, "✏️ Введите новое название предмета:")
    await callback.answer()


@router.message(EditItem.name)
async def edit_name_save(message: Message, db: Database, state: FSMContext):
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await topic_answer(message, "Название должно быть от 1 до 80 символов.")
        return
    data = await state.get_data()
    ok = await db.update_item_name(data["edit_item_id"], name)
    await state.clear()
    item = await db.get_storage_item(data["edit_item_id"])
    await topic_answer(message, "✅ Изменено.\n\n" + item_text(item) if ok else "Не удалось изменить.")


@router.callback_query(F.data.startswith("item:edit_qty:"))
async def edit_qty_start(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(EditItem.quantity)
    await state.update_data(edit_item_id=int(callback.data.rsplit(":", 1)[1]))
    await topic_answer(callback.message, "🔢 Введите новое количество:")
    await callback.answer()


@router.message(EditItem.quantity)
async def edit_qty_save(message: Message, db: Database, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await topic_answer(message, "Введите целое число от 1 до 9999.")
        return
    data = await state.get_data()
    ok = await db.update_item_quantity(data["edit_item_id"], int(raw))
    await state.clear()
    item = await db.get_storage_item(data["edit_item_id"])
    await topic_answer(message, "✅ Изменено.\n\n" + item_text(item) if ok else "Не удалось изменить.")


@router.callback_query(F.data.startswith("item:edit_comment:"))
async def edit_comment_start(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not await has_permission(callback.from_user.id, "storage.manage", db, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(EditItem.comment)
    await state.update_data(edit_item_id=int(callback.data.rsplit(":", 1)[1]))
    await topic_answer(callback.message, "📝 Введите новый комментарий. Отправьте <code>-</code>, чтобы очистить:")
    await callback.answer()


@router.message(EditItem.comment)
async def edit_comment_save(message: Message, db: Database, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) > 500:
        await topic_answer(message, "Максимум 500 символов.")
        return
    data = await state.get_data()
    comment = None if text == "-" else text
    ok = await db.update_item_comment(data["edit_item_id"], comment)
    await state.clear()
    item = await db.get_storage_item(data["edit_item_id"])
    await topic_answer(message, "✅ Изменено.\n\n" + item_text(item) if ok else "Не удалось изменить.")


# ---------------------------------------------------------------------------
# Players/help/fallback
# ---------------------------------------------------------------------------

@router.message(F.text == "👥 Игроки")
async def players(message: Message, db: Database, config: Config):
    if not is_admin(message.from_user.id, config):
        await topic_answer(message, "👥 Этот раздел доступен администраторам.")
        return
    rows = await db.list_players(limit=50)
    if not rows:
        await topic_answer(message, "Игроков пока нет.")
        return
    lines = [f"👥 <b>Игроки ({len(rows)})</b>", ""]
    for p in rows:
        u = f"@{escape(p.username)}" if p.username else "без username"
        lines.append(f"• <b>{escape(p.game_nickname)}</b> — {u}")
    await topic_answer(message, "\n".join(lines))


@router.message(F.text == "ℹ️ Помощь")
@router.message(Command("help"))
async def help_message(message: Message, config: Config):
    text = (
        "<b>🎮 XZONA Group Bot</b>\n\n"
        "👤 Игрок: регистрация игрового ника и просмотр своих предметов.\n"
        "📦 Хранилище: учёт вещей, принятых на хранение.\n"
        "🛒 Торговец Локи: формирование заказа и отправка Торговцу ГП.\n"
        "👥 Ники автоматически берутся из настроенной темы Telegram.\n"
        "🔐 Старые ники можно импортировать через Telethon прямо из админ-меню."
    )
    if is_admin(message.from_user.id, config):
        text += "\n\n🛡 Вам доступно меню ⚙️ Администрирование."
    await topic_answer(message, text)


@router.message()
async def fallback(message: Message, db: Database, config: Config):
    outcome = await sync_nick_message(message, db)
    if outcome.handled:
        if outcome.error:
            await temp_answer(message, f"⚠️ {outcome.error}", ttl=90)
        elif outcome.notice:
            await temp_answer(message, outcome.notice, ttl=120)
            if outcome.request_id:
                await publish_role_request_card(message.bot, db, outcome.request_id)
        return
    if message.chat.type == "private":
        await topic_answer(message, "Рабочее управление выполняется внутри тематических разделов игровой группы. Для справки используйте /help.")

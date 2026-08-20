from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .config import Config
from .db import Database, StorageItem
from .keyboards import (
    MAIN_MENU,
    confirm_add_keyboard,
    delete_confirm_keyboard,
    item_keyboard,
    issue_confirm_keyboard,
    players_keyboard,
    recent_names_keyboard,
    skip_comment_keyboard,
    storage_menu,
    storage_items_keyboard,
)
from .states import AddItem, EditItem, RegisterPlayer

router = Router()


def is_admin(user_id: int, config: Config) -> bool:
    return user_id in config.admin_ids


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


async def ensure_registered(message: Message, db: Database, state: FSMContext) -> bool:
    player = await db.get_player(message.from_user.id)
    if player:
        return True
    await state.set_state(RegisterPlayer.nickname)
    await message.answer("👤 Сначала укажите ваш <b>ник в игре</b>:")
    return False


@router.message(CommandStart())
async def start(message: Message, db: Database, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player:
        await state.set_state(RegisterPlayer.nickname)
        await message.answer(
            "Привет! Для работы с группой сначала зарегистрируем игровой ник.\n\n"
            "👤 Введите ваш <b>ник в XZONA</b>:"
        )
        return
    await message.answer(f"🎮 Привет, <b>{escape(player.game_nickname)}</b>!", reply_markup=MAIN_MENU)


@router.message(RegisterPlayer.nickname)
async def register_nickname(message: Message, db: Database, state: FSMContext):
    nickname = (message.text or "").strip()
    if len(nickname) < 2 or len(nickname) > 40:
        await message.answer("Ник должен быть длиной от 2 до 40 символов. Попробуйте ещё раз:")
        return
    if await db.nickname_exists_for_other(message.from_user.id, nickname):
        await message.answer("Такой игровой ник уже зарегистрирован. Введите другой:")
        return
    await db.upsert_player(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        game_nickname=nickname,
    )
    await state.clear()
    await message.answer(f"✅ Ник сохранён: <b>{escape(nickname)}</b>", reply_markup=MAIN_MENU)


@router.message(F.text == "👤 Мой профиль")
async def my_profile(message: Message, db: Database, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player:
        await state.set_state(RegisterPlayer.nickname)
        await message.answer("👤 Введите ваш игровой ник:")
        return
    items = await db.list_player_items(message.from_user.id)
    await message.answer(
        f"👤 <b>{escape(player.game_nickname)}</b>\n"
        f"Telegram: @{escape(player.username) if player.username else '—'}\n"
        f"📦 Сейчас на хранении: <b>{len(items)}</b>\n\n"
        "Чтобы сменить ник: /nickname"
    )


@router.message(Command("nickname"))
async def change_nickname(message: Message, state: FSMContext):
    await state.set_state(RegisterPlayer.nickname)
    await message.answer("Введите новый игровой ник:")


@router.message(F.text == "📦 Хранилище")
async def storage(message: Message, config: Config):
    await message.answer("📦 <b>Хранилище</b>", reply_markup=storage_menu(is_admin(message.from_user.id, config)))


@router.callback_query(F.data == "storage:add")
async def add_item_start(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not is_admin(callback.from_user.id, config):
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
        "🎒 <b>Введите название предмета вручную</b>"
        + (" или выберите недавний:" if recent else ":"),
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
        await message.answer("Название должно быть от 1 до 80 символов.")
        return
    await state.update_data(item_name=name)
    await state.set_state(AddItem.quantity)
    await message.answer("🔢 Введите количество:")


@router.message(AddItem.quantity)
async def add_quantity(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await message.answer("Введите целое количество от 1 до 9999:")
        return
    await state.update_data(quantity=int(raw))
    await state.set_state(AddItem.comment)
    await message.answer("📝 Добавьте комментарий или пропустите:", reply_markup=skip_comment_keyboard())


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
        await message.answer("Комментарий слишком длинный. Максимум 500 символов.")
        return
    await state.update_data(comment=comment or None)
    await state.set_state(AddItem.confirm)
    await show_add_confirm(message, state)


async def show_add_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer(
        "<b>📦 Новый предмет</b>\n\n"
        f"👤 Владелец: <b>{escape(data['player_nickname'])}</b>\n"
        f"🎒 Предмет: <b>{escape(data['item_name'])}</b>\n"
        f"🔢 Количество: <b>{data['quantity']}</b>\n"
        f"📝 Комментарий: {escape(data['comment']) if data.get('comment') else '—'}",
        reply_markup=confirm_add_keyboard(),
    )


@router.callback_query(AddItem.confirm, F.data == "add:confirm")
async def add_confirm(callback: CallbackQuery, db: Database, state: FSMContext, config: Config):
    if not is_admin(callback.from_user.id, config):
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
    status = "stored" if callback.data == "storage:list" else "issued"
    items = await db.list_storage_items(status=status, limit=20)
    if not items:
        await callback.message.edit_text("📭 Здесь пока пусто.")
        await callback.answer()
        return
    title = "📋 <b>Сейчас на хранении</b>" if status == "stored" else "📜 <b>Последние выдачи</b>"
    subtitle = "Нажмите на предмет, чтобы открыть карточку."
    await callback.message.edit_text(
        f"{title}\n\n{subtitle}",
        reply_markup=storage_items_keyboard(items),
    )
    await callback.answer()


@router.message(F.text.regexp(r"^/item_(\d+)$"))
async def item_command(message: Message, db: Database, config: Config):
    try:
        item_id = int((message.text or "").split("_", 1)[1])
    except Exception:
        return
    item = await db.get_storage_item(item_id)
    if not item:
        await message.answer("Предмет не найден.")
        return
    await message.answer(
        item_text(item),
        reply_markup=item_keyboard(item.id, is_admin(message.from_user.id, config), item.status == "issued"),
    )


@router.callback_query(F.data.startswith("item:view:"))
async def item_view(callback: CallbackQuery, db: Database, config: Config):
    item_id = int(callback.data.rsplit(":", 1)[1])
    item = await db.get_storage_item(item_id)
    if not item:
        await callback.answer("Предмет не найден", show_alert=True)
        return
    await callback.message.edit_text(
        item_text(item),
        reply_markup=item_keyboard(item.id, is_admin(callback.from_user.id, config), item.status == "issued"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("item:issue:"))
async def item_issue(callback: CallbackQuery, db: Database, config: Config):
    if not is_admin(callback.from_user.id, config):
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
    if not is_admin(callback.from_user.id, config):
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
    if not is_admin(callback.from_user.id, config):
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
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    ok = await db.delete_item(item_id)
    await callback.message.edit_text("🗑 Запись удалена." if ok else "Не удалось удалить запись.")
    await callback.answer()


@router.callback_query(F.data.startswith("item:edit_name:"))
async def edit_name_start(callback: CallbackQuery, state: FSMContext, config: Config):
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(EditItem.name)
    await state.update_data(edit_item_id=item_id)
    await callback.message.answer("✏️ Введите новое название предмета:")
    await callback.answer()


@router.message(EditItem.name)
async def edit_name_save(message: Message, db: Database, state: FSMContext):
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 80:
        await message.answer("Название должно быть от 1 до 80 символов.")
        return
    data = await state.get_data()
    ok = await db.update_item_name(data["edit_item_id"], name)
    await state.clear()
    item = await db.get_storage_item(data["edit_item_id"])
    await message.answer("✅ Изменено.\n\n" + item_text(item) if ok else "Не удалось изменить.")


@router.callback_query(F.data.startswith("item:edit_qty:"))
async def edit_qty_start(callback: CallbackQuery, state: FSMContext, config: Config):
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(EditItem.quantity)
    await state.update_data(edit_item_id=item_id)
    await callback.message.answer("🔢 Введите новое количество:")
    await callback.answer()


@router.message(EditItem.quantity)
async def edit_qty_save(message: Message, db: Database, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 9999:
        await message.answer("Введите целое число от 1 до 9999.")
        return
    data = await state.get_data()
    ok = await db.update_item_quantity(data["edit_item_id"], int(raw))
    await state.clear()
    item = await db.get_storage_item(data["edit_item_id"])
    await message.answer("✅ Изменено.\n\n" + item_text(item) if ok else "Не удалось изменить.")


@router.callback_query(F.data.startswith("item:edit_comment:"))
async def edit_comment_start(callback: CallbackQuery, state: FSMContext, config: Config):
    if not is_admin(callback.from_user.id, config):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    item_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(EditItem.comment)
    await state.update_data(edit_item_id=item_id)
    await callback.message.answer("📝 Введите новый комментарий. Отправьте <code>-</code>, чтобы очистить:")
    await callback.answer()


@router.message(EditItem.comment)
async def edit_comment_save(message: Message, db: Database, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) > 500:
        await message.answer("Максимум 500 символов.")
        return
    data = await state.get_data()
    comment = None if text == "-" else text
    ok = await db.update_item_comment(data["edit_item_id"], comment)
    await state.clear()
    item = await db.get_storage_item(data["edit_item_id"])
    await message.answer("✅ Изменено.\n\n" + item_text(item) if ok else "Не удалось изменить.")


@router.message(F.text == "👥 Игроки")
async def players(message: Message, db: Database, config: Config):
    if not is_admin(message.from_user.id, config):
        await message.answer("👥 Этот раздел доступен администраторам.")
        return
    rows = await db.list_players(limit=50)
    if not rows:
        await message.answer("Игроков пока нет.")
        return
    lines = [f"👥 <b>Игроки ({len(rows)})</b>", ""]
    for p in rows:
        u = f"@{escape(p.username)}" if p.username else "без username"
        lines.append(f"• <b>{escape(p.game_nickname)}</b> — {u}")
    await message.answer("\n".join(lines))


@router.message(F.text == "ℹ️ Помощь")
@router.message(Command("help"))
async def help_message(message: Message, config: Config):
    text = (
        "<b>🎮 XZONA Group Bot</b>\n\n"
        "👤 Игрок: регистрация игрового ника и просмотр своих предметов.\n"
        "📦 Хранилище: учёт вещей, принятых на хранение.\n"
        "🟡 На хранении → 🟢 Выдано с сохранением истории.\n"
    )
    if is_admin(message.from_user.id, config):
        text += "\n🛡 Вы администратор: вам доступно добавление, редактирование и выдача предметов."
    await message.answer(text)


@router.message()
async def fallback(message: Message):
    await message.answer("Используйте кнопки меню 👇", reply_markup=MAIN_MENU)

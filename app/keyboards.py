from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Хранилище"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="👥 Игроки"), KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)


def storage_menu(admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if admin:
        b.button(text="➕ Принять предмет", callback_data="storage:add")
    b.button(text="📋 Сейчас на хранении", callback_data="storage:list")
    b.button(text="📜 История выдач", callback_data="storage:history")
    b.adjust(1)
    return b.as_markup()


def players_keyboard(players, page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in players:
        username = f" @{p.username}" if p.username else ""
        b.button(text=f"{p.game_nickname}{username}", callback_data=f"add:player:{p.telegram_id}")
    if page > 0:
        b.button(text="⬅️", callback_data=f"add:players_page:{page-1}")
    if (page + 1) * page_size < total:
        b.button(text="➡️", callback_data=f"add:players_page:{page+1}")
    b.adjust(1)
    return b.as_markup()


def recent_names_keyboard(names: list[str]) -> InlineKeyboardMarkup | None:
    if not names:
        return None
    b = InlineKeyboardBuilder()
    for idx, name in enumerate(names):
        b.button(text=name, callback_data=f"add:recent:{idx}")
    b.adjust(2)
    return b.as_markup()


def skip_comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭ Без комментария", callback_data="add:skip_comment")]])


def confirm_add_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять на хранение", callback_data="add:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="add:cancel")],
        ]
    )


def item_keyboard(item_id: int, admin: bool, issued: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if admin and not issued:
        b.button(text="✅ Выдать игроку", callback_data=f"item:issue:{item_id}")
        b.button(text="✏️ Название", callback_data=f"item:edit_name:{item_id}")
        b.button(text="🔢 Количество", callback_data=f"item:edit_qty:{item_id}")
        b.button(text="📝 Комментарий", callback_data=f"item:edit_comment:{item_id}")
        b.button(text="🗑 Удалить", callback_data=f"item:delete:{item_id}")
    b.adjust(1)
    return b.as_markup()


def issue_confirm_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, выдать", callback_data=f"item:issue_confirm:{item_id}")],
            [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"item:view:{item_id}")],
        ]
    )


def delete_confirm_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"item:delete_confirm:{item_id}")],
            [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"item:view:{item_id}")],
        ]
    )


def storage_items_keyboard(items) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item in items:
        icon = "🟡" if item.status == "stored" else "🟢"
        title = f"{icon} #{item.id} {item.item_name} ×{item.quantity} — {item.player_nickname}"
        if len(title) > 62:
            title = title[:59] + "…"
        b.button(text=title, callback_data=f"item:view:{item.id}")
    b.adjust(1)
    return b.as_markup()

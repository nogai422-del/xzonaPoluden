from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .roles import FACTIONS, POSITIONS


def main_menu(admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🎒 Снаряжение группировки"), KeyboardButton(text="🛒 Рынок ГП")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="👥 Игроки")],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Администрирование")])
    rows.append([KeyboardButton(text="ℹ️ Помощь")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# Backwards-compatible constant for code that may still import it.
MAIN_MENU = main_menu(False)


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


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Telethon", callback_data="admin:telethon")],
            [InlineKeyboardButton(text="👥 Ники игроков", callback_data="admin:nicks")],
            [InlineKeyboardButton(text="🛒 Рынок ГП", callback_data="admin:market")],
        ]
    )


def telethon_menu(connected: bool) -> InlineKeyboardMarkup:
    rows = []
    if connected:
        rows.append([InlineKeyboardButton(text="🔄 Импорт старых ников", callback_data="telethon:sync_nicks")])
        rows.append([InlineKeyboardButton(text="🔌 Переподключить", callback_data="telethon:setup")])
        rows.append([InlineKeyboardButton(text="🗑 Отключить Telethon", callback_data="telethon:disconnect")])
    else:
        rows.append([InlineKeyboardButton(text="🔗 Подключить Telethon", callback_data="telethon:setup")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nicks_admin_menu(can_sync: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_sync:
        rows.append([InlineKeyboardButton(text="🔄 Импортировать старые ники", callback_data="telethon:sync_nicks")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def market_menu(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Новый заказ", callback_data="market:new")],
        [InlineKeyboardButton(text="📋 Мои заказы", callback_data="market:mine")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(text="⚙️ Настройки рынка", callback_data="admin:market")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def market_cart_keyboard(has_items: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить позицию", callback_data="market:add_more")]]
    if has_items:
        rows.append([InlineKeyboardButton(text="📝 Комментарий", callback_data="market:comment")])
        rows.append([InlineKeyboardButton(text="✅ Оформить и отправить", callback_data="market:submit")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="market:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def market_comment_skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ Без комментария", callback_data="market:comment_skip")]]
    )


def market_settings_keyboard(merchant_configured: bool, telethon_connected: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Указать Торговца ГП", callback_data="market_settings:merchant")],
    ]
    if merchant_configured:
        rows.append([InlineKeyboardButton(text="🧪 Тестовая отправка", callback_data="market_settings:test")])
    rows.append([InlineKeyboardButton(text="🔐 Telethon", callback_data="admin:telethon")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def market_topic_panel(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Создать заказ", url=f"https://t.me/{bot_username}?start=market")]
        ]
    )


# ---------------------------------------------------------------------------
# Group-first interface (forum topics)
# ---------------------------------------------------------------------------

def group_storage_panel() -> InlineKeyboardMarkup:
    from .community_views import storage_panel
    return storage_panel()


def legacy_group_storage_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Принять предмет", callback_data="gstorage:add")],
            [InlineKeyboardButton(text="📋 На хранении", callback_data="gstorage:list")],
            [InlineKeyboardButton(text="📜 История выдач", callback_data="gstorage:history")],
        ]
    )


def group_players_keyboard(players, page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in players:
        username = f" @{p.username}" if p.username else ""
        b.button(text=f"{p.game_nickname}{username}", callback_data=f"gstorage:player:{p.telegram_id}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"gstorage:players_page:{page-1}"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"gstorage:players_page:{page+1}"))
    rows = [[InlineKeyboardButton(text=f"{p.game_nickname}{(' @' + p.username) if p.username else ''}", callback_data=f"gstorage:player:{p.telegram_id}")] for p in players]
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_recent_names_keyboard(names: list[str]) -> InlineKeyboardMarkup | None:
    if not names:
        return None
    rows = []
    for idx in range(0, len(names), 2):
        rows.append([
            InlineKeyboardButton(text=name, callback_data=f"gstorage:recent:{i}")
            for i, name in enumerate(names[idx:idx+2], start=idx)
        ])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_quantity_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"{prefix}:qty:1"),
                InlineKeyboardButton(text="2", callback_data=f"{prefix}:qty:2"),
                InlineKeyboardButton(text="3", callback_data=f"{prefix}:qty:3"),
                InlineKeyboardButton(text="5", callback_data=f"{prefix}:qty:5"),
            ],
            [InlineKeyboardButton(text="⌨️ Другое количество", callback_data=f"{prefix}:qty:custom")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")],
        ]
    )


def group_storage_comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Без комментария", callback_data="gstorage:comment_skip")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")],
        ]
    )


def group_storage_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять на хранение", callback_data="gstorage:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")],
        ]
    )


def group_market_panel() -> InlineKeyboardMarkup:
    from .community_views import market_panel
    return market_panel()


def legacy_group_market_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Новый заказ", callback_data="gmarket:new")],
            [InlineKeyboardButton(text="📋 Мои заказы", callback_data="gmarket:mine")],
        ]
    )


def group_market_cart_keyboard(has_items: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить позицию", callback_data="gmarket:add_more")]]
    if has_items:
        rows.append([InlineKeyboardButton(text="📝 Комментарий", callback_data="gmarket:comment")])
        rows.append([InlineKeyboardButton(text="✅ Отправить Торговцу ГП", callback_data="gmarket:submit")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_market_comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Без комментария", callback_data="gmarket:comment_skip")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="gflow:cancel")],
        ]
    )


def market_order_status_keyboard(order_id: int, workflow_status: str) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if workflow_status == "pending":
        rows.append([InlineKeyboardButton(text="✅ Принять заказ", callback_data=f"gorder:accepted:{order_id}")])
        rows.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"gorder:rejected:{order_id}")])
    elif workflow_status == "accepted":
        rows.append([InlineKeyboardButton(text="📦 Заказ собран", callback_data=f"gorder:assembled:{order_id}")])
        rows.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"gorder:rejected:{order_id}")])
    elif workflow_status == "assembled":
        rows.append([InlineKeyboardButton(text="🚚 Выдан игроку", callback_data=f"gorder:issued:{order_id}")])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎒 Снаряжение группировки", callback_data="gadmin:storage")],
            [InlineKeyboardButton(text="👥 Ники игроков", callback_data="gadmin:nicks")],
            [InlineKeyboardButton(text="🎖 Роли и доступ", callback_data="gadmin:roles")],
            [InlineKeyboardButton(text="🛒 Рынок ГП", callback_data="gadmin:market")],
            [InlineKeyboardButton(text="🧩 Разделы и запуск", callback_data="gadmin:system")],
            [InlineKeyboardButton(text="🔐 Telethon", callback_data="gadmin:telethon")],
        ]
    )


def group_admin_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="gadmin:home")]]
    )


def group_telethon_menu(*, connected: bool, can_manage: bool, can_sync: bool, can_sync_members: bool = False, bot_username: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if connected:
        if can_sync_members:
            rows.append([InlineKeyboardButton(text="👥 Синхронизировать участников", callback_data="gtelethon:sync_members")])
            rows.append([InlineKeyboardButton(text="📋 Состав группы", callback_data="gtelethon:members")])
        if can_sync:
            rows.append([InlineKeyboardButton(text="🔄 Импортировать старые ники", callback_data="telethon:sync_nicks")])
        if can_manage:
            rows.append([InlineKeyboardButton(text="🗑 Отключить Telethon", callback_data="gtelethon:disconnect")])
    elif can_manage:
        rows.append([InlineKeyboardButton(text="🪟 Открыть окно авторизации", callback_data="gtelethon:web_auth")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="gadmin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_nicks_admin_menu(*, connected: bool, can_manage: bool, topic_ready: bool, bot_username: str | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🎖 Назначить должности", callback_data="gadmin:roles")]
    ]
    if connected and topic_ready:
        rows.append([InlineKeyboardButton(text="🔄 Импортировать старые ники", callback_data="telethon:sync_nicks")])
    elif not connected and can_manage:
        rows.append([InlineKeyboardButton(text="🪟 Авторизовать Telethon", callback_data="gtelethon:web_auth")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="gadmin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_market_admin_menu(merchant_configured: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="👤 Указать Торговца ГП", callback_data="gmarket_settings:merchant")]]
    if merchant_configured:
        rows.append([InlineKeyboardButton(text="🧪 Тестовая отправка", callback_data="gmarket_settings:test")])
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="gadmin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_role_requests_keyboard(requests) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for req in requests[:20]:
        label = f"#{req.id} {req.player_nickname} — {req.requested_label}"
        if len(label) > 62:
            label = label[:59] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"grole:view:{req.id}")])
    rows.append([InlineKeyboardButton(text="↩️ К ролям", callback_data="gadmin:roles")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_role_request_review_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"grole:approve:{request_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"grole:reject:{request_id}"),
            ],
            [InlineKeyboardButton(text="↩️ К заявкам", callback_data="grole:requests")],
        ]
    )


def group_role_admin_keyboard(*, unassigned_count: int, pending_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"👤 Без должности ({unassigned_count})", callback_data="grole:unassigned:0")],
            [InlineKeyboardButton(text="👥 Все игроки", callback_data="grole:all:0")],
            [InlineKeyboardButton(text=f"⏳ Заявки ({pending_count})", callback_data="grole:requests")],
            [InlineKeyboardButton(text="🔎 Найти игрока", callback_data="grole:search")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="gadmin:home")],
        ]
    )


def group_role_players_keyboard(players, *, page: int, total: int, mode: str, page_size: int = 10) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in players:
        status = "✅" if getattr(p, "position_status", None) == "approved" and getattr(p, "position_code", None) else "⚠️"
        label = f"{status} {p.game_nickname}"
        if len(label) > 58:
            label = label[:55] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"grole:player:{p.telegram_id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"grole:{mode}:{page-1}"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"grole:{mode}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="↩️ К ролям", callback_data="gadmin:roles")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_role_player_keyboard(
    player_id: int,
    *,
    available_internal_codes: list[str],
    current_position_code: str | None,
    has_role: bool,
) -> InlineKeyboardMarkup:
    icons = {
        "leader": "👑",
        "deputy_leader": "⭐",
        "trader": "💰",
        "diplomat": "🤝",
        "storekeeper": "📦",
        "sho_commander": "⚔️",
        "private": "🪖",
    }
    rows: list[list[InlineKeyboardButton]] = []
    for code in available_internal_codes:
        if code == current_position_code:
            continue
        spec = POSITIONS[code]
        rows.append([InlineKeyboardButton(text=f"{icons.get(code, '🎖')} {spec.label}", callback_data=f"grole:set:{player_id}:{code}")])
    rows.append([InlineKeyboardButton(text="🌐 Внешняя группировка", callback_data=f"grole:ext:{player_id}")])
    if has_role:
        rows.append([InlineKeyboardButton(text="🗑 Снять должность", callback_data=f"grole:clear:{player_id}")])
    rows.append([InlineKeyboardButton(text="↩️ К ролям", callback_data="gadmin:roles")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_external_factions_keyboard(player_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"grole:extf:{player_id}:{code}")]
        for code, label in FACTIONS.items()
    ]
    rows.append([InlineKeyboardButton(text="↩️ К игроку", callback_data=f"grole:player:{player_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_external_role_keyboard(player_id: int, faction_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👑 Лидер", callback_data=f"grole:setext:{player_id}:external_leader:{faction_code}")],
            [InlineKeyboardButton(text="⭐ Заместитель", callback_data=f"grole:setext:{player_id}:external_deputy:{faction_code}")],
            [InlineKeyboardButton(text="↩️ К группировкам", callback_data=f"grole:ext:{player_id}")],
        ]
    )


def group_role_search_results_keyboard(players) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in players[:20]:
        label = p.game_nickname
        if p.username:
            label += f" (@{p.username})"
        if len(label) > 58:
            label = label[:55] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"grole:player:{p.telegram_id}")])
    rows.append([InlineKeyboardButton(text="↩️ К ролям", callback_data="gadmin:roles")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

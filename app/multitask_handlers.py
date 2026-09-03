from __future__ import annotations

from datetime import datetime
from html import escape
import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ChatMemberUpdated

from .config import Config
from .db import Database
from .group_handlers import GROUP_TYPES, WORKFLOW_LABELS, flow_edit_from_message, has_permission, market_order_group_text, refresh_order_cards, safe_delete
from .housekeeping import cleanup_all_tracked_messages, delete_incoming_later, schedule_delete, temp_answer, temp_callback_message, topic_answer
from .roles import INTERNAL_POSITION_ORDER, POSITIONS, ROLE_CAPACITIES, is_external_position
from .states import GroupDiplomacy, GroupEventCreate, GroupGpStock, GroupInfoCreate, GroupTargetCreate
from .telethon_manager import TelethonManager

router = Router(name="multitask_v7")
ADMIN_CHAT_TYPES = set(GROUP_TYPES) | {"private"}
ANNOUNCE_VERSION = "v7.6"

TOPICS: dict[str, dict[str, str]] = {
    "general": {"label": "General", "emoji": "💬"},
    "nicks": {"label": "Ники игроков", "emoji": "🔑"},
    "storage": {"label": "Снаряжение группировки", "emoji": "🎒"},
    "market": {"label": "Рынок ГП", "emoji": "🪙"},
    "delivery": {"label": "Пункт выдачи заказов", "emoji": "⚛️"},
    "gp_stock": {"label": "Снаряжение ГП", "emoji": "🪖"},
    "events": {"label": "Мероприятия", "emoji": "🎮"},
    "diplomacy": {"label": "Союзы и Война", "emoji": "🤝"},
    "targets": {"label": "Список целей", "emoji": "🏴"},
    "news": {"label": "Инфа в Зоне (Новости)", "emoji": "📈"},
    "info": {"label": "Прочая информация", "emoji": "🗺"},
    "bar": {"label": "Бар «Гильза»", "emoji": "🍻"},
}

REL = {"ally": "🟢 Союз", "neutral": "⚪ Нейтралитет", "war": "🔴 Война"}


@router.my_chat_member()
async def bot_joined_group(event: ChatMemberUpdated, db: Database, bot: Bot):
    """Visible first-contact message when the bot is added to a group.

    Topic-specific instructions are posted after topic mapping. This message lands
    in General/default discussion so even members who were already in the group
    immediately see that the automation has been enabled.
    """
    if event.chat.type not in GROUP_TYPES:
        return
    old_status = getattr(event.old_chat_member, "status", None)
    new_status = getattr(event.new_chat_member, "status", None)
    active = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
    if new_status not in active or old_status in active:
        return
    key = f"joined_announce:{event.chat.id}:{ANNOUNCE_VERSION}"
    if await db.get_setting(key):
        return
    try:
        await bot.send_message(
            event.chat.id,
            "<b>🤖 XZONA Group Bot подключён</b>\n\n"
            "Я начинаю обслуживать разделы этой игровой группы. После первоначальной настройки в каждой рабочей теме появится собственная инструкция и кнопки.\n\n"
            "Руководству: откройте <code>/admin</code>, подключите Telethon и вручную назначьте каждый раздел командой <code>/set_..._topic</code> прямо внутри нужной темы. "
            "Автоматического определения разделов больше нет — бот использует только те привязки, которые назначило руководство.\n\n"
            "После настройки уже присутствующим игрокам ничего переустанавливать не нужно — они увидят инструкции прямо в темах."
        )
        await db.set_setting(key, datetime.utcnow().isoformat())
    except Exception:
        pass
TARGET_STATUS = {"active": "🔴 Активна", "taken": "🟡 В работе", "done": "✅ Выполнена", "cancelled": "❌ Отменена"}
EVENT_STATUS = {"open": "🟢 Запись открыта", "closed": "🟡 Запись закрыта", "done": "✅ Завершено", "cancelled": "❌ Отменено"}


def _thread(message: Message) -> int:
    return int(message.message_thread_id or 0)


def _topic_kwargs(thread_id: int) -> dict:
    return {"message_thread_id": thread_id} if thread_id else {}


async def _internal_user(user_id: int, db: Database, config: Config) -> bool:
    if user_id in config.admin_ids:
        return True
    p = await db.get_player(user_id)
    return bool(p and p.position_status == "approved" and not is_external_position(p.position_code))


def topic_intro(code: str) -> tuple[str, InlineKeyboardMarkup | None]:
    base = {
        "general": (
            "<b>🤖 XZONA Group Bot запущен</b>\n\n"
            "Бот обслуживает рабочие разделы группы, роли, заказы, склад, мероприятия, дипломатию и цели. "
            "Личное меню игрокам не требуется — используйте кнопки и инструкции внутри соответствующих тем.\n\n"
            "Представители других группировок могут общаться здесь; в рабочих разделах их сообщения автоматически убираются."
        ),
        "nicks": (
            "<b>🔑 Ники игроков — регистрация</b>\n\n"
            "Каждый участник, включая тех, кто уже находится в группе, должен иметь профиль. Напишите одним сообщением две строки:\n"
            "<code>ИгровойНик\nДолжность</code>\n\n"
            "Например: <code>Xan2795\nКладовщик</code>. Должность включается после подтверждения руководством. Старые записи можно импортировать через Telethon."
        ),
        "storage": (
            "<b>🎒 Снаряжение группировки</b>\n\n"
            "Кладовщик/руководство принимает имущество на хранение и отмечает выдачу. Все действия выполняются прямо в этой теме."
        ),
        "market": (
            "<b>🪙 Рынок ГП</b>\n\n"
            "Участники формируют заказы здесь. Торговец принимает заказ, отмечает сборку, после чего он автоматически появляется в Пункте выдачи."
        ),
        "delivery": (
            "<b>⚛️ Пункт выдачи заказов</b>\n\n"
            "Здесь появляются только собранные заказы. Торговец или руководство отмечает фактическую выдачу игроку."
        ),
        "gp_stock": (
            "<b>🪖 Снаряжение ГП</b>\n\n"
            "Каталог и остатки снаряжения ГП. Торговец и руководство изменяют количество, остальные участники могут смотреть наличие."
        ),
        "events": (
            "<b>🎮 Мероприятия</b>\n\n"
            "Командир ШО/руководство создаёт мероприятия. Участники записываются кнопкой, список и лимит обновляются автоматически."
        ),
        "diplomacy": (
            "<b>🤝 Союзы и Война</b>\n\n"
            "Дипломат/руководство ведёт актуальные отношения с группировками: союз, нейтралитет или война."
        ),
        "targets": (
            "<b>🏴 Список целей</b>\n\n"
            "Командир ШО/руководство создаёт цели, назначает исполнение и закрывает выполненные записи."
        ),
        "news": (
            "<b>📈 Инфа в Зоне / Новости</b>\n\n"
            "Официальная оперативная лента. Разрешённые редакторы публикуют записи; также можно подключить автоматический перенос из другой темы/группы."
        ),
        "info": (
            "<b>🗺 Прочая информация</b>\n\n"
            "База полезной информации. Дипломат/руководство добавляет записи, при необходимости подключается автоматический перенос из другого раздела."
        ),
        "bar": (
            "<b>🍻 Бар «Гильза»</b>\n\n"
            "Свободное общение. Бот здесь почти не вмешивается и не превращает беседу в служебный раздел."
        ),
    }[code]
    buttons: list[list[InlineKeyboardButton]] = []
    if code == "storage": buttons = [[InlineKeyboardButton(text="🎒 Открыть хранилище", callback_data="gstorage:list")]]
    elif code == "market": buttons = [[InlineKeyboardButton(text="🛒 Новый заказ", callback_data="gmarket:new"), InlineKeyboardButton(text="📋 Мои заказы", callback_data="gmarket:mine")]]
    elif code == "delivery": buttons = [[InlineKeyboardButton(text="📦 Готовые заказы", callback_data="v7delivery:list")]]
    elif code == "gp_stock": buttons = [[InlineKeyboardButton(text="📋 Остатки", callback_data="v7stock:list"), InlineKeyboardButton(text="✏️ Изменить", callback_data="v7stock:set")]]
    elif code == "events": buttons = [[InlineKeyboardButton(text="📅 Ближайшие", callback_data="v7events:list"), InlineKeyboardButton(text="➕ Создать", callback_data="v7events:new")]]
    elif code == "diplomacy": buttons = [
        [InlineKeyboardButton(text="📋 Отношения", callback_data="v7dip:list"), InlineKeyboardButton(text="🧭 Группировки", callback_data="v7dip:new")],
        [InlineKeyboardButton(text="📜 История", callback_data="v7dip:history")],
    ]
    elif code == "targets": buttons = [[InlineKeyboardButton(text="🎯 Активные цели", callback_data="v7targets:list"), InlineKeyboardButton(text="➕ Добавить", callback_data="v7targets:new")]]
    elif code in {"news","info"}: buttons = [[InlineKeyboardButton(text="📚 Последние записи", callback_data=f"v7info:list:{code}"), InlineKeyboardButton(text="➕ Добавить", callback_data=f"v7info:new:{code}")]]
    return base, InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


async def announce_topic(bot: Bot, db: Database, code: str, *, force: bool = False) -> bool:
    """Create or refresh one persistent instruction/panel message per topic.

    The message id is remembered. /announce_all and version upgrades edit the same
    message instead of stacking duplicates in the chat.
    """
    topic = await db.get_topic(code)
    if not topic:
        return False
    chat_id, thread_id = topic
    key = f"announce:{ANNOUNCE_VERSION}:{code}:{chat_id}:{thread_id}"
    msg_key = f"announce_message:{code}:{chat_id}:{thread_id}"
    already_current = bool(await db.get_setting(key))
    text, markup = topic_intro(code)
    if code == "nicks":
        counts = await db.position_counts()
        role_lines: list[str] = []
        icons = {
            "leader": "👑",
            "deputy_leader": "⭐",
            "trader": "💰",
            "diplomat": "🤝",
            "storekeeper": "📦",
            "sho_commander": "⚔️",
            "private": "🪖",
        }
        for position_code in INTERNAL_POSITION_ORDER:
            used = counts.get(position_code, 0)
            capacity = ROLE_CAPACITIES.get(position_code)
            if capacity is not None and used >= capacity:
                # Filled limited positions disappear from the public availability list.
                continue
            label = POSITIONS[position_code].label
            if capacity is None:
                suffix = "без ограничений"
            else:
                suffix = f"свободно {capacity - used}/{capacity}"
            role_lines.append(f"{icons.get(position_code, '🎖')} <b>{label}</b> — {suffix}")
        role_lines.append("🌐 Лидер/Заместитель внешней группировки — по согласованию")
        text += (
            "\n\n<b>Доступные должности сейчас:</b>\n"
            + "\n".join(role_lines)
            + "\n\nОграничения: Лидер — 1 место, Заместитель лидера — 5 мест. "
              "Когда места заканчиваются, должность исчезает из этого списка."
        )

    existing_raw = await db.get_setting(msg_key)
    existing_id = int(existing_raw) if existing_raw and str(existing_raw).isdigit() else None
    if existing_id:
        # Normal restarts do not touch an already-current panel. Force means a real
        # in-place refresh (used after role changes so availability stays current).
        if already_current and not force:
            return False
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_id,
                text=text,
                reply_markup=markup,
            )
            await db.set_setting(key, datetime.utcnow().isoformat())
            return True
        except Exception as exc:
            if "message is not modified" in str(exc).casefold():
                await db.set_setting(key, datetime.utcnow().isoformat())
                return True
            # The old instruction may have been deleted manually. Re-create it.
            pass

    try:
        sent = await bot.send_message(chat_id, text, reply_markup=markup, **_topic_kwargs(thread_id))
    except Exception:
        return False
    await db.set_setting(msg_key, str(sent.message_id))
    await db.set_setting(key, datetime.utcnow().isoformat())
    return True


async def announce_configured_topics(bot: Bot, db: Database, *, force: bool = False) -> int:
    sent = 0
    for code in TOPICS:
        if await announce_topic(bot, db, code, force=force):
            sent += 1
            await asyncio.sleep(0.08)
    return sent


async def startup_announcements(bot: Bot, db: Database, config: Config, telethon: TelethonManager | None = None) -> None:
    """Refresh only manually configured topic panels on startup.

    Forum topics are never discovered or rebound automatically in v7.6.
    """
    if not config.announce_on_start:
        return
    await asyncio.sleep(3)
    await announce_configured_topics(bot, db, force=False)
    try:
        from .handlers import publish_role_request_card
        for req in await db.list_pending_role_requests(limit=200):
            await publish_role_request_card(bot, db, req.id)
    except Exception:
        pass


async def remove_old_topic_panel(bot: Bot, db: Database, code: str, new_chat_id: int, new_thread_id: int) -> None:
    """Remove the persistent panel from a previous binding when a module is moved."""
    old_chat = await db.get_setting(f"topic:{code}:chat")
    old_thread = await db.get_setting(f"topic:{code}:thread")
    if old_chat is None or old_thread is None:
        legacy_prefix = {"general": "general", "nicks": "nicks", "storage": "storage", "market": "market"}.get(code)
        if legacy_prefix:
            old_chat = await db.get_setting(f"{legacy_prefix}_chat_id")
            old_thread = await db.get_setting(f"{legacy_prefix}_thread_id")
    if not old_chat or old_thread is None:
        return
    try:
        previous = (int(old_chat), int(old_thread))
    except ValueError:
        return
    if previous == (int(new_chat_id), int(new_thread_id)):
        return
    msg_key = f"announce_message:{code}:{previous[0]}:{previous[1]}"
    raw_message_id = await db.get_setting(msg_key)
    if raw_message_id and str(raw_message_id).isdigit():
        try:
            await bot.delete_message(previous[0], int(raw_message_id))
        except Exception:
            pass
    await db.delete_settings([msg_key])


async def _set_topic(message: Message, db: Database, config: Config, code: str, permission: str="roles.manage") -> None:
    if not message.from_user or not await has_permission(message.from_user.id, permission, db, config):
        await temp_answer(message, "Недостаточно прав.", ttl=45)
        return
    await remove_old_topic_panel(message.bot, db, code, message.chat.id, _thread(message))
    await db.set_topic(code, message.chat.id, _thread(message))
    await db.set_setting("primary_chat_id", str(message.chat.id))
    await db.audit(message.from_user.id, "topic.set", f"{code}={message.chat.id}/{_thread(message)}")
    await announce_topic(message.bot, db, code, force=True)
    await temp_answer(message, "✅ <b>Раздел привязан.</b> Основная инструкция обновлена без создания дубликата.", ttl=45)
    await delete_incoming_later(message)


@router.message(Command("set_delivery_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_delivery(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "delivery", "delivery.manage")
@router.message(Command("set_gp_stock_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_stock(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "gp_stock", "gp_stock.manage")
@router.message(Command("set_events_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_events(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "events", "events.manage")
@router.message(Command("set_diplomacy_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_dip(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "diplomacy", "diplomacy.manage")
@router.message(Command("set_targets_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_targets(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "targets", "targets.manage")
@router.message(Command("set_news_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_news(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "news", "news.manage")
@router.message(Command("set_info_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_info(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "info", "info.manage")
@router.message(Command("set_bar_topic"), F.chat.type.in_(GROUP_TYPES))
async def set_bar(message: Message, db: Database, config: Config): await _set_topic(message, db, config, "bar", "roles.manage")




@router.message(Command("cleanup_bot_messages"), F.chat.type.in_(GROUP_TYPES))
async def cleanup_bot_messages_cmd(message: Message, db: Database, config: Config, bot: Bot):
    if not message.from_user or not await has_permission(message.from_user.id, "roles.manage", db, config):
        return
    await delete_incoming_later(message)
    count = await cleanup_all_tracked_messages(bot)
    # This confirmation cleans itself too.
    await temp_answer(message, f"🧹 Удалено временных служебных сообщений: <b>{count}</b>. Постоянные инструкции и карточки не затронуты.", ttl=20)

@router.message(Command("announce_all"), F.chat.type.in_(GROUP_TYPES))
async def announce_all_cmd(message: Message, db: Database, config: Config, bot: Bot):
    if not message.from_user or not await has_permission(message.from_user.id, "roles.manage", db, config):
        return
    n = await announce_configured_topics(bot, db, force=True)
    await temp_answer(message, f"✅ Инструкции обновлены в <b>{n}</b> настроенных разделах без создания дублей.", ttl=60)
    await delete_incoming_later(message)


@router.message(Command("autoconfigure_topics"), F.chat.type.in_(GROUP_TYPES))
async def autoconfigure_topics_disabled(message: Message, db: Database, config: Config):
    if not message.from_user or not await has_permission(message.from_user.id, "roles.manage", db, config):
        return
    await temp_answer(
        message,
        "ℹ️ <b>Автоопределение разделов отключено.</b>\n\n"
        "Чтобы не было путаницы, бот использует только ручную привязку. "
        "Откройте нужную тему и отправьте соответствующую команду <code>/set_..._topic</code>.\n\n"
        "Текущие привязки: <code>/admin → 🧩 Разделы и запуск</code>.",
        ttl=120,
    )
    await delete_incoming_later(message)


# -------------------- GP stock --------------------
@router.callback_query(F.data == "v7stock:list")
async def stock_list(cb: CallbackQuery, db: Database, config: Config):
    if not await _internal_user(cb.from_user.id, db, config): return await cb.answer("Недоступно",show_alert=True)
    rows = await db.gp_stock_list()
    if rows:
        body = "\n".join(
            f"• {escape(r['item_name'])}: <b>{r['quantity']}</b> — резерв {r.get('reserved', 0)}, доступно {r.get('available', max(0, int(r['quantity'])-int(r.get('reserved',0))))}"
            for r in rows[:80]
        )
    else:
        body = "Список пока пуст."
    await temp_callback_message(cb, "<b>🪖 Снаряжение ГП — остатки</b>\n\n" + body, ttl=config.temp_message_ttl)
    await cb.answer()

@router.callback_query(F.data == "v7stock:set")
async def stock_set_start(cb: CallbackQuery, db: Database, config: Config, state: FSMContext):
    if not await has_permission(cb.from_user.id,"gp_stock.manage",db,config): return await cb.answer("Недостаточно прав",show_alert=True)
    await state.clear(); await state.update_data(flow_chat_id=cb.message.chat.id,flow_thread_id=_thread(cb.message)); await state.set_state(GroupGpStock.item_name)
    sent = await topic_answer(cb.message, "Введите название предмета:")
    await state.update_data(flow_message_id=sent.message_id)
    await cb.answer()

@router.message(GroupGpStock.item_name, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def stock_name(m: Message, state: FSMContext):
    name=(m.text or "").strip()
    if not name: return
    await state.update_data(stock_name=name); await state.set_state(GroupGpStock.quantity); await safe_delete(m); await flow_edit_from_message(m, state, "Введите фактическое количество (целое число ≥ 0):")

@router.message(GroupGpStock.quantity, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def stock_qty(m: Message, db: Database, config: Config, state: FSMContext):
    if not m.from_user or not await has_permission(m.from_user.id,"gp_stock.manage",db,config): return
    raw=(m.text or "").strip()
    if not raw.isdigit(): return await temp_answer(m, "Нужно целое число ≥ 0.", ttl=60)
    data = await state.get_data(); name = data.get("stock_name")
    try:
        await db.gp_stock_upsert(name, int(raw), m.from_user.id)
    except ValueError as exc:
        await safe_delete(m)
        return await temp_answer(m, f"⚠️ {escape(str(exc))}", ttl=120)
    await safe_delete(m)
    data = await state.get_data()
    flow_id = data.get("flow_message_id")
    if flow_id:
        try:
            await m.bot.edit_message_text(chat_id=m.chat.id, message_id=int(flow_id), text=f"✅ <b>{escape(name)}</b>: {int(raw)} шт.")
            schedule_delete(m.bot, m.chat.id, int(flow_id), 45)
        except Exception:
            await temp_answer(m, f"✅ <b>{escape(name)}</b>: {int(raw)} шт.", ttl=45)
    else:
        await temp_answer(m, f"✅ <b>{escape(name)}</b>: {int(raw)} шт.", ttl=45)
    await state.clear()


# -------------------- Events --------------------
def _event_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def event_text(event: dict, db_players: dict[int,str] | None=None) -> str:
    joined=[x for x in event.get('participants',[]) if x['status']=='joined']
    cap=f" / {event['capacity']}" if event['capacity'] else ""
    lines=[f"<b>🎮 МЕРОПРИЯТИЕ #{event['id']}</b>","",f"<b>{escape(event['title'])}</b>",f"📅 {escape(_event_time(event['starts_at']))}",f"👥 {len(joined)}{cap}",f"📌 {EVENT_STATUS.get(event['status'],event['status'])}"]
    if event.get('details'): lines += ["",escape(event['details'])]
    if joined and db_players:
        lines += ["","<b>Участники:</b>"]+[f"• {escape(db_players.get(x['user_id'],str(x['user_id'])))}" for x in joined]
    return "\n".join(lines)

def event_keyboard(eid:int,status:str)->InlineKeyboardMarkup:
    rows=[]
    if status=='open': rows.append([InlineKeyboardButton(text="✅ Участвую",callback_data=f"v7event:join:{eid}"),InlineKeyboardButton(text="↩️ Отменить участие",callback_data=f"v7event:leave:{eid}")])
    if status in {'open','closed'}: rows.append([InlineKeyboardButton(text="🔒 Закрыть запись",callback_data=f"v7event:close:{eid}"),InlineKeyboardButton(text="✅ Завершить",callback_data=f"v7event:done:{eid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def refresh_event(bot:Bot,db:Database,eid:int):
    e=await db.get_event(eid)
    if not e: return
    names={}
    for x in e['participants']:
        p=await db.get_player(x['user_id']); names[x['user_id']]=p.game_nickname if p else str(x['user_id'])
    if e.get('chat_id') and e.get('message_id'):
        try: await bot.edit_message_text(chat_id=e['chat_id'],message_id=e['message_id'],text=event_text(e,names),reply_markup=event_keyboard(eid,e['status']))
        except Exception: pass

@router.callback_query(F.data == "v7events:list")
async def events_list(cb:CallbackQuery,db:Database,config:Config):
    if not await _internal_user(cb.from_user.id,db,config): return await cb.answer("Недоступно",show_alert=True)
    rows=await db.list_events(); text="<b>📅 Ближайшие мероприятия</b>\n\n"+("\n".join(f"#{r['id']} — <b>{escape(r['title'])}</b> — {escape(_event_time(r['starts_at']))}" for r in rows) if rows else "Нет активных мероприятий.")
    await temp_callback_message(cb, text, ttl=config.temp_message_ttl); await cb.answer()

@router.callback_query(F.data == "v7events:new")
async def event_new(cb:CallbackQuery,db:Database,config:Config,state:FSMContext):
    if not await has_permission(cb.from_user.id,"events.manage",db,config): return await cb.answer("Недостаточно прав",show_alert=True)
    await state.clear(); await state.update_data(flow_chat_id=cb.message.chat.id,flow_thread_id=_thread(cb.message)); await state.set_state(GroupEventCreate.title)
    sent = await topic_answer(cb.message, "Название мероприятия:")
    await state.update_data(flow_message_id=sent.message_id); await cb.answer()

@router.message(GroupEventCreate.title, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def event_title(m: Message, state: FSMContext):
    await state.update_data(event_title=(m.text or '').strip())
    await state.set_state(GroupEventCreate.starts_at)
    await safe_delete(m)
    await flow_edit_from_message(m, state, "Дата и время, например <code>05.09.2026 20:00</code>:")
@router.message(GroupEventCreate.starts_at, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def event_date(m:Message,state:FSMContext):
    raw=(m.text or '').strip()
    try: dt=datetime.strptime(raw,"%d.%m.%Y %H:%M"); value=dt.isoformat(timespec='minutes')
    except ValueError: return await temp_answer(m, "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>.", ttl=60)
    await state.update_data(event_starts=value); await state.set_state(GroupEventCreate.capacity); await safe_delete(m); await flow_edit_from_message(m, state, "Лимит участников (0 = без лимита):")
@router.message(GroupEventCreate.capacity, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def event_capacity(m:Message,state:FSMContext):
    raw=(m.text or '').strip()
    if not raw.isdigit(): return await temp_answer(m, "Введите число 0 или больше.", ttl=60)
    await state.update_data(event_capacity=int(raw)); await state.set_state(GroupEventCreate.details); await safe_delete(m); await flow_edit_from_message(m, state, "Описание мероприятия или <code>-</code>, если не нужно:")
@router.message(GroupEventCreate.details, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def event_details(m:Message,db:Database,config:Config,state:FSMContext,bot:Bot):
    if not m.from_user or not await has_permission(m.from_user.id,"events.manage",db,config): return
    data=await state.get_data(); details=None if (m.text or '').strip()=='-' else (m.text or '').strip(); await safe_delete(m)
    eid=await db.create_event(data['event_title'],details,data['event_starts'],data['event_capacity'],m.from_user.id,m.chat.id,_thread(m)); e=await db.get_event(eid)
    data = await state.get_data(); flow_id = data.get("flow_message_id")
    if flow_id:
        try:
            await m.bot.edit_message_text(chat_id=m.chat.id, message_id=int(flow_id), text=event_text(e, {}), reply_markup=event_keyboard(eid, 'open'))
            await db.set_event_message(eid, int(flow_id))
        except Exception:
            sent = await topic_answer(m, event_text(e, {}), reply_markup=event_keyboard(eid, 'open')); await db.set_event_message(eid, sent.message_id)
    else:
        sent = await topic_answer(m, event_text(e, {}), reply_markup=event_keyboard(eid, 'open')); await db.set_event_message(eid, sent.message_id)
    await state.clear()

@router.callback_query(F.data.regexp(r"^v7event:(join|leave|close|done):\d+$"))
async def event_action(cb:CallbackQuery,db:Database,config:Config,bot:Bot):
    _,action,raw=cb.data.split(':'); eid=int(raw)
    if action in {'join','leave'}:
        if not await _internal_user(cb.from_user.id,db,config): return await cb.answer("Недоступно",show_alert=True)
        ok=await db.event_join(eid,cb.from_user.id,action=='join')
        if not ok: return await cb.answer("Запись закрыта или мест больше нет.",show_alert=True)
    else:
        if not await has_permission(cb.from_user.id,"events.manage",db,config): return await cb.answer("Недостаточно прав",show_alert=True)
        await db.event_set_status(eid,'closed' if action=='close' else 'done',cb.from_user.id)
    await refresh_event(bot,db,eid); await cb.answer("Готово")


# -------------------- Diplomacy --------------------
# Main human factions from the original S.T.A.L.K.E.R. trilogy.
DIPLOMACY_FACTIONS: tuple[tuple[str, str], ...] = (
    ("loners", "\u041e\u0434\u0438\u043d\u043e\u0447\u043a\u0438"),
    ("bandits", "\u0411\u0430\u043d\u0434\u0438\u0442\u044b"),
    ("duty", "\u0414\u043e\u043b\u0433"),
    ("freedom", "\u0421\u0432\u043e\u0431\u043e\u0434\u0430"),
    ("mercs", "\u041d\u0430\u0451\u043c\u043d\u0438\u043a\u0438"),
    ("monolith", "\u041c\u043e\u043d\u043e\u043b\u0438\u0442"),
    ("scientists", "\u0423\u0447\u0451\u043d\u044b\u0435"),
    ("military", "\u0412\u043e\u0435\u043d\u043d\u044b\u0435"),
    ("clear_sky", "\u0427\u0438\u0441\u0442\u043e\u0435 \u041d\u0435\u0431\u043e"),
    ("renegades", "\u0420\u0435\u043d\u0435\u0433\u0430\u0442\u044b"),
    # Kept because this faction is already used by the group's role model.
    ("sin", "\u0413\u0440\u0435\u0445"),
)
DIPLOMACY_FACTION_BY_CODE = dict(DIPLOMACY_FACTIONS)
REL_ICON = {"ally": "\U0001f7e2", "neutral": "\u26aa", "war": "\U0001f534"}


def diplomacy_factions_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for code, label in DIPLOMACY_FACTIONS:
        pair.append(InlineKeyboardButton(text=label, callback_data=f"v7dip:faction:{code}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="\u2795 \u0414\u0440\u0443\u0433\u0430\u044f \u0433\u0440\u0443\u043f\u043f\u0438\u0440\u043e\u0432\u043a\u0430", callback_data="v7dip:other")])
    rows.append([InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data="gflow:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def diplomacy_relation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001f7e2 \u0421\u043e\u044e\u0437", callback_data="v7dip:rel:ally"),
        InlineKeyboardButton(text="\u26aa \u041d\u0435\u0439\u0442\u0440\u0430\u043b", callback_data="v7dip:rel:neutral"),
        InlineKeyboardButton(text="\U0001f534 \u0412\u043e\u0439\u043d\u0430", callback_data="v7dip:rel:war"),
    ], [InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data="gflow:cancel")]])


def diplomacy_records_keyboard(rows: list[dict], can_manage: bool) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    for r in rows[:40]:
        title = f"{REL_ICON.get(r['relation'], '')} {r['faction_name']}"
        if can_manage:
            buttons.append([
                InlineKeyboardButton(text=title[:44], callback_data=f"v7dip:edit:{r['id']}"),
                InlineKeyboardButton(text="\U0001f5d1", callback_data=f"v7dip:delask:{r['id']}"),
            ])
        else:
            buttons.append([InlineKeyboardButton(text=title[:52], callback_data="v7dip:noop")])
    if can_manage:
        buttons.append([InlineKeyboardButton(text="\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c / \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c", callback_data="v7dip:new")])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


async def _dip_begin_choice(cb: CallbackQuery, state: FSMContext, text: str) -> None:
    await state.clear()
    await state.update_data(flow_chat_id=cb.message.chat.id, flow_thread_id=_thread(cb.message))
    await state.set_state(GroupDiplomacy.faction)
    sent = await topic_answer(cb.message, text, reply_markup=diplomacy_factions_keyboard())
    await state.update_data(flow_message_id=sent.message_id)


@router.callback_query(F.data == "v7dip:list")
async def dip_list(cb: CallbackQuery, db: Database, config: Config):
    if not await _internal_user(cb.from_user.id, db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e", show_alert=True)
    rows = await db.diplomacy_list()
    body = "\n".join(
        f"{REL.get(r['relation'], r['relation'])} \u2014 <b>{escape(r['faction_name'])}</b>"
        + (f" \u2014 {escape(r['note'])}" if r.get('note') else "")
        for r in rows
    ) if rows else "\u0417\u0430\u043f\u0438\u0441\u0435\u0439 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442."
    can_manage = await has_permission(cb.from_user.id, "diplomacy.manage", db, config)
    await temp_callback_message(
        cb,
        "<b>\U0001f91d \u041e\u0442\u043d\u043e\u0448\u0435\u043d\u0438\u044f \u0433\u0440\u0443\u043f\u043f\u0438\u0440\u043e\u0432\u043a\u0438</b>\n\n" + body,
        ttl=max(config.temp_message_ttl, 180),
        reply_markup=diplomacy_records_keyboard(rows, can_manage),
    )
    await cb.answer()


@router.callback_query(F.data == "v7dip:noop")
async def dip_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "v7dip:history")
async def dip_history(cb: CallbackQuery, db: Database, config: Config):
    if not await _internal_user(cb.from_user.id, db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e", show_alert=True)
    rows = await db.diplomacy_history(30)
    text = "<b>\U0001f4dc \u0418\u0441\u0442\u043e\u0440\u0438\u044f \u0434\u0438\u043f\u043b\u043e\u043c\u0430\u0442\u0438\u0438</b>\n\n" + (
        "\n".join(
            f"#{r['id']} {REL.get(r['relation'], r['relation'])} \u2014 <b>{escape(r['faction_name'])}</b>"
            + (f" \u2014 {escape(r['note'])}" if r.get('note') else "")
            for r in rows
        ) if rows else "\u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043f\u043e\u043a\u0430 \u043f\u0443\u0441\u0442\u0430."
    )
    await temp_callback_message(cb, text, ttl=config.temp_message_ttl)
    await cb.answer()


@router.callback_query(F.data == "v7dip:new")
async def dip_new(cb: CallbackQuery, db: Database, config: Config, state: FSMContext):
    if not await has_permission(cb.from_user.id, "diplomacy.manage", db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u043f\u0440\u0430\u0432", show_alert=True)
    await _dip_begin_choice(cb, state, "<b>\U0001f91d \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0433\u0440\u0443\u043f\u043f\u0438\u0440\u043e\u0432\u043a\u0443</b>:")
    await cb.answer()


@router.callback_query(GroupDiplomacy.faction, F.data.startswith("v7dip:faction:"))
async def dip_pick_faction(cb: CallbackQuery, state: FSMContext):
    code = cb.data.rsplit(":", 1)[1]
    faction = DIPLOMACY_FACTION_BY_CODE.get(code)
    if not faction:
        return await cb.answer("\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u0430\u044f \u0433\u0440\u0443\u043f\u043f\u0438\u0440\u043e\u0432\u043a\u0430", show_alert=True)
    await state.update_data(dip_faction=faction, dip_note=None)
    await state.set_state(GroupDiplomacy.relation)
    await cb.message.edit_text(
        f"<b>{escape(faction)}</b>\n\n\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0441\u0442\u0430\u0442\u0443\u0441 \u043e\u0442\u043d\u043e\u0448\u0435\u043d\u0438\u0439:",
        reply_markup=diplomacy_relation_keyboard(),
    )
    await cb.answer()


@router.callback_query(GroupDiplomacy.faction, F.data == "v7dip:other")
async def dip_other_faction(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0434\u0440\u0443\u0433\u043e\u0439 \u0433\u0440\u0443\u043f\u043f\u0438\u0440\u043e\u0432\u043a\u0438:")
    await cb.answer()


@router.message(GroupDiplomacy.faction, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def dip_faction(m: Message, state: FSMContext):
    faction = (m.text or "").strip()
    if not faction:
        return
    await state.update_data(dip_faction=faction, dip_note=None)
    await state.set_state(GroupDiplomacy.relation)
    await safe_delete(m)
    await flow_edit_from_message(m, state, f"<b>{escape(faction)}</b>\n\n\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0441\u0442\u0430\u0442\u0443\u0441 \u043e\u0442\u043d\u043e\u0448\u0435\u043d\u0438\u0439:", reply_markup=diplomacy_relation_keyboard())


@router.callback_query(F.data.regexp(r"^v7dip:edit:\d+$"))
async def dip_edit(cb: CallbackQuery, db: Database, config: Config, state: FSMContext):
    if not await has_permission(cb.from_user.id, "diplomacy.manage", db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u043f\u0440\u0430\u0432", show_alert=True)
    rid = int(cb.data.rsplit(":", 1)[1])
    row = await db.diplomacy_get(rid)
    if not row:
        return await cb.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u0443\u0436\u0435 \u0443\u0434\u0430\u043b\u0435\u043d\u0430", show_alert=True)
    await state.clear()
    await state.update_data(
        flow_chat_id=cb.message.chat.id,
        flow_thread_id=_thread(cb.message),
        dip_faction=row['faction_name'],
        dip_note=row.get('note'),
        dip_record_id=rid,
    )
    await state.set_state(GroupDiplomacy.relation)
    sent = await topic_answer(
        cb.message,
        f"<b>{escape(row['faction_name'])}</b>\n\n\u0422\u0435\u043a\u0443\u0449\u0438\u0439 \u0441\u0442\u0430\u0442\u0443\u0441: {REL.get(row['relation'], row['relation'])}\n\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043d\u043e\u0432\u044b\u0439:",
        reply_markup=diplomacy_relation_keyboard(),
    )
    await state.update_data(flow_message_id=sent.message_id)
    await cb.answer()


@router.callback_query(GroupDiplomacy.relation, F.data.startswith("v7dip:rel:"))
async def dip_rel(cb: CallbackQuery, db: Database, config: Config, state: FSMContext):
    if not await has_permission(cb.from_user.id, "diplomacy.manage", db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u043f\u0440\u0430\u0432", show_alert=True)
    relation = cb.data.rsplit(":", 1)[1]
    if relation not in REL:
        return await cb.answer("\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0441\u0442\u0430\u0442\u0443\u0441", show_alert=True)
    data = await state.get_data()
    faction = data.get('dip_faction')
    if not faction:
        await state.clear()
        return await cb.answer("\u0421\u0435\u0441\u0441\u0438\u044f \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u0430", show_alert=True)
    rid = await db.diplomacy_set(faction, relation, data.get('dip_note'), cb.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001f4dd \u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439", callback_data=f"v7dip:note:{rid}"),
        InlineKeyboardButton(text="\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c", callback_data=f"v7dip:delask:{rid}"),
    ]])
    await cb.message.edit_text(f"\u2705 {REL[relation]} \u2014 <b>{escape(faction)}</b>", reply_markup=kb)
    schedule_delete(cb.bot, cb.message.chat.id, cb.message.message_id, 180)
    await state.clear()
    await cb.answer("\u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e")


@router.callback_query(F.data.regexp(r"^v7dip:note:\d+$"))
async def dip_note_start(cb: CallbackQuery, db: Database, config: Config, state: FSMContext):
    if not await has_permission(cb.from_user.id, "diplomacy.manage", db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u043f\u0440\u0430\u0432", show_alert=True)
    rid = int(cb.data.rsplit(":", 1)[1])
    row = await db.diplomacy_get(rid)
    if not row:
        return await cb.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430", show_alert=True)
    await state.clear()
    await state.update_data(
        flow_chat_id=cb.message.chat.id,
        flow_thread_id=_thread(cb.message),
        dip_record_id=rid,
        dip_faction=row['faction_name'],
        dip_relation=row['relation'],
    )
    await state.set_state(GroupDiplomacy.note)
    sent = await topic_answer(cb.message, "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439/\u043f\u0440\u0438\u0447\u0438\u043d\u0443. <code>-</code> \u0443\u0434\u0430\u043b\u0438\u0442 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439:")
    await state.update_data(flow_message_id=sent.message_id)
    await cb.answer()


@router.message(GroupDiplomacy.note, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def dip_note(m: Message, db: Database, config: Config, state: FSMContext):
    if not m.from_user or not await has_permission(m.from_user.id, "diplomacy.manage", db, config):
        return
    data = await state.get_data()
    note = None if (m.text or '').strip() == '-' else (m.text or '').strip()
    rid = await db.diplomacy_set(data['dip_faction'], data['dip_relation'], note, m.from_user.id)
    await safe_delete(m)
    flow_id = data.get('flow_message_id')
    text = f"\u2705 {REL[data['dip_relation']]} \u2014 <b>{escape(data['dip_faction'])}</b>"
    if note:
        text += f"\n\U0001f4dd {escape(note)}"
    if flow_id:
        try:
            await m.bot.edit_message_text(chat_id=m.chat.id, message_id=int(flow_id), text=text)
            schedule_delete(m.bot, m.chat.id, int(flow_id), 90)
        except Exception:
            await temp_answer(m, text, ttl=90)
    else:
        await temp_answer(m, text, ttl=90)
    await state.clear()


@router.callback_query(F.data.regexp(r"^v7dip:delask:\d+$"))
async def dip_delete_ask(cb: CallbackQuery, db: Database, config: Config):
    if not await has_permission(cb.from_user.id, "diplomacy.manage", db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u043f\u0440\u0430\u0432", show_alert=True)
    rid = int(cb.data.rsplit(":", 1)[1])
    row = await db.diplomacy_get(rid)
    if not row:
        return await cb.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u0443\u0436\u0435 \u0443\u0434\u0430\u043b\u0435\u043d\u0430", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\U0001f5d1 \u0414\u0430, \u0443\u0434\u0430\u043b\u0438\u0442\u044c", callback_data=f"v7dip:delete:{rid}"),
        InlineKeyboardButton(text="\u21a9\ufe0f \u041d\u0435\u0442", callback_data="v7dip:noop"),
    ]])
    await temp_callback_message(
        cb,
        f"\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0437\u0430\u043f\u0438\u0441\u044c <b>{escape(row['faction_name'])}</b>?",
        ttl=120,
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data.regexp(r"^v7dip:delete:\d+$"))
async def dip_delete(cb: CallbackQuery, db: Database, config: Config, state: FSMContext):
    if not await has_permission(cb.from_user.id, "diplomacy.manage", db, config):
        return await cb.answer("\u041d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u043f\u0440\u0430\u0432", show_alert=True)
    rid = int(cb.data.rsplit(":", 1)[1])
    row = await db.diplomacy_get(rid)
    ok = await db.diplomacy_delete(rid, cb.from_user.id)
    if not ok:
        return await cb.answer("\u0417\u0430\u043f\u0438\u0441\u044c \u0443\u0436\u0435 \u0443\u0434\u0430\u043b\u0435\u043d\u0430", show_alert=True)
    await state.clear()
    await cb.message.edit_text(f"\U0001f5d1 \u0423\u0434\u0430\u043b\u0435\u043d\u043e: <b>{escape(row['faction_name']) if row else rid}</b>")
    schedule_delete(cb.bot, cb.message.chat.id, cb.message.message_id, 45)
    await cb.answer("\u0423\u0434\u0430\u043b\u0435\u043d\u043e")


# -------------------- Targets --------------------
def target_card(t:dict)->str:
    lines=[f"<b>🏴 ЦЕЛЬ #{t['id']}</b>","",f"👤 <b>{escape(t['target_name'])}</b>",f"📌 {TARGET_STATUS.get(t['status'],t['status'])}"]
    if t.get('reason'): lines.append(f"⚠️ Причина: {escape(t['reason'])}")
    if t.get('reward'): lines.append(f"💰 Награда: {escape(t['reward'])}")
    if t.get('last_location'): lines.append(f"📍 Последнее место: {escape(t['last_location'])}")
    return "\n".join(lines)
def target_kb(tid:int,status:str)->InlineKeyboardMarkup|None:
    if status=='active': return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎯 Взять цель",callback_data=f"v7target:taken:{tid}"),InlineKeyboardButton(text="✅ Выполнено",callback_data=f"v7target:done:{tid}")],[InlineKeyboardButton(text="❌ Отменить",callback_data=f"v7target:cancelled:{tid}")]])
    if status=='taken': return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Выполнено",callback_data=f"v7target:done:{tid}"),InlineKeyboardButton(text="↩️ Вернуть",callback_data=f"v7target:active:{tid}")]])
    return None
@router.callback_query(F.data == "v7targets:list")
async def targets_list(cb:CallbackQuery,db:Database,config:Config):
    if not await _internal_user(cb.from_user.id,db,config): return await cb.answer("Недоступно",show_alert=True)
    rows=await db.target_list(); await temp_callback_message(cb, "<b>🏴 Список целей</b>\n\n"+("\n".join(f"{TARGET_STATUS.get(r['status'])} #{r['id']} — <b>{escape(r['target_name'])}</b>" for r in rows) if rows else "Целей нет."), ttl=config.temp_message_ttl); await cb.answer()
@router.callback_query(F.data == "v7targets:new")
async def target_new(cb:CallbackQuery,db:Database,config:Config,state:FSMContext):
    if not await has_permission(cb.from_user.id,"targets.manage",db,config): return await cb.answer("Недостаточно прав",show_alert=True)
    await state.clear(); await state.set_state(GroupTargetCreate.name); sent=await topic_answer(cb.message, "Ник/название цели:"); await state.update_data(flow_message_id=sent.message_id); await cb.answer()
@router.message(GroupTargetCreate.name, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def t_name(m:Message,state:FSMContext): await state.update_data(t_name=(m.text or '').strip()); await state.set_state(GroupTargetCreate.reason); await safe_delete(m); await flow_edit_from_message(m,state,"Причина или <code>-</code>:")
@router.message(GroupTargetCreate.reason, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def t_reason(m:Message,state:FSMContext): await state.update_data(t_reason=None if (m.text or '').strip()=='-' else (m.text or '').strip()); await state.set_state(GroupTargetCreate.reward); await safe_delete(m); await flow_edit_from_message(m,state,"Награда или <code>-</code>:")
@router.message(GroupTargetCreate.reward, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def t_reward(m:Message,state:FSMContext): await state.update_data(t_reward=None if (m.text or '').strip()=='-' else (m.text or '').strip()); await state.set_state(GroupTargetCreate.location); await safe_delete(m); await flow_edit_from_message(m,state,"Последнее известное место или <code>-</code>:")
@router.message(GroupTargetCreate.location, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def t_location(m:Message,db:Database,config:Config,state:FSMContext):
    if not m.from_user or not await has_permission(m.from_user.id,"targets.manage",db,config): return
    d=await state.get_data(); loc=None if (m.text or '').strip()=='-' else (m.text or '').strip(); tid=await db.target_create(d['t_name'],d['t_reason'],d['t_reward'],loc,m.from_user.id); await safe_delete(m); rows=await db.target_list(100); t=next(x for x in rows if x['id']==tid)
    flow_id=d.get("flow_message_id")
    if flow_id:
        try: await m.bot.edit_message_text(chat_id=m.chat.id,message_id=int(flow_id),text=target_card(t),reply_markup=target_kb(tid,'active'))
        except Exception: await topic_answer(m, target_card(t),reply_markup=target_kb(tid,'active'))
    else: await topic_answer(m, target_card(t),reply_markup=target_kb(tid,'active'))
    await state.clear()
@router.callback_query(F.data.regexp(r"^v7target:(active|taken|done|cancelled):\d+$"))
async def target_action(cb:CallbackQuery,db:Database,config:Config):
    if not await has_permission(cb.from_user.id,"targets.manage",db,config): return await cb.answer("Недостаточно прав",show_alert=True)
    _,status,raw=cb.data.split(':'); await db.target_set_status(int(raw),status,cb.from_user.id,cb.from_user.id if status=='taken' else None); t=await db.target_get(int(raw))
    if t:
        try: await cb.message.edit_text(target_card(t),reply_markup=target_kb(t['id'],t['status']))
        except Exception: pass
    await cb.answer("Статус изменён")


# -------------------- News / Info --------------------
@router.callback_query(F.data.regexp(r"^v7info:list:(news|info)$"))
async def info_list(cb:CallbackQuery,db:Database,config:Config):
    if not await _internal_user(cb.from_user.id,db,config): return await cb.answer("Недоступно",show_alert=True)
    module=cb.data.rsplit(':',1)[1]; rows=await db.info_list(module); title="📈 Новости" if module=='news' else "🗺 Прочая информация"
    text=f"<b>{title}</b>\n\n"+("\n\n".join(f"<b>{escape(r['title'])}</b>\n{escape(r['body'][:800])}" for r in rows[:10]) if rows else "Записей пока нет.")
    await temp_callback_message(cb, text, ttl=config.temp_message_ttl); await cb.answer()
@router.callback_query(F.data.regexp(r"^v7info:new:(news|info)$"))
async def info_new(cb:CallbackQuery,db:Database,config:Config,state:FSMContext):
    module=cb.data.rsplit(':',1)[1]; perm='news.manage' if module=='news' else 'info.manage'
    if not await has_permission(cb.from_user.id,perm,db,config): return await cb.answer("Недостаточно прав",show_alert=True)
    await state.clear(); await state.update_data(info_module=module); await state.set_state(GroupInfoCreate.title); sent=await topic_answer(cb.message, "Заголовок:"); await state.update_data(flow_message_id=sent.message_id); await cb.answer()
@router.message(GroupInfoCreate.title, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def info_title(m:Message,state:FSMContext): await state.update_data(info_title=(m.text or '').strip()); await state.set_state(GroupInfoCreate.body); await safe_delete(m); await flow_edit_from_message(m,state,"Текст записи:")
@router.message(GroupInfoCreate.body, F.chat.type.in_(GROUP_TYPES), ~F.text.startswith("/"))
async def info_body(m:Message,db:Database,config:Config,state:FSMContext):
    if not m.from_user: return
    d=await state.get_data(); module=d['info_module']; perm='news.manage' if module=='news' else 'info.manage'
    if not await has_permission(m.from_user.id,perm,db,config): return
    body=(m.text or '').strip(); iid=await db.info_add(module,d['info_title'],body,m.from_user.id); await safe_delete(m)
    final=f"✅ Запись #{iid}\n\n<b>{escape(d['info_title'])}</b>\n{escape(body)}"
    flow_id=d.get("flow_message_id")
    if flow_id:
        try: await m.bot.edit_message_text(chat_id=m.chat.id,message_id=int(flow_id),text=final)
        except Exception: await topic_answer(m, final)
    else: await topic_answer(m, final)
    await state.clear()

@router.message(Command("set_news_source"),F.chat.type.in_(GROUP_TYPES))
async def news_source(m:Message,db:Database,config:Config): await _set_mirror_source(m,db,config,'news','news.manage')
@router.message(Command("set_info_source"),F.chat.type.in_(GROUP_TYPES))
async def info_source(m:Message,db:Database,config:Config): await _set_mirror_source(m,db,config,'info','info.manage')
async def _set_mirror_source(m:Message,db:Database,config:Config,kind:str,perm:str):
    if not m.from_user or not await has_permission(m.from_user.id,perm,db,config): return await temp_answer(m,"Недостаточно прав.",ttl=45)
    dest=await db.get_topic(kind)
    if not dest: return await temp_answer(m,f"Сначала привяжите тему назначения командой /set_{kind}_topic.",ttl=90)
    await db.mirror_set(kind,m.chat.id,_thread(m),dest[0],dest[1],m.from_user.id)
    await temp_answer(m,"✅ Эта тема назначена источником автоматического переноса новых сообщений. Бот должен оставаться участником исходной группы.",ttl=90)
    await delete_incoming_later(m)


# -------------------- Delivery --------------------
def delivery_text(order,items)->str:
    lines=[f"<b>⚛️ ЗАКАЗ ГОТОВ К ВЫДАЧЕ #{order.id}</b>","",f"👤 <b>{escape(order.requester_nickname)}</b>","", "<b>📦 Состав:</b>"]
    lines += [f"• {escape(x.item_name)} × <b>{x.quantity}</b>" for x in items]
    lines += ["",f"📌 {WORKFLOW_LABELS.get(order.workflow_status,order.workflow_status)}"]
    return "\n".join(lines)

def delivery_kb(order_id:int)->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Выдан игроку",callback_data=f"v7delivery:issue:{order_id}")]])

async def publish_delivery_card(bot:Bot,db:Database,order_id:int)->bool:
    loaded=await db.get_market_order(order_id); topic=await db.get_topic('delivery')
    if not loaded or not topic: return False
    existing=await db.get_market_delivery_ref(order_id)
    if existing: return True
    order,items=loaded
    sent=await bot.send_message(topic[0],delivery_text(order,items),reply_markup=delivery_kb(order_id),**_topic_kwargs(topic[1]))
    await db.set_market_delivery_message(order_id,topic[0],topic[1],sent.message_id)
    return True

@router.callback_query(F.data == "v7delivery:list")
async def delivery_list(cb:CallbackQuery,db:Database,config:Config):
    if not await _internal_user(cb.from_user.id,db,config): return await cb.answer("Недоступно",show_alert=True)
    orders=await db.list_market_orders(limit=50); ready=[o for o in orders if o.workflow_status=='assembled']
    # Recovery path: if the delivery topic was configured later, or Telegram failed
    # while the order was being assembled, recreate only missing delivery cards.
    for order in ready:
        try:
            await publish_delivery_card(cb.bot, db, order.id)
        except Exception:
            pass
    await temp_callback_message(cb, "<b>⚛️ Готовые к выдаче</b>\n\n"+("\n".join(f"• Заказ <b>#{o.id}</b> — {escape(o.requester_nickname)}" for o in ready) if ready else "Сейчас готовых заказов нет."), ttl=config.temp_message_ttl); await cb.answer()
@router.callback_query(F.data.regexp(r"^v7delivery:issue:\d+$"))
async def delivery_issue(cb:CallbackQuery,db:Database,config:Config,bot:Bot):
    if not await has_permission(cb.from_user.id,'delivery.manage',db,config): return await cb.answer("Недостаточно прав",show_alert=True)
    oid = int(cb.data.rsplit(':', 1)[1])
    loaded = await db.get_market_order(oid)
    if not loaded or loaded[0].workflow_status != 'assembled':
        return await cb.answer("Заказ уже не ожидает выдачи.", show_alert=True)
    order, items = loaded
    ok, shortages = await db.gp_stock_consume_reserved([(x.item_name, x.quantity) for x in items], cb.from_user.id)
    if not ok:
        lines = ["⚠️ Нельзя закрыть выдачу: резерв склада недостаточен:"] + [
            f"• {name}: нужно {need}, в резерве {have}" for name, need, have in shortages
        ]
        await temp_callback_message(cb, "\n".join(lines), ttl=120)
        return await cb.answer("Проверьте склад ГП", show_alert=True)
    await db.set_market_workflow_status(oid, 'issued')
    order, items = (await db.get_market_order(oid))
    await refresh_order_cards(bot, order, items)
    try:
        await cb.message.edit_text(delivery_text(order, items), reply_markup=None)
    except Exception:
        pass
    await db.audit(cb.from_user.id, 'delivery.issue', f"order #{oid}")
    await cb.answer("Выдача отмечена")

@router.callback_query(F.data == "gadmin:system", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def system_admin(cb: CallbackQuery, db: Database, config: Config, telethon: TelethonManager):
    if not await has_permission(cb.from_user.id, "roles.manage", db, config):
        return await cb.answer("Недостаточно прав", show_alert=True)
    topics = await db.list_topics()
    lines = ["<b>🧩 Разделы и запуск v7</b>", ""]
    for code, meta in TOPICS.items():
        lines.append(f"{'✅' if code in topics else '⚠️'} {meta['emoji']} {meta['label']}")
    lines += [
        "",
        "<b>Разделы назначаются только вручную.</b>",
        "Зайдите внутрь нужной forum-темы и отправьте:",
        "",
        "<code>/set_general_topic</code> — General",
        "<code>/set_nicks_topic</code> — Ники игроков",
        "<code>/set_storage_topic</code> — Снаряжение группировки",
        "<code>/set_market_topic</code> — Рынок ГП",
        "<code>/set_delivery_topic</code> — Пункт выдачи заказов",
        "<code>/set_gp_stock_topic</code> — Снаряжение ГП",
        "<code>/set_events_topic</code> — Мероприятия",
        "<code>/set_diplomacy_topic</code> — Союзы и Война",
        "<code>/set_targets_topic</code> — Список целей",
        "<code>/set_news_topic</code> — Новости",
        "<code>/set_info_topic</code> — Прочая информация",
        "<code>/set_bar_topic</code> — Бар «Гильза»",
        "",
        "Если ошиблись, просто выполните ту же команду в правильной теме — привязка перенесётся.",
        "<code>/announce_all</code> обновляет инструкции только в уже назначенных разделах.",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Повторить инструкции", callback_data="v7admin:announce")],
        [InlineKeyboardButton(text="📜 Журнал действий", callback_data="v7admin:audit")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="gadmin:home")],
    ])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "v7admin:announce", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def system_announce(cb: CallbackQuery, db: Database, config: Config, bot: Bot):
    if not await has_permission(cb.from_user.id, "roles.manage", db, config):
        return await cb.answer("Недостаточно прав", show_alert=True)
    count = await announce_configured_topics(bot, db, force=True)
    await db.audit(cb.from_user.id, "announce.all", str(count))
    await cb.answer(f"Опубликовано в {count} разделах", show_alert=True)


@router.callback_query(F.data == "v7admin:audit", F.message.chat.type.in_(ADMIN_CHAT_TYPES))
async def system_audit(cb: CallbackQuery, db: Database, config: Config):
    if not await has_permission(cb.from_user.id, "roles.manage", db, config):
        return await cb.answer("Недостаточно прав", show_alert=True)
    rows = await db.list_audit(25)
    text = "<b>📜 Последние действия</b>\n\n" + ("\n".join(
        f"#{r['id']} <code>{escape(r['action'])}</code> — {escape(r.get('details') or '—')}" for r in rows
    ) if rows else "Журнал пока пуст.")
    await temp_callback_message(cb, text, ttl=config.temp_message_ttl)
    await cb.answer()

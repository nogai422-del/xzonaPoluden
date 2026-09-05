from __future__ import annotations

import hashlib
from html import escape
from uuid import uuid4

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup

from .group_handlers import has_permission, safe_delete
from .housekeeping import topic_answer, temp_answer, schedule_delete
from .roles import is_external_position
from .community_views import (keyboard, storage_panel, market_panel, diplomacy_panel,
                              request_text, request_keyboard, ad_text, ad_keyboard, render_matrix, render_warehouse)

router = Router(name='community_v8')


class Flow(StatesGroup):
    value = State()
    confirm = State()


class InputErrors(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event,data)
        except ValueError as exc:
            if isinstance(event,CallbackQuery):
                await event.answer(str(exc)[:190],show_alert=True)
            elif isinstance(event,Message):
                await topic_answer(event,escape(str(exc)))


router.callback_query.middleware(InputErrors())
router.message.middleware(InputErrors())


async def internal(user, db):
    p = await db.get_player(user)
    if not p or p.position_status!='approved' or not p.position_code or is_external_position(p.position_code):
        raise ValueError('Сначала зарегистрируйте ник и подтвердите внутреннюю должность у руководства.')
    return p


async def permission(user, perm, db, config):
    if not await has_permission(user,perm,db,config):
        raise ValueError('Недостаточно прав для этого действия.')


async def in_topic(message, db, code):
    configured = await db.get_warehouse_topic() if code=='storage' else await db.get_topic(code)
    current = (message.chat.id, int(message.message_thread_id or 0))
    if not configured or current != configured:
        raise ValueError(f'Откройте настроенную тему «{dict(storage="Снаряжение группировки",market="Рынок ГП",diplomacy="Дипломатия")[code]}».')


async def start_flow(cb, state, kind, prompt, **data):
    await state.clear()
    sent = await _screen_text(cb, prompt, keyboard([('❌ Отмена','c8:cancel')]))
    await state.set_state(Flow.value)
    await state.update_data(kind=kind,nonce=uuid4().hex,chat=sent.chat.id,
                            thread=int(sent.message_thread_id or 0),prompt_id=sent.message_id,**data)


async def show_catalog(cb, db, config, kind, page=0, search='', archived=False):
    if kind not in ('request','stock','sale'):
        raise ValueError('Неизвестный каталог.')
    await in_topic(cb.message,db,'market' if kind=='sale' else 'storage')
    if kind=='stock':
        await permission(cb.from_user.id,'storage.manage',db,config)
    else:
        await internal(cb.from_user.id,db)
    cats = await db.catalogue_list(page,search,archived)
    buttons = [[(f"{r['name']} · свободно {r['quantity']-r['reserved']}",f"c8:pick:{kind}:{r['id']}")] for r in cats[:8]]
    nav = []
    # Search results are intentionally one page; narrow the query when needed.
    if not search:
        mode = 'archive' if archived else 'catalog'
        if page:
            nav.append(('←',f'c8:{mode}:{kind}:{page-1}'))
        if len(cats)>8:
            nav.append(('→',f'c8:{mode}:{kind}:{page+1}'))
    if nav:
        buttons.append(nav)
    buttons.append([('🔎 Поиск',f'c8:search:{kind}')])
    if kind in ('stock','sale') and not archived:
        buttons.append([('➕ Новая позиция',f'c8:newname:{kind}')])
    if kind=='stock':
        buttons.append([('🗃 Удалённые позиции' if not archived else '📦 Активные позиции',f'c8:{"archive" if not archived else "catalog"}:stock:0')])
    heading = '🗃 Удалённые позиции' if archived else '📦 Единый каталог'
    hint = '\nНайдено больше 8 позиций — уточните поиск.' if search and len(cats)>8 else ''
    await _screen_text(cb,f'<b>{heading}</b>\nВыберите предмет. Количество указано за вычетом резерва.'+ ('\nНичего не найдено.' if not cats else '')+hint,reply_markup=keyboard(*buttons))


async def choose_item(cb, state, db, config, kind, item_id):
    await in_topic(cb.message,db,'market' if kind=='sale' else 'storage')
    cat = await db.catalogue_get(item_id)
    if kind=='stock':
        await permission(cb.from_user.id,'storage.manage',db,config)
        buttons = [[('✏️ Остаток',f'c8:stockqty:{item_id}'),('✏️ Название',f'c8:rename:{item_id}')],
                   [('♻️ Восстановить' if cat['archived'] else '🗑 Удалить из списка', f'c8:archiveask:{item_id}')]]
        await _screen_text(cb,f"<b>{escape(cat['name'])}</b>\nВсего: {cat['quantity']}\nРезерв: {cat['reserved']}\nСвободно: {cat['quantity']-cat['reserved']}",reply_markup=keyboard(*buttons))
        return
    await internal(cb.from_user.id,db)
    if cat['archived']:
        raise ValueError('Позиция удалена из активного каталога.')
    if kind=='request':
        await start_flow(cb,state,'request_qty',f"<b>{escape(cat['name'])}</b>\nСвободно: {cat['quantity']-cat['reserved']}.\nСколько единиц нужно?",item_id=item_id)
    elif kind=='sale':
        await start_flow(cb,state,'sale_photo',f"<b>{escape(cat['name'])}</b>\nПрикрепите одну фотографию предмета как фото (не файл).",item_id=item_id)


async def _screen_photo(cb: CallbackQuery, png: bytes, caption: str, reply_markup=None, filename='screen.png') -> Message:
    """Keep a section on one visible message. Replace incompatible text screens instead of stacking messages."""
    media = InputMediaPhoto(media=BufferedInputFile(png, filename=filename), caption=caption, parse_mode='HTML')
    if cb.message.photo:
        try:
            return await cb.message.edit_media(media=media, reply_markup=reply_markup)
        except Exception:
            pass
    thread = cb.message.message_thread_id
    try:
        await cb.message.delete()
    except Exception:
        pass
    kwargs = {'message_thread_id': thread} if thread else {}
    return await cb.bot.send_photo(cb.message.chat.id, BufferedInputFile(png, filename=filename), caption=caption,
                                   reply_markup=reply_markup, **kwargs)


async def _screen_text(cb: CallbackQuery, text: str, reply_markup=None) -> Message:
    if not cb.message.photo and not cb.message.video and not cb.message.document:
        try:
            return await cb.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
    thread = cb.message.message_thread_id
    try:
        await cb.message.delete()
    except Exception:
        pass
    kwargs = {'message_thread_id': thread} if thread else {}
    return await cb.bot.send_message(cb.message.chat.id, text, reply_markup=reply_markup, **kwargs)


async def _screen_photo_id(cb: CallbackQuery, file_id: str, caption: str, reply_markup=None) -> Message:
    media = InputMediaPhoto(media=file_id, caption=caption, parse_mode='HTML')
    if cb.message.photo:
        try:
            return await cb.message.edit_media(media=media, reply_markup=reply_markup)
        except Exception:
            pass
    thread = cb.message.message_thread_id
    try:
        await cb.message.delete()
    except Exception:
        pass
    kwargs = {'message_thread_id': thread} if thread else {}
    return await cb.bot.send_photo(cb.message.chat.id, file_id, caption=caption, reply_markup=reply_markup, **kwargs)


def _warehouse_nav(back='c8:wh:home'):
    return [('⬅️ Назад', back), ('🏠 Склад', 'c8:wh:home')]


async def show_warehouse(cb: CallbackQuery, db, config, *, category_id=None, page=0):
    await in_topic(cb.message, db, 'storage')
    await internal(cb.from_user.id, db)
    categories = await db.warehouse_categories_list()
    rows = await db.warehouse_stock_rows(category_id, page, limit=12)
    shown = rows[:12]
    can_manage = await has_permission(cb.from_user.id, 'storage.manage', db, config)
    png = render_warehouse(shown, categories, selected_category=category_id)
    buttons = []
    if category_id is None:
        for cat in categories:
            buttons.append([(f"📁 {cat['name']} · {cat['item_count']}", f"c8:wh:cat:{cat['id']}:0")])
        buttons.append([('📋 Мои заявки', 'c8:requests:mine:0'), ('📝 Запросить', 'c8:wh:requestcats')])
        if can_manage:
            buttons.append([('🛠 Кладовщик', 'c8:wh:manage'), ('📜 Журнал', 'c8:wh:history:0')])
        buttons.append([('🏠 Главное меню', 'c8:wh:main')])
        caption = '<b>📦 Снаряжение группировки</b>\nАктуальный склад доступен всем участникам.'
    else:
        category = next((c for c in categories if c['id'] == category_id), None)
        if not category:
            raise ValueError('Раздел не найден.')
        for row in shown:
            buttons.append([(f"{row['name']} · {row['available']} свободно", f"c8:wh:item:{row['id']}:{category_id}:{page}")])
        nav=[]
        if page:
            nav.append(('◀️', f'c8:wh:cat:{category_id}:{page-1}'))
        if len(rows) > 12:
            nav.append(('▶️', f'c8:wh:cat:{category_id}:{page+1}'))
        if nav:
            buttons.append(nav)
        buttons.append(_warehouse_nav())
        caption = f"<b>📁 {escape(category['name'])}</b>\nНажмите предмет для подробностей или оформления заявки."
    await _screen_photo(cb, png, caption, keyboard(*buttons), filename='warehouse.png')


async def show_warehouse_item(cb: CallbackQuery, db, config, item_id: int, category_id: int, page: int = 0):
    await in_topic(cb.message, db, 'storage')
    await internal(cb.from_user.id, db)
    item = await db.catalogue_get(item_id)
    can_manage = await has_permission(cb.from_user.id, 'storage.manage', db, config)
    lines = [f"<b>📦 {escape(item['name'])}</b>", f"Раздел: {escape(item.get('category_name') or 'Без раздела')}",
             f"Всего: <b>{item['quantity']}</b>", f"Резерв: <b>{item['reserved']}</b>",
             f"Свободно: <b>{item['quantity']-item['reserved']}</b>"]
    buttons = [[('📝 Подать заявку', f'c8:wh:req:{item_id}')]]
    if can_manage:
        buttons += [[('📥 Принять', f'c8:wh:receive:{item_id}'), ('📤 Выдать', f'c8:wh:issue:{item_id}')],
                    [('📁 Перенести', f'c8:wh:movecat:{item_id}'), ('✏️ Название', f'c8:rename:{item_id}')],
                    [('🗑 Удалить', f'c8:archiveask:{item_id}')]]
    buttons.append(_warehouse_nav(f'c8:wh:cat:{category_id}:{page}'))
    await _screen_text(cb, '\n'.join(lines), keyboard(*buttons))


async def show_warehouse_manage(cb: CallbackQuery, db, config):
    await in_topic(cb.message, db, 'storage')
    await permission(cb.from_user.id, 'storage.manage', db, config)
    buttons = [
        [('📥 Приём', 'c8:wh:receivepick:0'), ('📤 Выдача', 'c8:wh:issuepick:0')],
        [('➕ Новый предмет', 'c8:wh:newitemcats'), ('📁 Разделы', 'c8:wh:categories')],
        [('📋 Очередь заявок', 'c8:requests:all:0'), ('📜 Журнал', 'c8:wh:history:0')],
        _warehouse_nav(),
    ]
    await _screen_text(cb, '<b>🛠 Раздел кладовщика</b>\n\nПриём и выдача сразу меняют остаток и фиксируются в журнале.', keyboard(*buttons))


@router.callback_query(F.data.in_({'gstorage:list','v7dip:list','v7dip:new','v7stock:list','v7stock:set'}))
async def legacy_entry(cb:CallbackQuery,db,config,state:FSMContext):
    if cb.data=='gstorage:list':
        personal = await db.get_topic('storage')
        communal = await db.get_warehouse_topic()
        if personal and personal != communal and personal == (cb.message.chat.id,int(cb.message.message_thread_id or 0)):
            from .group_handlers import group_storage_list
            return await group_storage_list(cb,db,config)
        await in_topic(cb.message,db,'storage')
        await internal(cb.from_user.id,db)
        await show_warehouse(cb,db,config)
    elif cb.data.startswith('v7dip:'):
        await in_topic(cb.message,db,'diplomacy')
        await internal(cb.from_user.id,db)
        await _screen_text(cb,'<b>📊 Дипломатия</b>',reply_markup=diplomacy_panel())
    else:
        storage = await db.get_warehouse_topic()
        if storage == (cb.message.chat.id,int(cb.message.message_thread_id or 0)):
            await show_catalog(cb,db,config,'stock' if cb.data.endswith('set') else 'request')
        else:
            await topic_answer(cb.message,'Управление общим складом перенесено в тему «Снаряжение группировки».')
    await cb.answer()


@router.callback_query(F.data.startswith('v7dip:'))
async def outdated_diplomacy(cb:CallbackQuery):
    await cb.answer('Эта карточка устарела. Откройте таблицу отношений в новой панели Дипломатии.',show_alert=True)


@router.callback_query(F.data.startswith('c8:'))
async def community_callback(cb:CallbackQuery,db,config,state:FSMContext):
    if not isinstance(cb.message,Message):
        return await cb.answer('Откройте тему группы.',show_alert=True)
    parts = cb.data.split(':')
    action = parts[1]
    uid = cb.from_user.id
    if action=='cancel':
        d=await state.get_data()
        if d.get('preview_id'):
            try:
                await cb.bot.delete_message(cb.message.chat.id,d['preview_id'])
            except Exception:
                pass
        return_to=d.get('return_to','c8:wh:home')
        await state.clear()
        await _screen_text(cb,'Ввод отменён.',keyboard([('⬅️ Назад',return_to),('🏠 Склад','c8:wh:home')]))
        return await cb.answer()
    if action=='wh':
        sub = parts[2] if len(parts) > 2 else 'home'
        if sub=='home':
            await show_warehouse(cb,db,config)
        elif sub=='cat':
            await show_warehouse(cb,db,config,category_id=int(parts[3]),page=int(parts[4]))
        elif sub=='item':
            await show_warehouse_item(cb,db,config,int(parts[3]),int(parts[4]),int(parts[5]))
        elif sub=='manage':
            await show_warehouse_manage(cb,db,config)
        elif sub=='main':
            await in_topic(cb.message,db,'storage')
            await internal(uid,db)
            await _screen_text(cb,'<b>📦 Снаряжение группировки — склад</b>\n\nПросмотр склада доступен всем участникам. Для работы используйте кнопку ниже.',storage_panel())
        elif sub=='requestcats':
            await in_topic(cb.message,db,'storage'); await internal(uid,db)
            cats=await db.warehouse_categories_list()
            buttons=[[(f"📁 {c['name']}",f"c8:wh:reqcat:{c['id']}:0")] for c in cats]
            buttons.append(_warehouse_nav())
            await _screen_text(cb,'<b>📝 Заявка на имущество</b>\nВыберите раздел:',keyboard(*buttons))
        elif sub=='reqcat':
            await in_topic(cb.message,db,'storage'); await internal(uid,db)
            cid,page=int(parts[3]),int(parts[4]); rows=await db.warehouse_stock_rows(cid,page,limit=10)
            buttons=[[(f"{r['name']} · {r['available']} свободно",f"c8:wh:req:{r['id']}")] for r in rows[:10] if r['available']>0]
            nav=[]
            if page: nav.append(('◀️',f'c8:wh:reqcat:{cid}:{page-1}'))
            if len(rows)>10: nav.append(('▶️',f'c8:wh:reqcat:{cid}:{page+1}'))
            if nav: buttons.append(nav)
            buttons.append(_warehouse_nav('c8:wh:requestcats'))
            await _screen_text(cb,'<b>📝 Выберите предмет</b>'+('\nНет доступных позиций.' if not buttons[:-1] else ''),keyboard(*buttons))
        elif sub=='req':
            item_id=int(parts[3]); item=await db.catalogue_get(item_id)
            await start_flow(cb,state,'request_qty',f"<b>{escape(item['name'])}</b>\nСвободно: {item['quantity']-item['reserved']}.\nСколько единиц нужно?",item_id=item_id,return_to='c8:wh:home')
        elif sub=='categories':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            cats=await db.warehouse_categories_list()
            buttons=[[(f"📁 {c['name']} · {c['item_count']}",f"c8:wh:catadmin:{c['id']}")] for c in cats]
            buttons.append([('➕ Новый раздел','c8:wh:newcat')]); buttons.append(_warehouse_nav('c8:wh:manage'))
            await _screen_text(cb,'<b>📁 Разделы склада</b>\nМожно добавлять собственные разделы.',keyboard(*buttons))
        elif sub=='catadmin':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            cid=int(parts[3]); cats=await db.warehouse_categories_list(include_archived=True); c=next((x for x in cats if x['id']==cid),None)
            if not c: raise ValueError('Раздел не найден.')
            buttons=[[('✏️ Переименовать',f'c8:wh:renamecat:{cid}')],[('🗑 Удалить раздел',f'c8:wh:archivecat:{cid}')],_warehouse_nav('c8:wh:categories')]
            await _screen_text(cb,f"<b>📁 {escape(c['name'])}</b>\nАктивных предметов: {c['item_count']}",keyboard(*buttons))
        elif sub=='newcat':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            await start_flow(cb,state,'wh_newcat','Введите название нового раздела:',return_to='c8:wh:categories')
        elif sub=='renamecat':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            await start_flow(cb,state,'wh_renamecat','Введите новое название раздела:',category_id=int(parts[3]),return_to=f'c8:wh:catadmin:{parts[3]}')
        elif sub=='archivecat':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            cid=int(parts[3]); await db.warehouse_category_edit(cid,uid,archived=True)
            await _screen_text(cb,'Раздел удалён. История операций сохранена.',keyboard(_warehouse_nav('c8:wh:categories')))
        elif sub=='newitemcats':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            cats=await db.warehouse_categories_list()
            buttons=[[(f"📁 {c['name']}",f"c8:wh:newitemcat:{c['id']}")] for c in cats]; buttons.append(_warehouse_nav('c8:wh:manage'))
            await _screen_text(cb,'<b>➕ Новый предмет</b>\nСначала выберите раздел:',keyboard(*buttons))
        elif sub=='newitemcat':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            await start_flow(cb,state,'wh_newitem_name','Введите название нового предмета:',category_id=int(parts[3]),return_to='c8:wh:manage')
        elif sub in ('receivepick','issuepick'):
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            page=int(parts[3]); rows=await db.warehouse_stock_rows(None,page,limit=10)
            verb='receive' if sub=='receivepick' else 'issue'
            buttons=[[(f"{r['name']} · {r['available']} свободно",f"c8:wh:{verb}:{r['id']}")] for r in rows[:10]]
            nav=[]
            if page: nav.append(('◀️',f'c8:wh:{sub}:{page-1}'))
            if len(rows)>10: nav.append(('▶️',f'c8:wh:{sub}:{page+1}'))
            if nav: buttons.append(nav)
            buttons.append(_warehouse_nav('c8:wh:manage'))
            await _screen_text(cb,'<b>'+('📥 Приём имущества' if verb=='receive' else '📤 Выдача имущества')+'</b>\nВыберите предмет:',keyboard(*buttons))
        elif sub=='receive':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            item=await db.catalogue_get(int(parts[3])); await start_flow(cb,state,'wh_receive_qty',f"<b>📥 {escape(item['name'])}</b>\nВведите количество для приёма:",item_id=item['id'],return_to='c8:wh:manage')
        elif sub=='issue':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            item=await db.catalogue_get(int(parts[3])); await start_flow(cb,state,'wh_issue_qty',f"<b>📤 {escape(item['name'])}</b>\nСвободно: {item['quantity']-item['reserved']}\nВведите количество для выдачи:",item_id=item['id'],return_to='c8:wh:manage')
        elif sub=='movecat':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            item_id=int(parts[3]); cats=await db.warehouse_categories_list()
            buttons=[[(f"📁 {c['name']}",f"c8:wh:move:{item_id}:{c['id']}")] for c in cats]; buttons.append(_warehouse_nav())
            await _screen_text(cb,'Выберите новый раздел для предмета:',keyboard(*buttons))
        elif sub=='move':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            item_id,cid=int(parts[3]),int(parts[4]); await db.warehouse_set_item_category(item_id,cid,uid)
            await _screen_text(cb,'Предмет перенесён в другой раздел.',keyboard(_warehouse_nav(f'c8:wh:cat:{cid}:0')))
        elif sub=='history':
            await in_topic(cb.message,db,'storage'); await permission(uid,'storage.manage',db,config)
            page=int(parts[3]); rows=await db.warehouse_movements_list(page,limit=8)
            labels={'receive':'📥 Приём','issue':'📤 Выдача','request_issue':'📦 По заявке','correction':'✏️ Коррекция'}
            lines=['<b>📜 Журнал склада</b>']
            for r in rows[:8]:
                who=escape(r.get('actor_nick') or str(r['actor_id'])); cp=f" → {escape(r['counterparty'])}" if r.get('counterparty') else ''
                note=f" · {escape(r['note'])}" if r.get('note') else ''
                lines.append(f"{labels.get(r['movement_type'],r['movement_type'])} · <b>{escape(r['name'])}</b> × {r['quantity']}{cp}\n{r['created_at'][:16]} · {who}{note}")
            nav=[]
            if page: nav.append(('◀️',f'c8:wh:history:{page-1}'))
            if len(rows)>8: nav.append(('▶️',f'c8:wh:history:{page+1}'))
            buttons=[]
            if nav: buttons.append(nav)
            buttons.append(_warehouse_nav('c8:wh:manage'))
            await _screen_text(cb,'\n\n'.join(lines) if len(lines)>1 else '<b>📜 Журнал склада</b>\nПока пусто.',keyboard(*buttons))
        else:
            raise ValueError('Неизвестный экран склада.')
    elif action in ('catalog','archive'):
        await show_catalog(cb,db,config,parts[2],int(parts[3]),archived=action=='archive')
    elif action=='pick':
        await choose_item(cb,state,db,config,parts[2],int(parts[3]))
    elif action in ('search','newname'):
        kind = parts[2]
        await in_topic(cb.message,db,'market' if kind=='sale' else 'storage')
        if kind=='stock':
            await permission(uid,'storage.manage',db,config)
        else:
            await internal(uid,db)
        if action=='newname' and kind=='request':
            raise ValueError('Новые позиции создаёт кладовщик.')
        await start_flow(cb,state,action,'Введите название или часть названия (до 80 символов):',catalog_kind=kind)
    elif action in ('stockqty','rename','archiveask','archiveyes'):
        await in_topic(cb.message,db,'storage')
        await permission(uid,'storage.manage',db,config)
        cat = await db.catalogue_get(int(parts[2]))
        if action in ('stockqty','rename'):
            await start_flow(cb,state,action,('Введите новый общий остаток (включая резерв):' if action=='stockqty' else 'Введите новое название. Оно обновится и в других разделах:'),item_id=cat['id'])
        elif action=='archiveask':
            await _screen_text(cb,f"{'Восстановить' if cat['archived'] else 'Удалить из активного списка'} «{escape(cat['name'])}»? История сохранится.",reply_markup=keyboard([('Подтвердить',f'c8:archiveyes:{cat["id"]}:{1-int(cat["archived"])}')],_warehouse_nav()))
        else:
            await db.catalogue_edit(cat['id'],uid,archived=bool(int(parts[3])))
            await _screen_text(cb,'Каталог обновлён.',keyboard(_warehouse_nav()))
    elif action=='requests':
        await in_topic(cb.message,db,'storage')
        await internal(uid,db)
        mode,page = parts[2],int(parts[3])
        if mode!='mine':
            await permission(uid,'storage.manage',db,config)
        requests = await db.warehouse_list(page,uid if mode=='mine' else None,-1 if mode=='mine' else mode=='history')
        buttons = [[(f"#{r['id']} {r['name']} × {r['quantity']} · {r['status']}",f'c8:request:{r["id"]}')] for r in requests[:8]]
        nav=[]
        if page:
            nav.append(('←',f'c8:requests:{mode}:{page-1}'))
        if len(requests)>8:
            nav.append(('→',f'c8:requests:{mode}:{page+1}'))
        buttons.append(nav)
        buttons.append(_warehouse_nav('c8:wh:home'))
        await _screen_text(cb,'<b>📋 Заявки на выдачу</b>'+('\nСписок пуст.' if not requests else ''),reply_markup=keyboard(*buttons))
    elif action in ('request','req'):
        await in_topic(cb.message,db,'storage')
        await internal(uid,db)
        rid = int(parts[-1])
        if action=='req':
            await db.warehouse_transition(rid,parts[2],uid)
        result = await db.community_rows('SELECT r.*,c.name,p.game_nickname FROM warehouse_requests r JOIN catalogue c ON c.id=r.item_id JOIN players p ON p.telegram_id=r.requester_id WHERE r.id=?',(rid,))
        if not result:
            raise ValueError('Заявка не найдена.')
        rk=request_keyboard(result[0])
        rows = list(rk.inline_keyboard) if rk else []
        rows.append([
            InlineKeyboardButton(text='⬅️ Назад', callback_data='c8:requests:mine:0' if result[0]['requester_id']==uid else 'c8:requests:all:0'),
            InlineKeyboardButton(text='🏠 Склад', callback_data='c8:wh:home'),
        ])
        await _screen_text(cb,request_text(result[0]),reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif action=='legacy_storage':
        await in_topic(cb.message,db,'storage')
        await permission(uid,'storage.manage',db,config)
        if await db.get_topic('storage') != await db.get_warehouse_topic():
            await topic_answer(cb.message,'Личные вещи сохранены в прежней теме хранения. Для работы с ними откройте там /storage_panel.')
        else:
            await topic_answer(cb.message,'Старый учёт вещей, принятых от игроков:',reply_markup=keyboard([('Принять личные вещи','gstorage:add'),('На хранении','c8:legacy_list')],[('Старые выдачи','gstorage:history')]))
    elif action=='legacy_list':
        await in_topic(cb.message,db,'storage')
        await permission(uid,'storage.manage',db,config)
        from .group_handlers import group_storage_list
        return await group_storage_list(cb.model_copy(update={'data':'gstorage:list'}),db,config)
    elif action=='ads':
        await in_topic(cb.message,db,'market')
        await internal(uid,db)
        mode,page=parts[2],int(parts[3])
        ads = await db.ad_list(page,uid if mode=='mine' else None)
        buttons=[[(f"#{r['id']} {r['name']} · {r['price']} ₽",f"c8:ad:{r['id']}")] for r in ads[:8]]
        nav=[]
        if page:
            nav.append(('←',f'c8:ads:{mode}:{page-1}'))
        if len(ads)>8:
            nav.append(('→',f'c8:ads:{mode}:{page+1}'))
        buttons.append(nav)
        await _screen_text(cb,'<b>🪧 Объявления</b>'+('\nПока пусто.' if not ads else ''),reply_markup=keyboard(*buttons))
    elif action in ('ad','adclose'):
        await in_topic(cb.message,db,'market')
        await internal(uid,db)
        aid=int(parts[-1])
        if action=='adclose':
            await db.ad_close(aid,uid,parts[2],admin=uid in config.admin_ids)
        result=await db.community_rows('SELECT a.*,c.name,p.game_nickname FROM sale_ads a JOIN catalogue c ON c.id=a.item_id JOIN players p ON p.telegram_id=a.seller_id WHERE a.id=?',(aid,))
        if not result:
            raise ValueError('Объявление не найдено.')
        r=result[0]
        ak=ad_keyboard(r)
        rows=list(ak.inline_keyboard) if ak else []
        rows.append([InlineKeyboardButton(text='⬅️ Назад',callback_data='c8:ads:all:0')])
        await _screen_photo_id(cb,r['photo_id'],ad_text(r),InlineKeyboardMarkup(inline_keyboard=rows))
    elif action in ('matrix','pair','paira','pairb','rel','faction_add','candidates','decide','dip_history'):
        await in_topic(cb.message,db,'diplomacy')
        await internal(uid,db)
        factions=await db.community_rows('SELECT * FROM factions ORDER BY id')
        names={r['id']:r['name'] for r in factions}
        if action=='matrix':
            relations=await db.community_rows('SELECT * FROM faction_relations')
            png=render_matrix(factions,relations)
            await _screen_photo(cb,png,'Отношения симметричны. «?» означает отсутствие данных. Враги союзников требуют решения Дипломата.',keyboard([('⬅️ Назад','v7dip:list')]),filename='diplomacy.png')
        elif action=='candidates':
            candidates=await db.ally_candidates()
            page=int(parts[2]); shown=candidates[page*6:page*6+6]
            lines=['<b>⚖️ Враги союзников</b>']
            buttons=[]
            for r in shown:
                decision=r['decision']
                status='Ожидает решения' if not decision else 'Включён в цели' if decision['approved'] else 'Не включён в цели'
                digest=hashlib.sha256(r['basis'].encode()).hexdigest()[:12]
                lines.append(f"<b>{escape(r['name'])}</b>\nВраждует с союзниками: {escape(r['allies'])}\n{status}")
                buttons.append([('✅ В цели',f'c8:decide:{r["id"]}:1:{digest}'),('🚫 Не включать',f'c8:decide:{r["id"]}:0:{digest}')])
            if not shown:
                lines.append('Кандидатов из врагов союзников нет.')
            nav=[]
            if page: nav.append(('◀️',f'c8:candidates:{page-1}'))
            if len(candidates)>(page+1)*6: nav.append(('▶️',f'c8:candidates:{page+1}'))
            if nav: buttons.append(nav)
            buttons.append([('⬅️ Назад','v7dip:list')])
            await _screen_text(cb,'\n\n'.join(lines),keyboard(*buttons))
        elif action=='decide':
            candidates=await db.ally_candidates()
            r=next((r for r in candidates if r['id']==int(parts[2])),None)
            if not r or hashlib.sha256(r['basis'].encode()).hexdigest()[:12]!=parts[4]:
                raise ValueError('Состав союзников изменился. Откройте кандидатов заново.')
            await db.ally_decide(r['id'],r['basis'],bool(int(parts[3])),uid)
            await _screen_text(cb,'Решение Дипломата сохранено. Список целей обновлён.',reply_markup=diplomacy_panel())
        elif action=='dip_history':
            history=await db.community_rows("SELECT a.*,p.game_nickname FROM audit_log a LEFT JOIN players p ON p.telegram_id=a.actor_id WHERE action LIKE 'diplomacy.%' ORDER BY a.id DESC LIMIT 15")
            lines=[]
            for r in history:
                details=r['details'] or ''
                if r['action']=='diplomacy.pair':
                    pair,rel=details.split('='); a,b=map(int,pair.split('/'))
                    details=f"{names.get(a,a)} / {names.get(b,b)}: {dict(ally='союз',neutral='нейтралитет',war='война',unknown='нет данных')[rel]}"
                lines.append(f"{r['created_at'][:16]} · {escape(r['game_nickname'] or str(r['actor_id']))}\n{escape(details)}")
            await _screen_text(cb,'<b>📜 История дипломатии</b>\n\n'+ ('\n\n'.join(lines) or 'Пока пуста.'),reply_markup=keyboard([('⬅️ Назад','v7dip:list')]))
        else:
            await permission(uid,'diplomacy.manage',db,config)
            if action=='faction_add':
                await start_flow(cb,state,'faction_add','Название новой группировки (до 80 символов):')
            elif action=='pair':
                await _screen_text(cb,'Выберите первую группировку:',reply_markup=keyboard(*[[(r['name'],f'c8:paira:{r["id"]}')] for r in factions], [('⬅️ Назад','v7dip:list')]))
            elif action=='paira':
                a=int(parts[2])
                if a not in names:
                    raise ValueError('Группировка не найдена.')
                await _screen_text(cb,f'Отношения {escape(names[a])} с:',reply_markup=keyboard(*[[(r['name'],f'c8:pairb:{a}:{r["id"]}')] for r in factions if r['id']!=a], [('⬅️ Назад','c8:pair')]))
            elif action=='pairb':
                a,b=int(parts[2]),int(parts[3])
                if a not in names or b not in names or a==b:
                    raise ValueError('Выберите разные группировки.')
                await start_flow(cb,state,'relation',f'{escape(names[a])} ↔ {escape(names[b])}',a=a,b=b)
                d=await state.get_data()
                await _screen_text(cb,'Выберите новый статус:',reply_markup=keyboard(*[[(label,f'c8:rel:{rel}:{d["nonce"]}')] for rel,label in [('ally','🟢 Союз'),('neutral','🟡 Нейтралитет'),('war','🔴 Война'),('unknown','? Нет данных')]], [('⬅️ Назад','c8:pair')]))
            elif action=='rel':
                d=await state.get_data()
                if d.get('kind')!='relation' or d.get('nonce')!=parts[3]:
                    raise ValueError('Этот выбор устарел. Выберите пару заново.')
                await db.relation_set(d['a'],d['b'],parts[2],uid)
                await state.clear()
                await _screen_text(cb,'Отношения и список целей обновлены.',reply_markup=diplomacy_panel())
    elif action=='submit':
        d=await state.get_data()
        if await state.get_state()!=Flow.confirm.state or d.get('nonce')!=parts[2]:
            raise ValueError('Форма уже отправлена или устарела.')
        await in_topic(cb.message,db,'market' if d['kind']=='sale' else 'storage')
        await internal(uid,db)
        if d['kind']=='sale':
            result=await db.ad_create(uid,d['item_id'],d['photo'],d['price'],d['description'],d['nonce'])
            text=f'Объявление #{result} сохранено. Карточка с фотографией появится на доске.'
        else:
            result=await db.warehouse_create(uid,d['item_id'],d['quantity'],d['reason'],d['nonce'])
            text=f'Заявка #{result} сохранена и ожидает Лидера / Заместителя. После одобрения она поступит Кладовщику.'
        await state.clear()
        if d.get('preview_id'):
            try:
                await cb.bot.delete_message(cb.message.chat.id,d['preview_id'])
            except Exception:
                pass
        await _screen_text(cb,text,reply_markup=storage_panel() if d['kind']!='sale' else market_panel())
    else:
        raise ValueError('Кнопка устарела. Откройте панель раздела.')
    await cb.answer()


@router.message(StateFilter(Flow), Command('cancel'))
async def cancel_message(message:Message,state:FSMContext):
    d=await state.get_data()
    if d.get('preview_id'):
        try:
            await message.bot.delete_message(message.chat.id,d['preview_id'])
        except Exception:
            pass
    await state.clear()
    await topic_answer(message,'Ввод отменён.')


@router.message(Flow.value, ~F.text.startswith('/'))
async def flow_value(message:Message,state:FSMContext,db,config):
    if not message.from_user:
        return
    d=await state.get_data()
    if (message.chat.id,int(message.message_thread_id or 0))!=(d.get('chat'),d.get('thread')):
        return
    uid=message.from_user.id
    kind=d['kind']
    module='market' if kind.startswith('sale') or d.get('catalog_kind')=='sale' else 'diplomacy' if kind in ('faction_add','relation') else 'storage'
    await in_topic(message,db,module)
    if kind in ('stockqty','rename') or kind.startswith('wh_') or d.get('catalog_kind')=='stock':
        await permission(uid,'storage.manage',db,config)
    elif kind in ('faction_add','relation'):
        await permission(uid,'diplomacy.manage',db,config)
    else:
        await internal(uid,db)
    text=(message.text or '').strip()
    prompt=None
    if kind=='wh_newcat':
        await db.warehouse_category_save(text,uid)
        await state.clear()
        prompt='✅ Новый раздел создан.'
    elif kind=='wh_renamecat':
        await db.warehouse_category_edit(d['category_id'],uid,name=text)
        await state.clear()
        prompt='✅ Раздел переименован.'
    elif kind=='wh_newitem_name':
        cat=await db.catalogue_save(text)
        await db.warehouse_set_item_category(cat['id'],d['category_id'],uid)
        await state.update_data(kind='wh_newitem_qty',item_id=cat['id'])
        prompt=f"{escape(cat['name'])}: введите начальный остаток (0 или больше):"
    elif kind=='wh_newitem_qty':
        if not text.isdecimal() or len(text)>7 or not 0<=int(text)<=1000000:
            raise ValueError('Остаток должен быть целым числом от 0 до 1000000.')
        if int(text)>0:
            await db.warehouse_receive(d['item_id'],int(text),uid,'Начальный остаток')
        await state.clear()
        prompt='✅ Предмет добавлен на склад.'
    elif kind=='wh_receive_qty':
        if not text.isdecimal() or len(text)>7 or not 1<=int(text)<=1000000:
            raise ValueError('Введите целое количество от 1 до 1000000.')
        await state.update_data(kind='wh_receive_note',quantity=int(text))
        prompt='Комментарий к приёму (или «-», если не нужен):'
    elif kind=='wh_receive_note':
        note='' if text=='-' else text
        await db.warehouse_receive(d['item_id'],d['quantity'],uid,note)
        await state.clear()
        prompt='✅ Приём оформлен. Остаток и журнал обновлены.'
    elif kind=='wh_issue_qty':
        if not text.isdecimal() or len(text)>7 or not 1<=int(text)<=1000000:
            raise ValueError('Введите целое количество от 1 до 1000000.')
        await state.update_data(kind='wh_issue_to',quantity=int(text))
        prompt='Кому выдано имущество? Укажите ник или имя:'
    elif kind=='wh_issue_to':
        if not 1<=len(text)<=100:
            raise ValueError('Укажите получателя: 1–100 символов.')
        await state.update_data(kind='wh_issue_note',counterparty=text)
        prompt='Комментарий к выдаче (или «-», если не нужен):'
    elif kind=='wh_issue_note':
        note='' if text=='-' else text
        await db.warehouse_direct_issue(d['item_id'],d['quantity'],uid,d['counterparty'],note)
        await state.clear()
        prompt='✅ Выдача оформлена. Остаток и журнал обновлены.'
    elif kind=='search':
        if not 1<=len(text)<=80:
            raise ValueError('Введите от 1 до 80 символов.')
        await state.clear()
        # Reuse the same catalogue renderer for a message-origin search.
        cb=CallbackQuery(id='search',from_user=message.from_user,chat_instance='search',message=message,data='').as_(message.bot)
        await show_catalog(cb,db,config,d['catalog_kind'],search=text)
        return
    if kind=='newname':
        cat=await db.catalogue_save(text)
        await state.clear()
        if d['catalog_kind']=='stock':
            await state.set_state(Flow.value)
            await state.set_data({**d,'kind':'stockqty','item_id':cat['id']})
            prompt=f"{escape(cat['name'])}: введите общий остаток (0 или больше):"
        else:
            await state.set_state(Flow.value)
            await state.set_data({**d,'kind':'sale_photo','item_id':cat['id']})
            prompt=f"{escape(cat['name'])}: прикрепите одну фотографию как фото."
    elif kind=='stockqty':
        if not text.isdecimal() or len(text)>7 or not 0<=int(text)<=1000000:
            raise ValueError('Остаток должен быть целым числом от 0 до 1000000.')
        cat=await db.catalogue_get(d['item_id'])
        await db.gp_stock_upsert(cat['name'],int(text),uid)
        await state.clear()
        prompt='Остаток сохранён.'
    elif kind=='rename':
        await db.catalogue_edit(d['item_id'],uid,name=text)
        await state.clear()
        prompt='Название обновлено в общем каталоге, на складе и в объявлениях.'
    elif kind=='request_qty':
        if not text.isdecimal() or len(text)>7 or not 1<=int(text)<=1000000:
            raise ValueError('Введите целое количество от 1 до 1000000.')
        await state.update_data(kind='request_reason',quantity=int(text))
        prompt='Для чего требуется имущество? До 500 символов:'
    elif kind=='request_reason':
        if not 1<=len(text)<=500:
            raise ValueError('Укажите причину: 1–500 символов.')
        await state.update_data(kind='request',reason=text)
        await state.set_state(Flow.confirm)
    elif kind=='sale_photo':
        if not message.photo or message.media_group_id:
            raise ValueError('Прикрепите одну фотографию как фото, без альбома и без отправки файлом.')
        await state.update_data(kind='sale_price',photo=message.photo[-1].file_id)
        prompt='Цена в игровых рублях, целое число (например, 15000):'
    elif kind=='sale_price':
        value=text.replace(' ','').replace('\u00a0','')
        if not value.isdecimal() or len(value)>13 or not 1<=int(value)<=1000000000000:
            raise ValueError('Цена должна быть целым числом от 1 до 1000000000000 ₽.')
        await state.update_data(kind='sale_description',price=int(value))
        prompt='Опишите предмет и его состояние (до 600 символов):'
    elif kind=='sale_description':
        if not 1<=len(text)<=600:
            raise ValueError('Описание должно содержать 1–600 символов.')
        await state.update_data(kind='sale',description=text)
        await state.set_state(Flow.confirm)
    elif kind=='faction_add':
        await db.faction_add(text,uid)
        await state.clear()
        prompt='Группировка добавлена. Задайте её отношения через таблицу.'
    elif kind=='relation':
        prompt='Выберите отношение кнопкой выше.'
    await safe_delete(message)
    if await state.get_state()==Flow.confirm.state:
        d=await state.get_data()
        cat=await db.catalogue_get(d['item_id'])
        markup=keyboard([('✅ Опубликовать' if d['kind']=='sale' else '✅ Отправить руководству',f'c8:submit:{d["nonce"]}')],[('Отмена','c8:cancel')])
        if d['kind']=='sale':
            kwargs={'message_thread_id':message.message_thread_id} if message.message_thread_id else {}
            preview = await message.bot.send_photo(message.chat.id,d['photo'],caption=f"<b>Предпросмотр объявления</b>\n{escape(cat['name'])}\n{d['price']} ₽\n\n{escape(d['description'])}",reply_markup=markup,**kwargs)
        else:
            preview = await topic_answer(message,f"<b>Заявка на выдачу</b>\n{escape(cat['name'])} × {d['quantity']}\n{escape(d['reason'])}",reply_markup=markup)
        await state.update_data(preview_id=preview.message_id)
        schedule_delete(message.bot,preview.chat.id,preview.message_id,600)
    elif prompt:
        current = await state.get_state()
        d2 = await state.get_data() if current else d
        markup = keyboard([('❌ Отмена','c8:cancel')]) if current else keyboard([('⬅️ Назад', d.get('return_to','c8:wh:manage')),('🏠 Склад','c8:wh:home')])
        prompt_id = d.get('prompt_id')
        if prompt_id:
            try:
                await message.bot.edit_message_text(prompt, message.chat.id, prompt_id, reply_markup=markup)
            except Exception:
                await topic_answer(message,prompt,reply_markup=markup)
        else:
            await topic_answer(message,prompt,reply_markup=markup)

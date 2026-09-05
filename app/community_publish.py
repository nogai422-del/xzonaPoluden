"""Persistent Telegram cards, with retries after failures and restarts."""
import asyncio
import hashlib
import json
import logging
from html import escape

from aiogram.exceptions import TelegramBadRequest

from .community_views import request_text, request_keyboard, ad_text, ad_keyboard

log = logging.getLogger(__name__)


async def persistent_card(bot, db, key, topic, text, markup=None, photo=None):
    ref_key = f'community:card:{key}:{topic[0]}:{topic[1]}'
    raw = await db.get_setting(ref_key)
    ref = json.loads(raw) if raw else {}
    digest = hashlib.sha256((text + str(markup) + str(photo)).encode()).hexdigest()
    if ref.get('digest')==digest:
        return
    mid = ref.get('message_id')
    if mid:
        try:
            if photo:
                await bot.edit_message_caption(chat_id=topic[0],message_id=mid,caption=text,reply_markup=markup)
            else:
                await bot.edit_message_text(chat_id=topic[0],message_id=mid,text=text,reply_markup=markup)
        except TelegramBadRequest as exc:
            message = str(exc).casefold()
            if 'message is not modified' in message:
                pass
            elif 'message to edit not found' in message:
                mid = None
            else:
                raise
    if not mid:
        kwargs = dict(chat_id=topic[0],reply_markup=markup)
        if topic[1]:
            kwargs['message_thread_id'] = topic[1]
        sent = await (bot.send_photo(photo=photo,caption=text,**kwargs) if photo else bot.send_message(text=text,**kwargs))
        mid = sent.message_id
    await db.set_setting(ref_key, json.dumps(dict(message_id=mid,digest=digest)))


async def publish_community(bot, db):
    # A failed card is retried independently; it cannot block the other modules.
    async def publish(key, topic, text, markup=None, photo=None):
        try:
            await persistent_card(bot,db,key,topic,text,markup,photo)
        except Exception as exc:
            log.warning('Community card %s failed: %s', key, type(exc).__name__)

    storage = await db.get_warehouse_topic()
    if storage:
        people = await db.community_rows("SELECT telegram_id,game_nickname,position_code FROM players WHERE position_status='approved' AND position_code IN ('leader','deputy_leader','storekeeper')")
        requests = await db.community_rows("SELECT r.*,c.name,p.game_nickname FROM warehouse_requests r JOIN catalogue c ON c.id=r.item_id JOIN players p ON p.telegram_id=r.requester_id ORDER BY r.id")
        for r in requests:
            target_roles = {'leader','deputy_leader'} if r['status']=='pending' else {'storekeeper'} if r['status']=='approved' else set()
            mentions = [f'<a href="tg://user?id={p["telegram_id"]}">{escape(p["game_nickname"])}</a>' for p in people if p['position_code'] in target_roles]
            suffix = '\n\nК рассмотрению: '+', '.join(mentions) if mentions else ''
            if target_roles and not mentions:
                suffix = '\n\n⚠️ Руководству нужно назначить '+('Кладовщика.' if r['status']=='approved' else 'Лидера / Заместителя.')
            # Distinct stage cards send a new Telegram notification to the next
            # responsible role. The original request is updated to its final state.
            await publish(f'request:{r["id"]}',storage,request_text(r)+suffix,request_keyboard(r))
            if r['reviewed_by'] and r['status'] in ('approved','issued','cancelled'):
                await publish(f'issue:{r["id"]}',storage,request_text(r)+suffix,request_keyboard(r))
    market = await db.get_topic('market')
    if market:
        ads = await db.community_rows("SELECT a.*,c.name,p.game_nickname FROM sale_ads a JOIN catalogue c ON c.id=a.item_id JOIN players p ON p.telegram_id=a.seller_id ORDER BY a.id")
        for r in ads:
            await publish(f'ad:{r["id"]}',market,ad_text(r),ad_keyboard(r),r['photo_id'])
    target_topic = await db.get_topic('targets')
    if target_topic:
        targets = await db.community_rows("SELECT t.* FROM targets t JOIN faction_targets f ON t.id=f.target_id WHERE t.status='active' ORDER BY t.target_name")
        text = '<b>🏴 Группировки в списке целей</b>\n\n'
        text += '\n'.join(f'<b>{escape(r["target_name"])}</b> — {escape(r["reason"])}' for r in targets) or 'Активных целей из дипломатии нет.'
        text += '\n\nОбновляется по дипломатии. Враг союзника появляется только после решения Дипломата.'
        await publish('faction_targets',target_topic,text)


async def community_publish_loop(bot, db):
    while True:
        try:
            await publish_community(bot,db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Community publisher failed: %s', type(exc).__name__)
        await asyncio.sleep(15)

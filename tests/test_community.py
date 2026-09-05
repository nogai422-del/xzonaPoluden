import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.db import Database
from app.community_db import name_key
from app.community_publish import publish_community
from app.community_views import render_matrix
from app.roles import parse_position


class CommunityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name)/'test.db')
        await self.db.init()
        roles={1:'leader',2:'deputy_leader',3:'storekeeper',4:'private',5:'diplomat',6:'trader',7:'external_leader',8:'private'}
        for uid,role in roles.items():
            await self.db.upsert_player(uid,None,f'User {uid}',f'Ник{uid}')
            with sqlite3.connect(self.db.path) as conn:
                conn.execute("UPDATE players SET position_code=?,position_status='approved' WHERE telegram_id=?",(role,uid))
        self.cat = await self.db.catalogue_save('Аптечка')
        await self.db.gp_stock_upsert('аптечка',10,3)
        self.factions={r['name']:r['id'] for r in await self.db.community_rows('SELECT * FROM factions')}

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def request(self,qty=3,key='one'):
        return await self.db.warehouse_create(4,self.cat['id'],qty,'Для рейда',key)

    async def relation(self,a,b,status):
        await self.db.relation_set(self.factions[a],self.factions[b],status,5)

    async def active_targets(self):
        return await self.db.community_rows("SELECT * FROM targets WHERE status='active'")

    async def test_warehouse_full_workflow_and_duplicate_issue(self):
        rid=await self.request()
        with self.assertRaises(ValueError):
            await self.db.warehouse_transition(rid,'issue',3)
        await self.db.warehouse_transition(rid,'approve',2)
        cat=await self.db.catalogue_get(self.cat['id'])
        self.assertEqual((cat['quantity'],cat['reserved']),(10,3))
        for uid in (1,2,4,5,6,7):
            with self.assertRaises(ValueError):
                await self.db.warehouse_transition(rid,'issue',uid)
        await self.db.warehouse_transition(rid,'issue',3)
        with self.assertRaises(ValueError):
            await self.db.warehouse_transition(rid,'issue',3)
        cat=await self.db.catalogue_get(self.cat['id'])
        self.assertEqual((cat['quantity'],cat['reserved']),(7,0))
        result=(await self.db.warehouse_list(history=True))[0]
        self.assertEqual((result['reviewed_by'],result['issued_by']),(2,3))

    async def test_only_leadership_can_approve(self):
        rid=await self.request()
        for uid in (3,4,5,6,7):
            with self.assertRaises(ValueError):
                await self.db.warehouse_transition(rid,'approve',uid)
        await self.db.warehouse_transition(rid,'approve',1)

    async def test_concurrent_approvals_cannot_overbook(self):
        a=await self.request(7,'a'); b=await self.request(7,'b')
        result=await asyncio.gather(self.db.warehouse_transition(a,'approve',1),self.db.warehouse_transition(b,'approve',2),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,ValueError) for x in result),1)
        cat=await self.db.catalogue_get(self.cat['id'])
        self.assertEqual(cat['reserved'],7)

    async def test_concurrent_issue_debits_once(self):
        rid=await self.request()
        await self.db.warehouse_transition(rid,'approve',1)
        result=await asyncio.gather(self.db.warehouse_transition(rid,'issue',3),self.db.warehouse_transition(rid,'issue',3),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,ValueError) for x in result),1)
        self.assertEqual((await self.db.catalogue_get(self.cat['id']))['quantity'],7)

    async def test_cancel_releases_and_reject_never_reserves(self):
        rid=await self.request()
        await self.db.warehouse_transition(rid,'approve',1)
        with self.assertRaises(ValueError):
            await self.db.warehouse_transition(rid,'cancel',8)
        with self.assertRaises(ValueError):
            await self.db.gp_stock_upsert('Аптечка',2,3)
        await self.db.warehouse_transition(rid,'cancel',4)
        self.assertEqual((await self.db.catalogue_get(self.cat['id']))['reserved'],0)
        rid2=await self.request(key='two')
        await self.db.warehouse_transition(rid2,'reject',2)
        with self.assertRaises(ValueError):
            await self.db.warehouse_transition(rid2,'approve',1)
        self.assertEqual((await self.db.catalogue_get(self.cat['id']))['quantity'],10)

    async def test_invalid_and_external_requests(self):
        for qty in (0,-1,1000001):
            with self.assertRaises(ValueError):
                await self.request(qty)
        with self.assertRaises(ValueError):
            await self.db.warehouse_create(7,self.cat['id'],1,'Test','x')
        with self.assertRaises(ValueError):
            await self.request(11)

    async def test_role_revocation_blocks_pending_actions(self):
        rid=await self.request()
        with sqlite3.connect(self.db.path) as conn:
            conn.execute("UPDATE players SET position_status='pending' WHERE telegram_id=1")
        with self.assertRaises(ValueError):
            await self.db.warehouse_transition(rid,'approve',1)

    async def test_request_idempotency_and_restart(self):
        rid=await self.request()
        self.assertEqual(await self.request(),rid)
        await self.db.warehouse_transition(rid,'approve',1)
        db=Database(self.db.path)
        await db.init()
        self.assertEqual((await db.catalogue_get(self.cat['id']))['reserved'],3)
        await db.warehouse_transition(rid,'issue',3)

    async def test_cancel_racing_issue_keeps_inventory_consistent(self):
        rid=await self.request()
        await self.db.warehouse_transition(rid,'approve',1)
        results=await asyncio.gather(self.db.warehouse_transition(rid,'issue',3),self.db.warehouse_transition(rid,'cancel',4),return_exceptions=True)
        self.assertEqual(sum(isinstance(x,ValueError) for x in results),1)
        row=(await self.db.warehouse_list(history=True))[0]
        stock=await self.db.catalogue_get(self.cat['id'])
        self.assertEqual(stock['reserved'],0)
        self.assertEqual(stock['quantity'],7 if row['status']=='issued' else 10)

    async def test_catalogue_rename_preserves_old_order_reservations(self):
        order=await self.db.create_market_order(requester_id=4,items=[('аптечка',2)],comment=None,merchant_target=None)
        await self.db.gp_stock_reserve([('Аптечка',2)],6)
        await self.db.catalogue_edit(self.cat['id'],3,name='Медкомплект')
        _,items=await self.db.get_market_order(order)
        self.assertEqual(items[0].item_name,'Медкомплект')
        ok,_=await self.db.gp_stock_consume_reserved([(i.item_name,i.quantity) for i in items],6)
        self.assertTrue(ok)
        self.assertEqual((await self.db.catalogue_get(self.cat['id']))['quantity'],8)

    async def test_warehouse_uses_common_topic_and_preserves_personal(self):
        await self.db.set_topic('storage',-1001,9)
        self.assertEqual(await self.db.get_warehouse_topic(),(-1001,9))
        await self.db.set_topic('gp_stock',-1001,10)
        self.assertEqual(await self.db.get_warehouse_topic(),(-1001,10))
        self.assertEqual(await self.db.get_topic('storage'),(-1001,9))

    async def test_ad_publication_and_closed_caption_refresh(self):
        await self.db.set_topic('market',-1001,20)
        aid=await self.db.ad_create(4,self.cat['id'],'saved-photo',500,'Состояние отличное','photo_ad')
        bot=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=55)),edit_message_caption=AsyncMock())
        await publish_community(bot,self.db)
        self.assertEqual(bot.send_photo.call_args.kwargs['photo'],'saved-photo')
        self.assertEqual(bot.send_photo.call_args.kwargs['message_thread_id'],20)
        await self.db.ad_close(aid,4,'sold')
        await publish_community(bot,self.db)
        self.assertIn('Продано',bot.edit_message_caption.call_args.kwargs['caption'])
        self.assertIsNone(bot.edit_message_caption.call_args.kwargs['reply_markup'])

    async def test_catalogue_normalization_rename_archive_and_restore(self):
        self.assertEqual((await self.db.catalogue_save(' АПТЕЧКА  '))['id'],self.cat['id'])
        a=await self.db.catalogue_save('Ёж'); b=await self.db.catalogue_save('еж')
        self.assertEqual(a['id'],b['id'])
        ad=await self.db.ad_create(4,self.cat['id'],'photo-id',100,'Описание','ad')
        rid=await self.request()
        await self.db.warehouse_transition(rid,'approve',1)
        await self.db.catalogue_edit(self.cat['id'],3,name='Медкомплект')
        self.assertEqual((await self.db.ad_list())[0]['name'],'Медкомплект')
        await self.db.warehouse_transition(rid,'issue',3)
        with self.assertRaises(ValueError):
            await self.db.catalogue_edit(self.cat['id'],3,archived=True)
        await self.db.gp_stock_upsert('Медкомплект',0,3)
        await self.db.catalogue_edit(self.cat['id'],3,archived=True)
        self.assertNotIn(self.cat['id'],[r['id'] for r in await self.db.catalogue_list()])
        self.assertEqual((await self.db.ad_list())[0]['id'],ad)
        await self.db.catalogue_edit(self.cat['id'],3,archived=False)
        self.assertEqual((await self.db.catalogue_get(self.cat['id']))['archived'],0)

    async def test_ads_photo_required_owner_and_moderator(self):
        for photo,price,description in [('',100,'ok'),('photo',0,'ok'),('photo',100,'')]:
            with self.assertRaises(ValueError):
                await self.db.ad_create(4,self.cat['id'],photo,price,description,'bad')
        aid=await self.db.ad_create(4,self.cat['id'],'photo',100,'Описание','ad')
        self.assertEqual(await self.db.ad_create(4,self.cat['id'],'photo',100,'Описание','ad'),aid)
        with self.assertRaises(ValueError):
            await self.db.ad_close(aid,8,'sold')
        await self.db.ad_close(aid,4,'sold')
        self.assertEqual(await self.db.ad_list(),[])
        self.assertEqual((await self.db.ad_list(user=4))[0]['status'],'sold')
        with self.assertRaises(ValueError):
            await self.db.ad_close(aid,4,'removed')

    async def test_direct_war_target_and_peace(self):
        await self.relation('Полдень','Долг','war')
        targets=await self.active_targets()
        self.assertEqual([t['target_name'] for t in targets],['Долг'])
        await self.relation('Долг','Полдень','war')
        self.assertEqual(len(await self.active_targets()),1)
        self.assertFalse(await self.db.target_set_status(targets[0]['id'],'done',1))
        with self.assertRaises(ValueError):
            await self.db.target_create('ДОЛГ',None,None,None,1)
        manual=await self.db.target_create('Игрок',None,None,None,1)
        await self.relation('Полдень','Долг','neutral')
        self.assertEqual([t['id'] for t in await self.active_targets()],[manual])

    async def test_ally_enemy_exclusively_diplomat(self):
        await self.relation('Полдень','Свобода','ally')
        await self.relation('Свобода','Долг','war')
        self.assertEqual(await self.active_targets(),[])
        candidate=(await self.db.ally_candidates())[0]
        for uid in (1,2,3,4,6,7):
            with self.assertRaises(ValueError):
                await self.db.ally_decide(candidate['id'],candidate['basis'],True,uid)
        await self.db.ally_decide(candidate['id'],candidate['basis'],True,5)
        self.assertEqual([r['target_name'] for r in await self.active_targets()],['Долг'])
        await self.db.ally_decide(candidate['id'],candidate['basis'],False,5)
        self.assertEqual(await self.active_targets(),[])

    async def test_expiring_ally_decisions_and_stale_buttons(self):
        await self.relation('Полдень','Свобода','ally')
        await self.relation('Свобода','Долг','war')
        c=(await self.db.ally_candidates())[0]
        await self.db.ally_decide(c['id'],c['basis'],True,5)
        await self.relation('Свобода','Долг','neutral')
        self.assertEqual(await self.active_targets(),[])
        await self.relation('Свобода','Долг','war')
        self.assertEqual(await self.active_targets(),[])
        with self.assertRaises(ValueError):
            await self.db.ally_decide(c['id'],c['basis'],True,5)
        c=(await self.db.ally_candidates())[0]
        await self.db.ally_decide(c['id'],c['basis'],True,5)
        await self.relation('Полдень','Свобода','unknown')
        self.assertEqual(await self.active_targets(),[])

    async def test_no_transitive_war_and_own_allies_excluded(self):
        await self.relation('Полдень','Свобода','ally')
        await self.relation('Полдень','Долг','ally')
        await self.relation('Свобода','Долг','war')
        self.assertEqual(await self.db.ally_candidates(),[])
        await self.relation('Свобода','Бандиты','war')
        await self.relation('Бандиты','Монолит','war')
        self.assertEqual([r['name'] for r in await self.db.ally_candidates()],['Бандиты'])

    async def test_publisher_retry_and_no_duplicate_on_restart(self):
        await self.db.set_topic('storage',-1001,10)
        rid=await self.request()
        bot=SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('offline')),edit_message_text=AsyncMock())
        await publish_community(bot,self.db)
        bot.send_message.side_effect=None
        bot.send_message.return_value=SimpleNamespace(message_id=123)
        await publish_community(bot,self.db)
        self.assertEqual(bot.send_message.call_count,2)
        await publish_community(bot,Database(self.db.path))
        self.assertEqual(bot.send_message.call_count,2)
        await self.db.warehouse_transition(rid,'approve',1)
        await publish_community(bot,self.db)
        self.assertEqual(bot.send_message.call_count,3)
        bot.edit_message_text.assert_awaited()
        self.assertIn('tg://user?id=3',bot.send_message.call_args.kwargs['text'])

    async def test_matrix_and_role_alias(self):
        image=render_matrix(await self.db.community_rows('SELECT * FROM factions'),[])
        self.assertTrue(image.startswith(b'\x89PNG'))
        self.assertEqual(parse_position('Старшина')[0],'storekeeper')


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_real_v7_schema_without_losing_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.db'
            with sqlite3.connect(path) as conn:
                conn.executescript((Path(__file__).parent/'fixtures'/'v7_schema.sql').read_text(encoding='utf-8'))
            conn.close()
            db=Database(path)
            await db.init(); await db.init()
            backup=path.with_suffix('.db.pre-v8.bak')
            self.assertTrue(backup.exists())
            with sqlite3.connect(backup) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM gp_stock').fetchone()[0],2)
            conn.close()
            stock=await db.gp_stock_list()
            self.assertEqual(len(stock),1)
            self.assertEqual((stock[0]['quantity'],stock[0]['reserved']),(12,2))
            self.assertEqual(len(await db.list_storage_items()),1)
            self.assertEqual(len(await db.community_rows("SELECT * FROM targets WHERE status='active'")),1)


if __name__=='__main__':
    unittest.main()

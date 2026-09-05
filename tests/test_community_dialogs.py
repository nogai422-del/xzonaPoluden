"""Exercise real aiogram routing with a fake Telegram transport (no network)."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import Update, Message, Chat, User, CallbackQuery, PhotoSize

from app.community_handlers import router, Flow
from app.db import Database


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls=[]
        self.messages=[]

    async def close(self):
        pass

    async def stream_content(self,*args,**kwargs):
        yield b''

    async def make_request(self,bot,method,timeout=None):
        self.calls.append(method)
        if method.__api_method__=='answerCallbackQuery':
            return True
        if method.__api_method__.startswith(('send','editMessage')):
            message=Message(message_id=len(self.calls)+100,date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id,type='supergroup'),
                from_user=User(id=123456,is_bot=True,first_name='TestBot'),
                message_thread_id=getattr(method,'message_thread_id',None),is_topic_message=True,
                text=getattr(method,'text',None),caption=getattr(method,'caption',None),
                reply_markup=getattr(method,'reply_markup',None),
                photo=[PhotoSize(file_id='photo',file_unique_id='photo',width=100,height=100)] if method.__api_method__=='sendPhoto' else None).as_(bot)
            self.messages.append(message)
            return message
        return True


class DialogTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.dp=Dispatcher(fsm_strategy=FSMStrategy.USER_IN_TOPIC,events_isolation=SimpleEventIsolation())
        cls.dp.include_router(router)

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.tmp.name)/'db.sqlite')
        await self.db.init()
        self.session=FakeSession()
        self.bot=Bot('123456:FAKE_TEST_TOKEN_NOT_FOR_NETWORK',session=self.session)
        self.config=SimpleNamespace(admin_ids=set(),temp_message_ttl=90)
        self.dp['db']=self.db; self.dp['config']=self.config
        self.dp.fsm.storage=MemoryStorage()
        self.dp.fsm.events_isolation=SimpleEventIsolation()
        self.n=0
        for uid,role in [(1,'leader'),(3,'storekeeper'),(4,'private'),(5,'diplomat'),(7,'external_leader')]:
            await self.db.upsert_player(uid,None,f'User {uid}',f'Ник{uid}')
            await self.db.set_player_role(uid,role,None,1)
        for code,topic in [('storage',10),('market',20),('diplomacy',30)]:
            await self.db.set_topic(code,-1001,topic)
        self.cat=await self.db.catalogue_save('Аптечка')
        await self.db.gp_stock_upsert('Аптечка',10,3)

    async def asyncTearDown(self):
        await self.dp.fsm.storage.close()
        await self.dp.fsm.events_isolation.close()
        await self.bot.session.close()
        self.tmp.cleanup()

    async def event(self,data=None,text=None,uid=4,topic=20,photo=False,album=False):
        self.n+=1
        user=User(id=uid,first_name=f'User {uid}',is_bot=False)
        msg=Message(message_id=self.n,date=datetime.now(timezone.utc),chat=Chat(id=-1001,type='supergroup'),
                    from_user=user,message_thread_id=topic,is_topic_message=True,text=text,
                    photo=[PhotoSize(file_id='photo-file',file_unique_id='unique',width=600,height=400)] if photo else None,
                    media_group_id='album' if album else None).as_(self.bot)
        update=Update(update_id=self.n,callback_query=CallbackQuery(id=str(self.n),from_user=user,chat_instance='test',message=msg,data=data) if data else None,message=msg if not data else None)
        await self.dp.feed_update(self.bot,update)

    def latest_button(self,prefix):
        for msg in reversed(self.session.messages):
            if msg.reply_markup:
                for row in msg.reply_markup.inline_keyboard:
                    for button in row:
                        if button.callback_data.startswith(prefix):
                            return button.callback_data
        self.fail('Button not found: '+prefix)

    async def test_sale_entire_dialog_photo_validation_and_repeat_submit(self):
        await self.event(data=f'c8:pick:sale:{self.cat["id"]}')
        await self.event(text='No photo')
        self.assertIn('фотографию',self.session.messages[-1].text)
        await self.event(photo=True,album=True)
        self.assertEqual(await self.db.ad_list(),[])
        await self.event(photo=True)
        self.assertIn('Цена',self.session.messages[-1].text)
        await self.event(text='бесплатно')
        self.assertIn('целым',self.session.messages[-1].text)
        await self.event(text='15 000')
        await self.event(text='<b>Новая</b> аптечка')
        preview=self.session.messages[-1]
        self.assertTrue(preview.photo)
        self.assertIn('&lt;b&gt;',preview.caption)
        self.assertEqual(await self.db.ad_list(),[])
        submit=self.latest_button('c8:submit:')
        await self.event(data=submit)
        ads=await self.db.ad_list()
        self.assertEqual(len(ads),1)
        self.assertEqual((ads[0]['photo_id'],ads[0]['price']),('photo-file',15000))
        await self.event(data=submit)
        self.assertEqual(len(await self.db.ad_list()),1)

    async def test_request_dialog_permissions_and_topic_isolation(self):
        await self.event(data=f'c8:pick:request:{self.cat["id"]}',topic=10)
        await self.event(text='3',topic=20)
        self.assertEqual(await self.db.warehouse_list(),[])
        await self.event(text='3',topic=10)
        await self.event(text='Для рейда',topic=10)
        await self.event(data=self.latest_button('c8:submit:'),topic=10)
        rid=(await self.db.warehouse_list())[0]['id']
        await self.event(data=f'c8:req:approve:{rid}',topic=10,uid=3)
        self.assertEqual((await self.db.warehouse_list())[0]['status'],'pending')
        await self.event(data=f'c8:req:approve:{rid}',topic=10,uid=1)
        self.assertEqual((await self.db.warehouse_list())[0]['status'],'approved')
        await self.event(data=f'c8:req:issue:{rid}',topic=10,uid=3)
        self.assertEqual((await self.db.catalogue_get(self.cat['id']))['quantity'],7)

    async def test_cancel_and_external_cannot_create(self):
        await self.event(data=f'c8:pick:sale:{self.cat["id"]}',uid=7)
        self.assertEqual(self.session.calls[-1].__api_method__,'answerCallbackQuery')
        self.assertTrue(self.session.calls[-1].show_alert)
        await self.event(data=f'c8:pick:sale:{self.cat["id"]}')
        # /cancel includes a bot_command entity in real Telegram updates.
        await self.event(data='c8:cancel')
        await self.event(photo=True)
        self.assertEqual(await self.db.ad_list(),[])

    async def test_search_new_position_and_stock_edit(self):
        await self.event(data='c8:search:sale')
        await self.event(text='АПТ')
        self.assertEqual(self.latest_button('c8:pick:sale:'),f'c8:pick:sale:{self.cat["id"]}')
        await self.event(data='c8:newname:stock',topic=10,uid=3)
        await self.event(text='Патроны',topic=10,uid=3)
        await self.event(text='50',topic=10,uid=3)
        cats=await self.db.catalogue_list(search='патроны')
        self.assertEqual(cats[0]['quantity'],50)

    async def test_diplomacy_pair_dialog_and_stale_relation_button(self):
        factions=await self.db.community_rows('SELECT * FROM factions ORDER BY id')
        a,b=factions[0]['id'],factions[1]['id']
        await self.event(data=f'c8:pairb:{a}:{b}',topic=30,uid=5)
        war=self.latest_button('c8:rel:war:')
        await self.event(data=war,topic=30,uid=5)
        self.assertEqual(len(await self.db.community_rows("SELECT * FROM targets WHERE status='active'")),1)
        await self.event(data=war,topic=30,uid=5)
        self.assertTrue(self.session.calls[-1].show_alert)
        await self.event(data='c8:matrix',topic=30)
        self.assertTrue(self.session.messages[-1].photo)

    async def test_common_topic_binding_and_catalogue_pagination(self):
        await self.db.set_topic('gp_stock',-1001,40)
        await self.event(data=f'c8:pick:request:{self.cat["id"]}',topic=10)
        self.assertTrue(self.session.calls[-1].show_alert)
        for i in range(10):
            await self.db.catalogue_save(f'Предмет {i}')
        await self.event(data='v7stock:list',topic=40)
        next_page=self.latest_button('c8:catalog:request:1')
        await self.event(data=next_page,topic=40)
        self.assertEqual(self.latest_button('c8:catalog:request:0'),'c8:catalog:request:0')


if __name__=='__main__':
    unittest.main()

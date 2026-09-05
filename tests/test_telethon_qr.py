import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import SessionPasswordNeededError
from app.telethon_manager import TelethonManager
from app.telethon_web import TelethonWebAuth, LoginTicket


class QRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=SimpleNamespace(set_telethon_auth=AsyncMock(),get_telethon_auth=AsyncMock(return_value=None))
        self.manager=TelethonManager(SimpleNamespace(bot_token='test'),self.db)
        self.event=asyncio.Event()
        self.started=False
        self.failure=None
        async def wait():
            self.started=True
            await self.event.wait()
            if self.failure:
                raise self.failure
            return SimpleNamespace(phone='123456789')
        self.qr=SimpleNamespace(url='tg://login?token=TEST_ONLY',expires=datetime.now(timezone.utc)+timedelta(seconds=30),wait=wait)
        self.client=SimpleNamespace(connect=AsyncMock(),disconnect=AsyncMock(),qr_login=AsyncMock(return_value=self.qr),
            is_user_authorized=AsyncMock(return_value=True),session=SimpleNamespace(save=Mock(return_value='saved-session')),
            sign_in=AsyncMock(return_value=SimpleNamespace(phone='123456789')),is_connected=Mock(return_value=True))
        self.factory=patch('app.telethon_manager.TelegramClient',return_value=self.client)
        self.factory.start()
        self.web=TelethonWebAuth(self.manager,host='127.0.0.1',port=0,public_url='http://localhost')

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.factory.stop()

    async def test_qr_wait_starts_before_display_and_saves_session(self):
        qid=await self.manager.begin_qr_login(123,'hash')
        self.assertTrue(self.started)
        self.assertEqual(self.manager.qr_status(qid)['state'],'waiting')
        self.event.set(); await self.manager._qr_task
        self.assertEqual(self.manager.qr_status(qid)['state'],'connected')
        self.assertTrue(self.manager.connected)
        self.assertEqual(self.db.set_telethon_auth.call_args.kwargs['phone'],'123456789')

    async def test_qr_2fa_and_wrong_ticket(self):
        self.failure=SessionPasswordNeededError(request=None)
        qid=await self.manager.begin_qr_login(123,'hash')
        self.event.set(); await self.manager._qr_task
        self.assertEqual(self.manager.qr_status(qid)['state'],'password')
        with self.assertRaises(RuntimeError):
            await self.manager.submit_password('password',qr_id='wrong')
        self.db.set_telethon_auth.assert_not_awaited()
        await self.manager.submit_password('password',qr_id=qid)
        self.assertTrue(self.manager.connected)

    async def test_expiry_and_refresh_invalidate_previous_qr(self):
        self.failure=asyncio.TimeoutError()
        old=await self.manager.begin_qr_login(123,'hash')
        self.event.set(); await self.manager._qr_task
        self.assertEqual(self.manager.qr_status(old)['state'],'expired')
        self.event.clear(); self.failure=None
        new=await self.manager.refresh_qr_login(old)
        self.assertNotEqual(old,new)
        self.assertEqual(self.manager.qr_status(old)['state'],'expired')
        self.assertEqual(self.manager.qr_status(new)['state'],'waiting')

    async def test_web_qr_without_phone_and_private_status(self):
        self.web.create_login_url(1)
        token=next(iter(self.web._tickets))
        request=SimpleNamespace(query={'t':token},post=AsyncMock(return_value={'api_id':'123','api_hash':'a'*32,'method':'qr'}))
        response=await self.web._start_login(request)
        self.assertEqual(response.status,200)
        self.assertIn('data:image/png;base64,',response.text)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        status=await self.web._qr_status(request)
        self.assertNotIn('TEST_ONLY',status.text)
        denied=await self.web._qr_status(SimpleNamespace(query={'t':'wrong'}))
        self.assertEqual(denied.status,403)

    async def test_cancel_stops_waiter(self):
        qid=await self.manager.begin_qr_login(123,'hash')
        task=self.manager._qr_task
        await self.manager.cancel_pending()
        self.assertTrue(task.done())
        self.assertIsNone(self.manager.pending)
        self.db.set_telethon_auth.assert_not_awaited()


if __name__=='__main__':
    unittest.main()

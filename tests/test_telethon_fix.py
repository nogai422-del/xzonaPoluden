import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.telethon_manager import TelethonManager
from app.telethon_web import TelethonWebAuth


class TelethonFixTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        return TelethonManager(SimpleNamespace(bot_token='test-token'),
                               SimpleNamespace(get_telethon_auth=AsyncMock(return_value=None)))

    async def test_health_does_not_call_telegram(self):
        manager=self.manager()
        manager.is_connected=AsyncMock(side_effect=AssertionError('Network in health check'))
        server=TelethonWebAuth(manager,host='127.0.0.1',port=0,public_url='http://localhost')
        response=await server._health(None)
        self.assertEqual(response.status,200)
        self.assertFalse(json.loads(response.text)['telethon_connected'])
        manager.is_connected.assert_not_awaited()

    async def test_corrupt_saved_session_does_not_crash_startup(self):
        manager=self.manager()
        manager.db.get_telethon_auth.return_value={
            'api_id':'123','api_hash_enc':manager.secrets.encrypt('hash'),
            'session_enc':manager.secrets.encrypt('corrupt-session')}
        await manager.initialize()
        self.assertFalse(manager.connected)
        self.assertIsNotNone(manager.last_error)

    async def test_status_hang_is_bounded(self):
        manager=self.manager()
        async def hang():
            await asyncio.Event().wait()
        manager.client=SimpleNamespace(is_connected=Mock(return_value=True),is_user_authorized=hang)
        with patch('app.telethon_manager.STATUS_TIMEOUT',0.01):
            self.assertFalse(await manager.is_connected())
        self.assertIn('не отвечает',manager.last_error)

    async def test_restore_hang_disconnects_and_can_retry(self):
        manager=self.manager()
        manager.db.get_telethon_auth.return_value={
            'api_id':'123','api_hash_enc':manager.secrets.encrypt('hash'),
            'session_enc':manager.secrets.encrypt('fake')}
        async def hang():
            await asyncio.Event().wait()
        client=SimpleNamespace(connect=hang,disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True))
        with patch('app.telethon_manager.StringSession'),patch('app.telethon_manager.TelegramClient',return_value=client),patch('app.telethon_manager.RESTORE_TIMEOUT',0.01):
            await manager.initialize()
            client.disconnect.assert_awaited_once()
            self.assertIsNone(manager.client)
            manager._next_restore_at=0
            client.connect=AsyncMock()
            client.is_connected=Mock(return_value=True)
            self.assertTrue(await manager.is_connected())


if __name__=='__main__':
    unittest.main()

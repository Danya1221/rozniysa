import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot_publisher import BotAPIRejected, BotAPIPublisher
from retail_control import RetailController
from retail_publisher import RetailPublisher
from retail_runtime import RetailSyncService
import test_retail as fixtures
from test_botapi_transport import FakeResponse, FakeSession


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.RetailAsyncTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.RetailAsyncTests.asyncSetUp(self)
        await self.publisher.ensure_target()

    def pending(self, **extra):
        pending = {'binding': self.publisher.binding(), 'key': 'page', 'text': '<b>Price</b>', **extra}
        self.state.set('pending_publish', pending)
        return pending

    def message(self, ident=99, **extra):
        return SimpleNamespace(id=ident, raw_text='Price', sender_id=999, **extra)

    async def test_missing_entity_cache_is_refreshed_before_reading_history(self):
        self.pending()
        self.publisher.client = SimpleNamespace(
            get_messages=AsyncMock(side_effect=[ValueError('missing input entity'), [self.message()]]),
            get_dialogs=AsyncMock(return_value=[]))
        await self.publisher.recover_pending()
        self.publisher.client.get_dialogs.assert_awaited_once_with(limit=None)
        self.assertIsNone(self.state.get('pending_publish'))
        self.assertEqual(self.state.get('published')['messages']['page']['id'], 99)
        self.assertFalse(self.publisher.messages)

    async def test_real_missing_access_preserves_pending_and_gives_both_recovery_paths(self):
        pending = self.pending()
        self.publisher.client = SimpleNamespace(
            get_messages=AsyncMock(side_effect=ValueError('missing input entity')),
            get_dialogs=AsyncMock(return_value=[]))
        with self.assertRaisesRegex(RuntimeError, 'Telegram-аккаунт 1'):
            await self.publisher.recover_pending()
        self.assertEqual(self.state.get('pending_publish'), pending)
        self.assertFalse(self.publisher.messages)

    async def test_network_failure_is_not_reported_as_missing_group_membership(self):
        self.pending()
        self.publisher.client = SimpleNamespace(get_messages=AsyncMock(side_effect=ConnectionError()))
        with self.assertRaisesRegex(RuntimeError, 'временно'):
            await self.publisher.recover_pending()
        self.assertIsNotNone(self.state.get('pending_publish'))

    async def test_only_recent_untracked_matching_message_is_recovered(self):
        now = datetime.now(timezone.utc)
        self.pending(started_at=now.timestamp(), after_id=20)
        self.publisher.client = SimpleNamespace(get_messages=AsyncMock(return_value=[
            self.message(30, date=now - timedelta(days=1)), self.message(10, date=now), self.message(40, date=now)]))
        await self.publisher.recover_pending()
        self.assertEqual(self.state.get('published')['messages']['page']['id'], 40)

    async def test_ambiguous_matching_messages_do_not_choose_an_old_copy(self):
        self.pending()
        self.publisher.client = SimpleNamespace(get_messages=AsyncMock(return_value=[self.message(30), self.message(40)]))
        with self.assertRaisesRegex(RuntimeError, 'однозначно'):
            await self.publisher.recover_pending()
        self.assertIsNotNone(self.state.get('pending_publish'))

    async def test_channel_post_can_be_recovered_by_exact_text_and_recent_boundary(self):
        self.pending(after_id=20)
        self.publisher.target_kind = 'channel'
        self.publisher.client = SimpleNamespace(get_messages=AsyncMock(return_value=[
            SimpleNamespace(id=40, raw_text='Price', sender_id=self.publisher.target, post=True)]))
        await self.publisher.recover_pending()
        self.assertEqual(self.state.get('published')['messages']['page']['id'], 40)

    async def test_explicit_text_rejection_does_not_poison_next_attempt(self):
        self.publisher._send = AsyncMock(side_effect=BotAPIRejected('Bad Request'))
        with self.assertRaises(BotAPIRejected):
            await BotAPIPublisher.publish(self.publisher, {'page': '<b>Price</b>'})
        self.assertIsNone(self.state.get('pending_publish'))

    async def test_explicit_photo_rejection_does_not_poison_next_attempt(self):
        self.publisher._photo = AsyncMock(side_effect=BotAPIRejected('Too Many Requests'))
        with self.assertRaises(BotAPIRejected):
            await self.publisher.node('brand:apple', 'Apple', [], 'Apple')
        self.assertIsNone(self.state.get('pending_retail_node'))
        self.publisher._photo = AsyncMock(return_value={'message_id': 88})
        await self.publisher.node('brand:apple', 'Apple', [], 'Apple')
        self.assertEqual(self.publisher.nodes()['brand:apple']['id'], 88)

    async def test_exhausted_429_is_definite_rejection_but_503_is_ambiguous(self):
        publisher = RetailPublisher('TEST', -100123456, self.state, self.settings, self.store)
        for status in (400, 429, 503):
            session = FakeSession([FakeResponse({'ok': False, 'error_code': status,
                'description': 'test error', **({'parameters': {'retry_after': 1}} if status == 429 else {})}, status)] * 5)
            publisher.http = session
            with patch('bot_publisher.asyncio.sleep', new=AsyncMock()):
                with self.assertRaises(RuntimeError) as caught:
                    await publisher.api('sendMessage', chat_id=-100123456, text='Price')
            self.assertEqual(isinstance(caught.exception, BotAPIRejected), status != 503)
            self.assertEqual(len(session.calls), 5 if status == 429 else 1)

    async def test_generated_photo_429_clears_pending_but_503_preserves_it(self):
        class MultipartSession(FakeSession):
            def post(self, url, data):
                return super().post(url, data)
        for status in (429, 503):
            publisher = RetailPublisher('TEST', -100123456, self.state, self.settings, self.store)
            publisher.target = -100123456
            publisher.http = MultipartSession([FakeResponse({'ok': False, 'error_code': status,
                'description': 'test error', **({'parameters': {'retry_after': 1}} if status == 429 else {})}, status)] * 5)
            with patch('retail_publisher.cover_bytes', return_value=b'jpeg'), \
                 patch('retail_publisher.asyncio.sleep', new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await publisher.node('brand:apple', 'Apple', [], 'Apple')
            self.assertEqual(self.state.get('pending_retail_node') is None, status == 429)
            self.assertEqual(len(publisher.http.calls), 5 if status == 429 else 1)

    def controller(self):
        service = RetailSyncService(None, self.settings, self.state, self.store, 'checkout_test_bot')
        service.publisher = self.publisher
        controller = RetailController('TEST', service, [42])
        controller.send = AsyncMock()
        controller.answer_callback = AsyncMock()
        controller.refresh_catalog = AsyncMock()
        return controller

    async def confirm(self, controller, token, actor=42):
        await controller.handle_callback({'id': 'test', 'data': 'retail:retry:' + token,
            'from': {'id': actor}, 'message': {'chat': {'id': actor, 'type': 'private'}}})

    async def test_manual_retry_only_clears_the_exact_previewed_pending(self):
        self.pending()
        controller = self.controller()
        self.assertIn('retail:recover', str(controller.menu()))
        await controller.show_recovery(42)
        token = controller.retry_waiting[42][0]
        await self.confirm(controller, token)
        self.assertIsNone(self.state.get('pending_publish'))
        controller.refresh_catalog.assert_awaited_once_with(42)

    async def test_stale_retry_confirmation_cannot_clear_a_different_send(self):
        self.pending()
        controller = self.controller()
        await controller.show_recovery(42)
        token = controller.retry_waiting[42][0]
        new = self.pending(text='<b>New price</b>')
        await self.confirm(controller, token)
        self.assertEqual(self.state.get('pending_publish'), new)
        controller.refresh_catalog.assert_not_awaited()

    async def test_non_admin_cannot_clear_pending(self):
        pending = self.pending()
        controller = self.controller()
        await controller.show_recovery(42)
        await self.confirm(controller, controller.retry_waiting[42][0], actor=99)
        self.assertEqual(self.state.get('pending_publish'), pending)

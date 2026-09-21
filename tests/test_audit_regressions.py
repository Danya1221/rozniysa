import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from retail_control import RetailController
from retail_publisher import RetailPublisher
from retail_runtime import RetailSyncService
from retail_store import stable_id
from retail_catalog import render_prices
from prices import units
from prices import parse_documents
from runtime import timestamp
import test_retail as retail_fixtures
import test_catalog_bridge as bridge_fixtures


class RetailRegressionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = retail_fixtures.RetailAsyncTests.asyncSetUp
    asyncTearDown = retail_fixtures.RetailAsyncTests.asyncTearDown

    def service(self):
        service = RetailSyncService(None, self.settings, self.state, self.store, 'checkout_test_bot')
        service.publisher = self.publisher
        self.state.set('sources', {'@first': {
            'status': 'open', 'checked': timestamp(),
            'items': [i.to_dict() for i in parse_documents(['17 256 Black (eSim) — 80000']).items],
        }})
        return service

    def controller(self, service):
        controller = RetailController('TEST', service, [42])
        controller.send = AsyncMock()
        controller.answer_callback = AsyncMock()
        return controller

    async def click(self, controller, data='retail:test_order', actor=42):
        await controller.handle_callback({'id': 'test', 'from': {'id': actor},
            'message': {'chat': {'id': actor, 'type': 'private'}}, 'data': data})
        if controller.task:
            await controller.task

    async def test_normal_refresh_contains_only_real_products(self):
        service = self.service()
        count, _ = await service.render()
        self.assertEqual(count, 1)
        self.assertIsNone(self.store.get('catalog', stable_id('temporary-retail-test|iphone-17|256gb|blue|hybrid')))

    async def test_old_test_button_cannot_recreate_test_posts(self):
        service = self.service()
        controller = self.controller(service)
        await self.click(controller)
        first_ids = set(self.publisher.messages)
        await self.click(controller)
        self.assertEqual(set(self.publisher.messages), first_ids)
        self.assertFalse(self.publisher.messages)
        await self.click(controller, 'retail:test_remove')
        self.assertFalse(self.store.scan('catalog'))
        self.assertNotIn('retail:test_order', str(controller.menu()))

    async def test_test_button_cannot_overwrite_catalog_during_sync(self):
        service = self.service()
        controller = self.controller(service)
        async with service.lock:
            await self.click(controller)
        self.assertFalse(self.store.scan('catalog'))
        self.assertFalse(self.publisher.messages)

    async def test_test_button_without_source_snapshot_cannot_empty_checkout(self):
        service = self.service()
        self.state.set('sources', {})
        controller = self.controller(service)
        await self.click(controller)
        self.assertFalse(self.store.scan('catalog'))

    async def test_customer_cannot_publish_test(self):
        await self.click(self.controller(self.service()), actor=99)
        self.assertFalse(self.publisher.messages)
        self.assertFalse(self.store.scan('catalog'))

    async def test_open_empty_source_does_not_resurrect_closed_stock(self):
        service = self.service()
        cache = self.state.get('sources')
        cache['@first']['status'] = 'closed'
        cache['@second'] = {'status': 'open', 'checked': timestamp(), 'items': [], 'error': None}
        self.state.set('sources', cache)
        self.assertEqual(service.retail_cached_items(), [])

    async def test_only_exact_legacy_test_posts_of_our_bot_are_cleaned(self):
        text = 'ТЕСТОВАЯ ПОЗИЦИЯ\n\niPhone 17 256GB Blue Sim+eSim — 150 000'
        url = 'https://t.me/checkout_test_bot?start=p_15dcc99ee2ffb0dca9788f6a'
        self.publisher.messages.update({90: {'text': text}, 91: {'text': text}, 92: {'text': text}})
        messages = [
            SimpleNamespace(id=90, sender_id=999, raw_text=text, entities=[SimpleNamespace(url=url)]),
            SimpleNamespace(id=91, sender_id=777, raw_text=text, entities=[SimpleNamespace(url=url)]),
            SimpleNamespace(id=92, sender_id=999, raw_text=text, entities=[SimpleNamespace(url='https://t.me/other')]),
        ]
        self.publisher.client = SimpleNamespace(get_messages=AsyncMock(return_value=messages))
        await self.publisher.publish(self.pages)
        self.assertNotIn(90, self.publisher.messages)
        self.assertIn(91, self.publisher.messages)
        self.assertIn(92, self.publisher.messages)
        await self.publisher.publish(self.pages)
        self.publisher.client.get_messages.assert_awaited_once()

    async def test_test_history_failure_does_not_stop_actual_prices(self):
        self.publisher.client = SimpleNamespace(get_messages=AsyncMock(side_effect=PermissionError()))
        await self.publisher.publish(self.pages)
        self.assertEqual(len(self.state.get('published')['messages']), len(self.pages))
        self.assertIsNone(self.state.get('retired_test_cleaned'))

    async def test_binding_cannot_change_in_the_middle_of_publication(self):
        await self.publisher.ensure_target()
        self.publisher._usable_chat = AsyncMock(return_value=(
            {'id': -100987654, 'type': 'supergroup', 'title': 'Other'}, None))
        async with self.publisher.layout_lock:
            with self.assertRaisesRegex(RuntimeError, 'публикац|обновлен'):
                await self.publisher.bind_group(-100987654)
        self.assertEqual(self.publisher.target, -100123456)

    async def test_generated_photo_5xx_is_not_retried_and_keeps_checkpoint(self):
        class Response:
            status = 503
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def json(self, **kwargs):
                return {'ok': False, 'description': 'Service Unavailable'}
        class HTTP:
            closed = False
            calls = 0
            def post(self, *args, **kwargs):
                self.calls += 1
                return Response()
        publisher = RetailPublisher('TEST', -100123456, self.state, self.settings, self.store)
        publisher.target = -100123456
        publisher.http = HTTP()
        with patch('retail_publisher.cover_bytes', return_value=b'jpeg'), \
             patch('retail_publisher.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await publisher.node('brand:apple', 'Apple', [], 'Apple')
        self.assertEqual(publisher.http.calls, 1)
        self.assertIsNotNone(self.state.get('pending_retail_node'))


class BridgeRegressionTests(unittest.TestCase):
    setUp, tearDown = bridge_fixtures.BridgeTests.setUp, bridge_fixtures.BridgeTests.tearDown

    def test_malformed_saved_url_does_not_block_price_snapshot(self):
        self.store.set('bridge', 'catalog_url', 'https://[')
        self.bridge.put_catalog([], confirmed=False)
        self.assertNotIn('catalog_url', self.opener.bodies[-1])
        self.assertEqual(self.store.get('bridge', 'catalog_url'), '')


class LongPriceTests(unittest.TestCase):
    def test_one_long_product_does_not_block_other_prices_or_lose_its_link(self):
        title = 'iPhone 17 ' + '🟠 & <вариант> ' * 400
        products = [
            {'id': stable_id('long'), 'title': title, 'price': '80000', 'currency': 'RUB',
             'brand': 'Apple', 'section': 'iPhone 17'},
            {'id': stable_id('normal'), 'title': 'iPhone 17 White', 'price': '79000', 'currency': 'RUB',
             'brand': 'Apple', 'section': 'iPhone 17'},
        ]
        pages, _ = render_prices(products, 'checkout_test_bot')
        text = '\n'.join(pages.values())
        self.assertEqual(text.count('<a href='), 2)
        self.assertIn('?start=p_' + products[0]['id'], text)
        self.assertIn('… — 80 000', text)
        self.assertIn('iPhone 17 White — 79 000', text)
        self.assertTrue(all(units(page) <= 3900 for page in pages.values()))
        self.assertEqual(products[0]['title'], title)

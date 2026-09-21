import asyncio
import io
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Settings, Source
from prices import parse_documents, select_items, Item, ParseResult, activation_state, units
from retail_catalog import to_product, dedupe_products, render_prices, cover_bytes
from retail_publisher import RetailPublisher
from retail_runtime import RetailSyncService
from retail_store import RetailStore, stable_id
from runtime import timestamp
from state import StateStore

SAMPLE = '''iPhone 17
Не активированное
17 256 Black (1Sim+eSim) — 79800
17 256 White (eSim) — 79000
Актив
17 Pro 1TB Blue (1Sim+eSim) — 139100
DJI / Insta360
Note 14 8/128 Green NFC 🇵🇾 — 15200
Oura Ring 5 - Size 8 Black 🇪🇺 — 35300
S11 42 Jet Black(S/M) 🇺🇸 — 28300
MacBook Air 13 M4 16/256 Silver — 85000
iMac 24 M4 16/256 Silver — 130000
16 Pro 128 Desert CPO 🇺🇸 (Ориг. УпаковкаiPhone ) — 77300'''


class CatalogTests(unittest.TestCase):
    def test_supplier_variants_grouped_without_cross_brand_leaks(self):
        items = parse_documents([SAMPLE]).items
        self.assertEqual(len(items), 9)
        products = [to_product(i, Settings()) for i in items]
        by_title = {p['title']: p for p in products}
        for needle, brand in [('Note 14', 'Xiaomi'), ('Oura', 'Oura Ring'), ('S11', 'Apple')]:
            p = next(p for title,p in by_title.items() if needle in title)
            self.assertEqual(p['brand'], brand)
            self.assertNotIn('DJI', p['title'])
        self.assertEqual(sum(p['section'] == 'MacBook / iMac' for p in products), 2)
        self.assertTrue(all('Упак' not in p['title'] for p in products))

    def test_action_cameras_share_cover_but_keep_global_brands_separate(self):
        items = parse_documents(['''DJI Osmo Action 5 Pro — 41000
Insta360 X5 — 52000
GoPro Hero 13 Black — 47000''']).items
        products = [to_product(item, Settings()) for item in items]

        self.assertEqual([p['brand'] for p in products], ['Экшн-камеры'] * 3)
        self.assertEqual([p['section'] for p in products], ['DJI', 'Insta360', 'GoPro'])

        pages, navigation = render_prices(products, 'checkout_test_bot')
        self.assertIn('Экшн-камеры', navigation)
        self.assertEqual(
            [entry['section'] for entry in navigation['Экшн-камеры']],
            ['DJI', 'GoPro', 'Insta360'],
        )
        text = '\n'.join(pages.values())
        self.assertIn('<b>DJI</b>', text)
        self.assertIn('<b>Insta360</b>', text)
        self.assertIn('<b>GoPro</b>', text)
        self.assertNotIn('<b>DJI / Insta360</b>', text)

    def test_activation_header_and_pure_esim_are_distinct(self):
        items = parse_documents([SAMPLE]).items
        self.assertEqual(items[0].sim, 'hybrid')
        self.assertEqual(items[1].sim, 'esim')
        self.assertEqual(activation_state(items[2].title), 'active')
        filtered = select_items(items[:3], Settings(sim_filter='esim'))
        self.assertEqual(filtered, [items[1]])

    def test_ids_survive_markup_and_price_updates(self):
        item = parse_documents(['17 256 Black (1Sim+eSim) — 79800']).items[0]
        before = to_product(item, Settings())
        after = to_product(Item(item.title, Decimal('90000'), item.currency, item.block, item.sim), Settings(markup=Decimal('3000')))
        self.assertEqual(before['id'], after['id'])
        self.assertNotEqual(before['price'], after['price'])
        self.assertLess(len('p_' + before['id']), 64)

    def test_customer_visible_id_collision_keeps_lowest_price_once(self):
        items = parse_documents(['16 Pro 128 Desert CPO 🇺🇸 (Ориг. Упаковка iPhone) — 77300\n16 Pro 128 Desert CPO 🇺🇸 — 76800']).items
        products = [to_product(item, Settings()) for item in items]
        self.assertEqual(len(products), 2)
        self.assertEqual(products[0]['id'], products[1]['id'])
        unique = dedupe_products(products)
        self.assertEqual(len(unique), 1)
        self.assertEqual(unique[0]['price'], '76800')

    def test_every_position_has_link_and_large_section_paginates(self):
        items = parse_documents([f'17 256 Colour{i} (1Sim+eSim) — {79800+i}' for i in range(250)]).items
        products = [to_product(i, Settings()) for i in items]
        pages, nav = render_prices(products, 'checkout_test_bot')
        self.assertGreater(len(pages), 1)
        self.assertEqual(sum(p.count('<a href=') for p in pages.values()), 250)
        self.assertTrue(all(units(p) <= 3900 for p in pages.values()))
        self.assertTrue(all(not p.startswith('<b>—') for p in pages.values()))
        self.assertEqual(list(pages), nav['Apple'][0]['keys'])

    def test_html_escaped_and_two_sim_group_headers(self):
        products = [to_product(i, Settings()) for i in parse_documents([SAMPLE]).items]
        pages, _ = render_prices(products, 'checkout_test_bot')
        text = '\n'.join(pages.values())
        self.assertIn('SIM + eSIM', text)
        self.assertIn('Не активированное', text)
        self.assertNotIn('Статус неизвестен', text)

    def test_bad_checkout_username_stops_publication(self):
        with self.assertRaises(ValueError):
            render_prices([], 'https://bad.test')

    def test_explicit_out_of_stock_removes_previous_position(self):
        result = parse_documents(['17 256 Black (eSim) — 79800\n17 256 Black (eSim) — нет в наличии'])
        self.assertEqual(result.items, [])
        self.assertEqual(result.unavailable, 1)
        self.assertFalse(result.closed)

    def test_cover_is_readable_jpeg(self):
        from PIL import Image
        for brand in ('Apple', 'Samsung', 'Аксессуары'):
            image = Image.open(io.BytesIO(cover_bytes(brand)))
            self.assertEqual(image.size, (1200,640))
            self.assertEqual(image.format, 'JPEG')


class FakePublisher(RetailPublisher):
    def __init__(self, state, settings, store):
        super().__init__('TEST', -100123456, state, settings, store)
        self.calls, self.messages = [], {}
        self.counter = 10
        self.fail_send = False
        self.forbid_delete = False

    async def api(self, method, **p):
        self.calls.append((method,p))
        if method == 'getMe':
            return {'id': 999, 'username': 'retail_test_bot'}
        if method == 'getChat':
            return {'id': -100123456, 'type': 'supergroup', 'title': 'Shop'}
        if method == 'getChatMember':
            return {'status': 'administrator'}
        if method in {'sendMessage','sendPhoto'}:
            if self.fail_send:
                raise RuntimeError('Unknown send outcome')
            self.counter += 1
            self.messages[self.counter] = p.copy()
            return {'message_id': self.counter}
        if method.startswith('editMessage'):
            if p['message_id'] not in self.messages:
                raise RuntimeError('message to edit not found')
            self.messages[p['message_id']].update(p)
            return True
        if method == 'deleteMessage':
            if self.forbid_delete:
                raise RuntimeError("message can't be deleted")
            self.messages.pop(p['message_id'], None)
            return True
        if method == 'pinChatMessage':
            return True
        raise AssertionError(method)

    async def _photo(self, caption, brand, keyboard, photo_id=None):
        return await self.api('sendPhoto', caption=caption, brand=brand, reply_markup=keyboard, photo=photo_id)


class RetailAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name)/'state.json')
        self.store = RetailStore('sqlite:///' + self.tmp.name + '/store.db')
        self.settings = Settings(send_delay=0, sources=(Source('@first'), Source('@second')))
        self.publisher = FakePublisher(self.state, self.settings, self.store)
        self.products = [to_product(i, self.settings) for i in parse_documents([SAMPLE]).items]
        self.pages, self.publisher.navigation = render_prices(self.products, 'checkout_test_bot')

    async def asyncTearDown(self):
        await self.publisher.close()
        self.state.close()
        self.tmp.cleanup()

    async def test_generated_cover_waits_and_retries_on_telegram_429(self):
        class FakeResponse:
            def __init__(self, status, payload):
                self.status = status
                self.payload = payload
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def json(self, content_type=None):
                return self.payload

        class FakeHTTP:
            closed = False
            def __init__(self):
                self.responses = [
                    FakeResponse(429, {'ok': False, 'description': 'Too Many Requests: retry after 28',
                                       'parameters': {'retry_after': 28}}),
                    FakeResponse(200, {'ok': True, 'result': {'message_id': 777}}),
                ]
            def post(self, *args, **kwargs):
                return self.responses.pop(0)

        publisher = RetailPublisher('TEST', -100123456, self.state, self.settings, self.store)
        publisher.target = -100123456
        publisher.http = FakeHTTP()
        with patch('retail_publisher.cover_bytes', return_value=b'jpeg'), \
             patch('retail_publisher.asyncio.sleep', new=AsyncMock()) as sleeper:
            result = await publisher._photo('Apple', 'Apple', {'inline_keyboard': []})
        self.assertEqual(result['message_id'], 777)
        sleeper.assert_awaited_once_with(29)

    async def test_two_menus_covers_and_price_back_links(self):
        await self.publisher.publish(self.pages)
        nodes = self.publisher.nodes()
        self.assertIn('root:0', nodes)
        self.assertIn('root:1', nodes)
        self.assertEqual(sum(n['photo'] for n in nodes.values()), 3)
        self.assertIsNotNone(self.store.get('system', 'catalog_url'))
        self.assertTrue(any(m == 'editMessageReplyMarkup' for m,_ in self.publisher.calls))
        for m,p in self.publisher.calls:
            if m in {'editMessageCaption','editMessageText'}:
                self.assertTrue(all(len(row) <= 2 for row in p.get('reply_markup',{}).get('inline_keyboard',[])))

    async def test_legacy_real_group_layout_is_forced_to_rebuild_once(self):
        items = parse_documents([
            "iPhone 12 128 Black — 45000\n"
            "iPhone 17 256 Black — 80000\n"
            "Samsung Galaxy S26 12/256 Black — 90000"
        ]).items
        products = [to_product(item, self.settings) for item in items]
        pages, navigation = render_prices(products, 'checkout_test_bot')
        self.publisher.navigation = navigation

        # Simulate the real broken production chronology:
        # all covers first, price messages later, roots at the end.
        await self.publisher.ensure_target()
        apple_key = "brand:" + __import__('hashlib').sha256("Apple".encode()).hexdigest()[:16]
        samsung_key = "brand:" + __import__('hashlib').sha256("Samsung".encode()).hexdigest()[:16]
        self.publisher.messages = {
            11: {'caption': '<b>Apple</b>'},
            12: {'caption': '<b>Samsung</b>'},
            20: {'text': next(iter(pages.values()))},
            21: {'text': list(pages.values())[-1]},
            30: {'text': '<b>Каталог · другие бренды</b>'},
            31: {'text': 'Каталог техники'},
        }
        self.publisher.counter = 31
        page_keys = list(pages)
        self.state.set('published', {'binding': self.publisher.binding(), 'messages': {
            page_keys[0]: {'id': 20, 'hash': 'old', 'content': pages[page_keys[0]]},
            page_keys[-1]: {'id': 21, 'hash': 'old', 'content': pages[page_keys[-1]]},
        }})
        self.state.set('retail_nodes', {'binding': self.publisher.binding(), 'nodes': {
            apple_key: {'id': 11, 'text': '<b>Apple</b>', 'photo': True},
            samsung_key: {'id': 12, 'text': '<b>Samsung</b>', 'photo': True},
            'root:1': {'id': 30, 'text': '<b>Каталог · другие бренды</b>', 'photo': False},
            'root:0': {'id': 31, 'text': 'Каталог техники', 'photo': False},
        }})
        self.state.set('retail_layout_version', 2)

        await self.publisher.publish(pages)

        self.assertEqual(self.state.get('retail_layout_version'), 3)
        nodes = self.publisher.nodes()
        manifest = self.state.get('published')['messages']
        desired = self.publisher._desired_units(pages)
        ids = [self.publisher._unit_id(unit, manifest, nodes) for unit in desired]
        self.assertEqual(ids, sorted(ids))
        self.assertLess(
            next(record['id'] for key, record in nodes.items()
                 if key.startswith('brand:') and record.get('text', '').startswith('<b>Apple</b>')),
            min(manifest[key]['id'] for section in navigation['Apple'] for key in section['keys'])
        )
        self.assertLess(
            max(manifest[key]['id'] for section in navigation['Apple'] for key in section['keys']),
            next(record['id'] for key, record in nodes.items()
                 if key.startswith('brand:') and record.get('text', '').startswith('<b>Samsung</b>'))
        )

    async def test_brand_cover_is_immediately_before_its_price_group(self):
        items = parse_documents([
            "iPhone 11 128 Black — 40000\n"
            "iPhone 18 256 Blue — 150000\n"
            "Apple Watch Ultra 3 49mm Black — 70000\n"
            "Samsung Galaxy S26 12/256 Black — 90000\n"
            "Samsung Buds 4 Black — 20000"
        ]).items
        products = [to_product(item, self.settings) for item in items]
        pages, navigation = render_prices(products, 'checkout_test_bot')
        self.publisher.navigation = navigation

        await self.publisher.publish(pages)

        nodes = self.publisher.nodes()
        manifest = self.state.get('published')['messages']

        def brand_id(name):
            return next(
                record['id'] for key, record in nodes.items()
                if key.startswith('brand:') and record.get('text', '').startswith('<b>' + name + '</b>')
            )

        apple_keys = [
            key for section in navigation['Apple'] for key in section['keys']
        ]
        samsung_keys = [
            key for section in navigation['Samsung'] for key in section['keys']
        ]
        apple_ids = [manifest[key]['id'] for key in apple_keys]
        samsung_ids = [manifest[key]['id'] for key in samsung_keys]

        self.assertLess(brand_id('Apple'), min(apple_ids))
        self.assertLess(max(apple_ids), brand_id('Samsung'))
        self.assertLess(brand_id('Samsung'), min(samsung_ids))
        self.assertLess(max(samsung_ids), nodes['root:1']['id'])
        self.assertLess(nodes['root:1']['id'], nodes['root:0']['id'])

    async def test_apple_cover_buttons_jump_to_iphone_generations_below(self):
        items = parse_documents([
            "iPhone 11 128 Black — 40000\n"
            "iPhone 12 128 White — 45000\n"
            "iPhone 17 256 Black — 80000\n"
            "iPhone 18 256 Blue — 150000"
        ]).items
        products = [to_product(item, self.settings) for item in items]
        pages, navigation = render_prices(products, 'checkout_test_bot')
        self.publisher.navigation = navigation

        await self.publisher.publish(pages)

        nodes = self.publisher.nodes()
        apple = next(
            record for key, record in nodes.items()
            if key.startswith('brand:') and record.get('text', '').startswith('<b>Apple</b>')
        )
        labels = [
            button['text']
            for row in apple['rows']
            for button in row
            if button['text'] != '← Все бренды'
        ]
        self.assertEqual(labels[:4], ['iPhone 11', 'iPhone 12', 'iPhone 17', 'iPhone 18'])

        manifest = self.state.get('published')['messages']
        expected = {}
        for section in navigation['Apple']:
            if section['keys']:
                expected[section['section']] = manifest[section['keys'][0]]['id']
        for row in apple['rows']:
            for button in row:
                if button['text'] in expected:
                    self.assertTrue(button['url'].endswith('/' + str(expected[button['text']])))

    async def test_main_catalog_is_physically_last_message(self):
        await self.publisher.publish(self.pages)
        nodes = self.publisher.nodes()
        manifest = self.state.get('published')['messages']
        price_ids = [entry['id'] for entry in manifest.values()]
        brand_ids = [
            record['id'] for key, record in nodes.items()
            if key.startswith('brand:')
        ]
        self.assertGreater(nodes['root:1']['id'], max(price_ids + brand_ids))
        self.assertGreater(nodes['root:0']['id'], nodes['root:1']['id'])
        self.assertEqual(self.state.get('retail_pinned')['id'], nodes['root:0']['id'])
        self.assertTrue(self.store.get('system', 'catalog_url').endswith('/' + str(nodes['root:0']['id'])))

    async def test_new_price_post_moves_catalog_back_to_end(self):
        await self.publisher.publish(self.pages)
        old_root = self.publisher.nodes()['root:0']['id']

        expanded = dict(self.pages)
        expanded['extra:test'] = '<b>Тестовый раздел</b>\n\n<a href="https://t.me/checkout_test_bot?start=p_extra">Товар — 1 000</a>'
        await self.publisher.publish(expanded)

        nodes = self.publisher.nodes()
        manifest = self.state.get('published')['messages']
        self.assertNotEqual(nodes['root:0']['id'], old_root)
        self.assertGreater(nodes['root:0']['id'], max(entry['id'] for entry in manifest.values()))
        self.assertGreater(nodes['root:0']['id'], nodes['root:1']['id'])

    async def test_unchanged_prices_do_not_send_duplicate_messages(self):
        await self.publisher.publish(self.pages)
        count = self.publisher.counter
        self.publisher.calls.clear()
        await self.publisher.publish(self.pages)
        self.assertEqual(self.publisher.counter, count)
        self.assertFalse(self.publisher.calls)

    async def test_deleted_navigation_rebuilds_links_before_pinning(self):
        await self.publisher.publish(self.pages)
        nodes = self.publisher.nodes()
        old_root = nodes['root:0']['id']
        del self.publisher.messages[old_root]
        nodes['root:0']['checked'] = 0
        self.publisher.save_nodes(nodes)
        await self.publisher.publish(self.pages)
        new_root = self.publisher.nodes()['root:0']['id']
        self.assertNotEqual(new_root, old_root)
        self.assertTrue(self.store.get('system','catalog_url').endswith('/'+str(new_root)))
        self.assertEqual(self.state.get('retail_pinned')['id'],new_root)

    async def test_deleted_page_is_recovered_without_breaking_brand_layout(self):
        await self.publisher.publish(self.pages)
        before = {
            key: dict(value)
            for key, value in self.state.get('published')['messages'].items()
        }
        key = next(iter(before))
        del self.publisher.messages[before[key]['id']]
        self.state.set('retail_probe', 0)

        await self.publisher.publish(self.pages)

        manifest = self.state.get('published')['messages']
        self.assertIn(key, manifest)
        self.assertIn(manifest[key]['id'], self.publisher.messages)

        # The repair may rebuild a whole physical sequence because Telegram cannot
        # insert a recovered message into the middle. What matters is that the
        # storefront chronology is correct after recovery.
        nodes = self.publisher.nodes()
        desired = self.publisher._desired_units(self.pages)
        ids = [
            self.publisher._unit_id(unit, manifest, nodes)
            for unit in desired
        ]
        self.assertTrue(all(message_id is not None for message_id in ids))
        self.assertEqual(ids, sorted(ids))

    async def test_changed_price_keeps_id_and_changes_keyboard_checkpoint(self):
        await self.publisher.publish(self.pages)
        ids = {k:v['id'] for k,v in self.state.get('published')['messages'].items()}
        changed = {k: v.replace('79 800', '81 000') for k,v in self.pages.items()}
        await self.publisher.publish(changed)
        self.assertEqual(ids, {k:v['id'] for k,v in self.state.get('published')['messages'].items()})

    async def test_upload_cover_edits_media_without_new_post(self):
        await self.publisher.publish(self.pages)
        count = self.publisher.counter
        self.state.set('retail_covers', {'Apple':'photo_file_id'})
        await self.publisher.publish(self.pages)
        self.assertEqual(self.publisher.counter, count)
        self.assertTrue(any(m == 'editMessageMedia' for m,_ in self.publisher.calls))

    async def test_ambiguous_send_stops_blind_retries(self):
        self.publisher.fail_send = True
        with self.assertRaises(RuntimeError):
            await self.publisher.publish(self.pages)
        self.assertIsNotNone(self.state.get('pending_retail_node'))
        self.publisher.fail_send = False
        with self.assertRaisesRegex(RuntimeError, 'Восстановить публикацию'):
            await self.publisher.publish(self.pages)
        self.assertEqual(self.publisher.counter, 10)

    async def test_old_posts_redacted_when_deletion_forbidden(self):
        await self.publisher.publish(self.pages)
        self.publisher.forbid_delete = True
        self.publisher.navigation = {}
        await self.publisher.publish({})
        self.assertFalse(self.state.get('published')['messages'])
        self.assertTrue(any(p.get('text') == 'Раздел обновлён.' for p in self.publisher.messages.values()))

    async def test_source_failure_retains_previous_items_as_unconfirmed(self):
        service = RetailSyncService(None, self.settings, self.state, self.store, 'checkout_test_bot')
        service.publisher = self.publisher
        service.ready = True
        service.connect = AsyncMock()
        old = parse_documents(['17 256 Black (1Sim+eSim) — 79800']).items
        fresh = parse_documents(['Samsung Galaxy S26 256 Black — 90000']).items
        self.state.set('sources', {'@first': {'status':'open','checked':timestamp(),'items':[i.to_dict() for i in old]}})
        first = SimpleNamespace(source=self.settings.sources[0], fetch=AsyncMock(side_effect=ConnectionError()))
        second = SimpleNamespace(source=self.settings.sources[1], fetch=AsyncMock(return_value=ParseResult(fresh,[])))
        service.active_reader_entries = lambda: iter([(1,first),(1,second)])
        await service.sync()
        catalog = self.store.scan('catalog')
        self.assertEqual(len(catalog),2)
        self.assertNotIn(stable_id('temporary-retail-test|iphone-17|256gb|blue|hybrid'), dict(catalog))
        self.assertFalse(self.store.get('system','catalog')['confirmed'])

    async def test_closed_supplier_stale_lower_price_does_not_beat_open_supplier(self):
        service = RetailSyncService(None, self.settings, self.state, self.store, 'checkout_test_bot')
        cheap_closed = parse_documents(['17 256 Black (eSim) — 70000']).items
        fresh_open = parse_documents(['17 256 Black (eSim) — 80000']).items
        self.state.set('sources', {
            '@first': {'status':'closed','checked':timestamp(),'items':[i.to_dict() for i in cheap_closed], 'error':None},
            '@second': {'status':'open','checked':timestamp(),'items':[i.to_dict() for i in fresh_open], 'error':None},
        })
        selected = service.retail_cached_items()
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].price, Decimal('80000'))

    async def test_closed_supplier_keeps_catalog_and_no_fake_freshness(self):
        service = RetailSyncService(None, self.settings, self.state, self.store, 'checkout_test_bot')
        service.publisher = self.publisher
        old = parse_documents(['17 256 Black (eSim) — 79800']).items
        self.state.set('sources', {'@first': {'status':'closed','checked':'2020-01-01T00:00:00+00:00',
                                               'items':[i.to_dict() for i in old]}})
        await service.refresh_format()
        meta = self.store.get('system','catalog')
        self.assertFalse(meta['confirmed'])
        self.assertLess(meta['checked_at'],time.time()-3600)
        self.assertEqual(meta['count'],1)
        self.assertIsNone(self.store.get('catalog', stable_id('temporary-retail-test|iphone-17|256gb|blue|hybrid')))

    async def test_navigation_order_is_stable_even_if_pages_dict_is_reversed(self):
        await self.publisher.publish(self.pages)
        before = {
            key: value['id']
            for key, value in self.state.get('published')['messages'].items()
        }

        reordered = dict(reversed(list(self.pages.items())))
        await self.publisher.publish(reordered)

        manifest = self.state.get('published')['messages']
        self.assertEqual(
            {key: value['id'] for key, value in manifest.items()},
            before,
        )
        nodes = self.publisher.nodes()
        desired = self.publisher._desired_units(reordered)
        ids = [self.publisher._unit_id(unit, manifest, nodes) for unit in desired]
        self.assertEqual(ids, sorted(ids))

    async def test_pending_checkpoint_is_cleared_with_saved_message(self):
        await self.publisher.ensure_target()
        await self.publisher.node('root:0','Welcome',[])
        self.assertIsNone(self.state.get('pending_retail_node'))
        self.assertTrue(self.publisher.nodes()['root:0']['id'])

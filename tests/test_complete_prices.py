import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl import types

from config import Settings, Source
from prices import parse_documents, render_blocks, select_items
from supplier import SupplierReader, table_text


class CompletePriceTests(unittest.TestCase):
    def test_retail_price_on_next_line_and_next_message_keeps_all_variants(self):
        result = parse_documents(["Xiaomi\n• Note 17 6/256 Black🇪🇺\n🇪🇺 От 1 шт - 19 900\nОт 6 шт - 18 000\n"
                                  "• Note 17 6/256 Sky Teal🇪🇺", "🇪🇺 От 1 шт - 19 900\nPOCO F8 Pro 12/512 Black\n25 500"])
        self.assertEqual(len(result.items), 3)
        self.assertEqual([item.price for item in result.items], [Decimal(19900), Decimal(19900), Decimal(25500)])
        self.assertIn("Sky Teal", result.items[1].title)
        self.assertEqual(result.items[0].title.count("🇪🇺"), 1)

    def test_new_brands_and_apple_families_have_their_own_blocks(self):
        rows = ["Realme C75", "Tecno Camon 40", "Infinix Note 50", "OnePlus 13", "Nothing Phone 3", "AirPods 4", "Apple Watch S10", "iPad Air M3", "MacBook Air M4"]
        items = parse_documents(["\n".join(name + " — 20000" for name in rows)]).items
        self.assertEqual([item.block for item in items], ["Realme", "Tecno", "Infinix", "OnePlus", "Nothing", "AirPods", "Apple Watch", "iPad", "MacBook / iMac"])

    def test_accessory_below_100_and_no_status_defaults_inactive(self):
        items = parse_documents(["AirTag 1 pack — 35 $\niPhone 16 Pro 256 Natural CPO 🇺🇸 — 79000"]).items
        self.assertEqual(items[0].price, Decimal(35))
        content = "\n".join(render_blocks(items, Settings()).values())
        self.assertNotIn("Не активированное", content)
        self.assertNotIn("Статус не указан", content)

    def test_all_selection_overrides_old_env_filters(self):
        items = parse_documents(["Realme C75 — 10000\nЧехол iPhone 17 — 500"]).items
        settings = Settings(include_blocks=("Samsung",), allow_accessories=False)
        self.assertEqual(select_items(items, settings), [])
        selected = select_items(items, settings, {"include_blocks": [], "allow_accessories": True})
        self.assertEqual(len(selected), 2)

    def test_price_column_does_not_get_confused_with_stock_or_sku(self):
        text = table_text([["Товар", "Цвет", "Цена", "Остаток", "Артикул"],
                           ["Samsung S26 12/256", "Blue", 60000, 128, "SKU12345"],
                           ["Honor 400 12/256", "Black", 30000, 99, "SKU99999"]])
        items = parse_documents([text]).items
        self.assertEqual([item.price for item in items], [Decimal(60000), Decimal(30000)])
        self.assertNotIn("SKU", items[0].title)


def message(number, text, buttons=()):
    return SimpleNamespace(id=number, raw_text=text, buttons=buttons, document=None, out=False)


class CatalogSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reused_next_callback_collects_every_page(self):
        next_button = SimpleNamespace(text="Далее", button=types.KeyboardButtonCallback("Далее", b"next"), click=AsyncMock())
        pages = [message(1, f"Realme C{i} — {10000 + i}", [[next_button]] if i < 3 else []) for i in range(4)]
        reader = SupplierReader(None, Settings(action_delay=0), Source("@supplier"))
        reader.messages = AsyncMock(return_value=pages[:1])
        reader.collect_action = AsyncMock(side_effect=[[page] for page in pages[1:]])
        result = await reader.fetch()
        self.assertEqual(len(result.items), 4)
        self.assertEqual(reader.collect_action.await_count, 3)

    async def test_static_feed_is_not_truncated_at_action_history_limit(self):
        rows = [message(i, f"Realme C{i} — 10000") for i in range(310, 0, -1)]
        client = SimpleNamespace(get_messages=AsyncMock(side_effect=lambda entity, limit: rows if limit is None else rows[:limit]))
        reader = SupplierReader(client, Settings(), Source("@supplier", mode="feed"))
        reader.entity = "supplier"
        result = await reader.fetch()
        self.assertEqual(len(result.items), 310)
        self.assertEqual(client.get_messages.call_args.kwargs["limit"], None)

    async def test_all_linked_sections_read_once_without_invites_or_orders(self):
        first_link = SimpleNamespace(text="iPhone", button=types.KeyboardButtonUrl("iPhone", "https://t.me/sourceprice/10"))
        second_link = SimpleNamespace(text="Dyson", button=types.KeyboardButtonUrl("Dyson", "https://t.me/sourceprice/11"))
        invite = SimpleNamespace(text="Вступить", button=types.KeyboardButtonUrl("Вступить", "https://t.me/+invite"))
        posts = {10: message(10, "iPhone 17 256 Black — 60000", [[second_link]]),
                 11: message(11, "Dyson HS08 — 40000", [[first_link]])}
        client = SimpleNamespace(get_messages=AsyncMock(side_effect=lambda entity, ids: posts[ids]))
        reader = SupplierReader(client, Settings(action_delay=0), Source("@supplier", mode="feed"))
        reader.messages = AsyncMock(return_value=[message(1, "Каталог", [[first_link, second_link, invite]])])
        with patch("supplier.resolve_input_peer", new=AsyncMock(return_value="entity")):
            result = await reader.fetch()
        self.assertEqual(len(result.items), 2)
        self.assertEqual(client.get_messages.await_count, 2)

    async def test_callback_categories_read_and_purchase_button_ignored(self):
        category = SimpleNamespace(text="Realme", button=types.KeyboardButtonCallback("Realme", b"category"), click=AsyncMock())
        purchase = SimpleNamespace(text="Купить Realme", button=types.KeyboardButtonCallback("Купить Realme", b"buy"), click=AsyncMock())
        reader = SupplierReader(None, Settings(action_delay=0), Source("@supplier"))
        reader.messages = AsyncMock(return_value=[message(1, "Каталог", [[category, purchase]])])
        reader.collect_action = AsyncMock(return_value=[message(2, "Realme C75 — 10000")])
        result = await reader.fetch()
        self.assertEqual(len(result.items), 1)
        reader.collect_action.assert_awaited_once_with(category.click)

    async def test_missing_linked_section_fails_instead_of_publishing_partial_catalog(self):
        button = SimpleNamespace(text="Dyson", button=types.KeyboardButtonUrl("Dyson", "https://t.me/sourceprice/11"))
        reader = SupplierReader(SimpleNamespace(get_messages=AsyncMock(return_value=None)), Settings(action_delay=0), Source("@supplier"))
        with patch("supplier.resolve_input_peer", new=AsyncMock(return_value="entity")):
            with self.assertRaisesRegex(RuntimeError, "удалил раздел"):
                await reader.expand_catalog([message(1, "iPhone 17 256 Black — 60000", [[button]])])

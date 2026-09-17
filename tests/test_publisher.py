import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.extensions import html

from config import Settings
from publisher import Publisher
from state import StateStore


class FakeTelegram:
    def __init__(self):
        self.rows = {}
        self.sent = 0
        self.edited = 0
        self.deleted = []
        self.lose_send = False
        self.fail_edit = False

    async def get_me(self):
        return SimpleNamespace(id=42)

    async def get_messages(self, target, ids=None):
        if isinstance(ids, int):
            return self.rows.get(ids)
        return [self.rows.get(i) for i in ids or []]

    async def iter_messages(self, target, **kwargs):
        for message in sorted(self.rows.values(), key=lambda m: m.id, reverse=True):
            yield message

    async def send_message(self, target, text, **kwargs):
        self.sent += 1
        raw, entities = html.parse(text)
        message = SimpleNamespace(id=self.sent + 100, raw_text=raw, message=raw,
                                  entities=entities, out=True, sender_id=42)
        self.rows[message.id] = message
        if self.lose_send:
            self.lose_send = False
            raise ConnectionError("response lost")
        return message

    async def edit_message(self, target, message_id, text, **kwargs):
        if self.fail_edit:
            raise PermissionError("edit forbidden")
        self.edited += 1
        raw, entities = html.parse(text)
        self.rows[message_id].raw_text = raw
        self.rows[message_id].entities = entities

    async def delete_messages(self, target, ids):
        self.deleted.extend(ids)
        for number in ids:
            self.rows.pop(number, None)


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.json")
        self.client = FakeTelegram()
        self.publisher = Publisher(self.client, "@target", self.store, Settings(send_delay=0))
        self.pages = {"a:0": "📦 АКТУАЛЬНЫЙ ПРАЙС\n\n<b>— iPhone 17 —</b>\n\n<code>iPhone 17</code> — 60000"}

    def tearDown(self):
        self.temp.cleanup()

    async def test_repeated_sync_edits_in_place(self):
        await self.publisher.publish(self.pages)
        await self.publisher.publish(self.pages)
        self.assertEqual(self.client.sent, 1)
        self.assertEqual(self.client.edited, 0)
        changed = {key: text.replace("60000", "61000") for key, text in self.pages.items()}
        await self.publisher.publish(changed)
        self.assertEqual(self.client.sent, 1)
        self.assertEqual(self.client.edited, 1)

    async def test_deleted_message_recreated_even_when_hash_equal(self):
        await self.publisher.publish(self.pages)
        self.client.rows.clear()
        await self.publisher.publish(self.pages)
        self.assertEqual(self.client.sent, 2)

    async def test_edit_error_never_falls_back_to_duplicate_send(self):
        await self.publisher.publish(self.pages)
        self.client.fail_edit = True
        with self.assertRaises(PermissionError):
            await self.publisher.publish({"a:0": self.pages["a:0"] + "\nЦена изменилась"})
        self.assertEqual(self.client.sent, 1)

    async def test_restart_recovers_message_without_manifest(self):
        await self.publisher.publish(self.pages)
        fresh = StateStore(Path(self.temp.name) / "fresh.json")
        await Publisher(self.client, "@target", fresh, Settings(send_delay=0)).publish(self.pages)
        self.assertEqual(self.client.sent, 1)

    async def test_lost_send_response_is_recovered(self):
        self.client.lose_send = True
        with self.assertRaises(ConnectionError):
            await self.publisher.publish(self.pages)
        await self.publisher.publish(self.pages)
        self.assertEqual(self.client.sent, 1)
        self.assertIsNone(self.store.get("pending_publish"))

    async def test_lost_replacement_response_is_recovered(self):
        await self.publisher.publish(self.pages)
        self.client.rows.clear()
        self.client.lose_send = True
        with self.assertRaises(ConnectionError):
            await self.publisher.publish(self.pages)
        await self.publisher.publish(self.pages)
        self.assertEqual(self.client.sent, 2)

    async def test_disabling_deletes_only_managed_posts(self):
        await self.publisher.publish(self.pages)
        external = SimpleNamespace(id=999, raw_text="ГАРАНТИЯ: 7 дней", message="ГАРАНТИЯ: 7 дней",
                                   entities=[], out=True, sender_id=42)
        self.client.rows[999] = external
        await self.publisher.publish({})
        self.assertIn(999, self.client.rows)
        self.assertNotIn(999, self.client.deleted)

    async def test_legacy_single_post_is_reused(self):
        old = await self.client.send_message("@target", "📦 АКТУАЛЬНЫЙ ПРАЙС\n\nСтарый прайс")
        self.store.set("target_message_id", old.id)
        await self.publisher.publish(self.pages)
        self.assertEqual(self.client.sent, 1)
        self.assertEqual(self.client.edited, 1)

    async def test_night_without_cache_hides_existing_price_pages(self):
        await self.publisher.publish(self.pages)
        fresh = StateStore(Path(self.temp.name) / "empty.json")
        await Publisher(self.client, "@target", fresh, Settings(send_delay=0)).hide_existing()
        self.assertEqual(self.client.sent, 1)
        self.assertIn("Продажи закрыты", next(iter(self.client.rows.values())).raw_text)
        self.assertNotIn("60000", next(iter(self.client.rows.values())).raw_text)

    async def test_night_legacy_post_reopens_in_place(self):
        await self.client.send_message("@target", "📦 АКТУАЛЬНЫЙ ПРАЙС\n\niPhone 17 — 60000")
        await self.publisher.hide_existing()
        self.assertNotIn("60000", next(iter(self.client.rows.values())).raw_text)
        await self.publisher.publish(self.pages)
        self.assertEqual(self.client.sent, 1)

    async def test_night_does_not_touch_guarantee(self):
        await self.client.send_message("@target", "ГАРАНТИЯ: 7 дней")
        await self.publisher.hide_existing()
        self.assertEqual(next(iter(self.client.rows.values())).raw_text, "ГАРАНТИЯ: 7 дней")

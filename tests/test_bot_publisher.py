import tempfile
import unittest
from pathlib import Path

from bot_publisher import BotAPIPublisher
from config import Settings
from state import StateStore


class FakeBotPublisher(BotAPIPublisher):
    def __init__(self, target, state, settings, chats, members=None):
        super().__init__("TOKEN", target, state, settings)
        self.chats = chats
        self.members = members or {}
        self.calls = []
        self.next_message_id = 123
        self.missing_ids = set()

    async def api(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 999, "username": "control_bot"}
        if method == "getChat":
            chat_id = payload["chat_id"]
            if chat_id not in self.chats:
                raise RuntimeError("chat not found")
            return dict(self.chats[chat_id])
        if method == "getChatMember":
            chat_id = payload["chat_id"]
            return dict(self.members.get(chat_id, {"status": "administrator", "can_post_messages": True}))
        if method == "sendMessage":
            message_id = self.next_message_id
            self.next_message_id += 1
            return {"message_id": message_id}
        if method == "editMessageText":
            if int(payload["message_id"]) in self.missing_ids:
                raise RuntimeError("Bot API editMessageText: Bad Request: MESSAGE_ID_INVALID")
            raise RuntimeError("Bot API editMessageText: Bad Request: message is not modified")
        if method == "deleteMessage":
            if int(payload["message_id"]) in self.missing_ids:
                raise RuntimeError("Bot API deleteMessage: Bad Request: MESSAGE_ID_INVALID")
            return True
        raise AssertionError(method)


class BotPublisherTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "state.json")

    def tearDown(self):
        self.temp.cleanup()

    async def test_supplier_or_user_id_is_never_used_as_destination(self):
        settings = Settings(send_delay=0, admin_ids=(42,))
        chats = {
            6781674751: {"id": 6781674751, "type": "private", "username": "supplier_bot"},
            42: {"id": 42, "type": "private", "first_name": "Admin"},
        }
        publisher = FakeBotPublisher(6781674751, self.state, settings, chats)

        with self.assertRaisesRegex(RuntimeError, "/bind"):
            await publisher.ensure_target()

        self.assertIsNone(publisher.target)
        self.assertFalse(publisher.used_admin_fallback)

    async def test_valid_channel_is_used_directly(self):
        settings = Settings(send_delay=0, admin_ids=(42,))
        chats = {
            "@prices": {"id": -100123, "type": "channel", "title": "Prices"},
            42: {"id": 42, "type": "private", "first_name": "Admin"},
        }
        publisher = FakeBotPublisher("@prices", self.state, settings, chats)

        target = await publisher.ensure_target()

        self.assertEqual(target, -100123)
        self.assertFalse(publisher.used_admin_fallback)

    async def test_publish_works_after_persistent_group_binding(self):
        settings = Settings(send_delay=0, admin_ids=(42,))
        group_id = -100777
        chats = {
            6781674751: {"id": 6781674751, "type": "private", "username": "supplier_bot"},
            group_id: {"id": group_id, "type": "supergroup", "title": "Розница"},
        }
        publisher = FakeBotPublisher(6781674751, self.state, settings, chats)
        await publisher.bind_group(group_id, title="Розница", chat_type="supergroup")

        changes = await publisher.publish({"iphone:0": "Прайс"})

        self.assertEqual(changes, 1)
        self.assertTrue(any(method == "sendMessage" and payload["chat_id"] == group_id
                            for method, payload in publisher.calls))

    async def test_unchanged_page_keeps_same_id_after_existence_probe(self):
        settings = Settings(send_delay=0, admin_ids=(42,))
        group_id = -100777
        chats = {
            group_id: {"id": group_id, "type": "supergroup", "title": "Розница"},
        }
        publisher = FakeBotPublisher(group_id, self.state, settings, chats)

        first_changes = await publisher.publish({"iphone:0": "Одинаковый прайс"})
        self.assertEqual(first_changes, 1)

        publisher.calls.clear()
        second_changes = await publisher.publish({"iphone:0": "Одинаковый прайс"})

        self.assertEqual(second_changes, 0)
        self.assertTrue(any(method == "editMessageText" for method, _ in publisher.calls))
        self.assertFalse(any(method == "sendMessage" for method, _ in publisher.calls))

    async def test_changed_page_is_still_edited(self):
        settings = Settings(send_delay=0, admin_ids=(42,))
        group_id = -100777
        chats = {
            group_id: {"id": group_id, "type": "supergroup", "title": "Розница"},
        }
        publisher = FakeBotPublisher(group_id, self.state, settings, chats)
        await publisher.publish({"iphone:0": "Старый прайс"})

        original_api = publisher.api
        async def changed_api(method, **payload):
            if method == "editMessageText" and payload.get("text") == "Новый прайс":
                publisher.calls.append((method, payload))
                return True
            return await original_api(method, **payload)
        publisher.api = changed_api
        publisher.calls.clear()
        changes = await publisher.publish({"iphone:0": "Новый прайс"})

        self.assertEqual(changes, 1)
        self.assertTrue(any(method == "editMessageText" and payload.get("text") == "Новый прайс"
                            for method, payload in publisher.calls))

    async def test_deleted_saved_id_rebuilds_all_managed_price_posts(self):
        settings = Settings(send_delay=0, admin_ids=(42,))
        group_id = -100777
        chats = {group_id: {"id": group_id, "type": "supergroup", "title": "Розница"}}
        publisher = FakeBotPublisher(group_id, self.state, settings, chats)
        pages = {"one:0": "Первый блок", "two:0": "Второй блок"}

        await publisher.publish(pages)
        old_manifest = self.state.get("published")["messages"]
        old_ids = [old_manifest[key]["id"] for key in pages]
        publisher.missing_ids.add(old_ids[0])
        publisher.calls.clear()

        changes = await publisher.publish(pages)
        new_manifest = self.state.get("published")["messages"]
        new_ids = [new_manifest[key]["id"] for key in pages]

        self.assertNotEqual(old_ids, new_ids)
        self.assertGreater(changes, 1)
        self.assertTrue(any(method == "deleteMessage" and payload["message_id"] == old_ids[1]
                            for method, payload in publisher.calls))
        self.assertEqual(sum(1 for method, _ in publisher.calls if method == "sendMessage"), 2)


    async def test_failed_rebuild_keeps_old_live_posts_until_new_set_is_complete(self):
        settings = Settings(send_delay=0, admin_ids=(42,))
        group_id = -100777
        chats = {group_id: {"id": group_id, "type": "supergroup", "title": "Розница"}}
        publisher = FakeBotPublisher(group_id, self.state, settings, chats)
        pages = {"one:0": "Первый блок", "two:0": "Второй блок"}

        await publisher.publish(pages)
        old_manifest = self.state.get("published")["messages"]
        old_ids = [old_manifest[key]["id"] for key in pages]
        publisher.missing_ids.add(old_ids[0])
        original_api = publisher.api

        async def fail_replacement_send(method, **payload):
            if method == "sendMessage":
                publisher.calls.append((method, payload))
                raise RuntimeError("Bot API sendMessage: временный сбой соединения: ServerDisconnectedError")
            return await original_api(method, **payload)

        publisher.api = fail_replacement_send
        publisher.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "ServerDisconnectedError"):
            await publisher.publish(pages)

        # The surviving old block must still be live; destructive deletion is delayed
        # until a complete new set has been sent successfully.
        self.assertFalse(any(method == "deleteMessage" and payload["message_id"] == old_ids[1]
                             for method, payload in publisher.calls))
        staged = self.state.get("rebuild_old_manifest", {})
        self.assertEqual(staged.get("binding"), publisher.binding())
        self.assertEqual(len(staged.get("messages", {})), 2)



if __name__ == "__main__":
    unittest.main()

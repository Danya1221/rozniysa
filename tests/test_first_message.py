import tempfile
import unittest
from pathlib import Path

from config import Settings
from first_message_publisher import PinnedBotAPIPublisher
from state import StateStore


class FakePinnedPublisher(PinnedBotAPIPublisher):
    def __init__(self, state, settings):
        super().__init__("TOKEN", -100777, state, settings)
        self.calls = []
        self.next_id = 100

    async def api(self, method, **payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 999, "username": "control_bot"}
        if method == "getChat":
            return {"id": -100777, "type": "supergroup", "title": "Retail"}
        if method == "getChatMember":
            return {"status": "administrator", "can_pin_messages": True}
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method in {"editMessageText", "deleteMessage", "pinChatMessage"}:
            return True
        raise AssertionError(method)


class FirstMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "state.json")
        self.settings = Settings(send_delay=0)
        self.publisher = FakePinnedPublisher(self.state, self.settings)

    def tearDown(self):
        self.temp.cleanup()

    async def test_first_message_is_created_pinned_and_before_republished_prices(self):
        await self.publisher.ensure_target()
        binding = self.publisher.binding()
        self.state.set("published", {
            "binding": binding,
            "messages": {"old": {"id": 55, "hash": "x", "content": "old"}},
        })

        await self.publisher.set_first_message("Наше первое сообщение")

        methods = [method for method, _ in self.publisher.calls]
        self.assertIn("deleteMessage", methods)
        self.assertIn("pinChatMessage", methods)
        record = self.state.get("first_message")
        self.assertTrue(record["pinned"])
        first_id = record["id"]
        self.assertEqual(self.state.get("published")["messages"], {})

        await self.publisher.publish({"apple:0": "<b>— Apple —</b>\n\n<code>AirPods — 10000</code>"})

        sends = [payload for method, payload in self.publisher.calls if method == "sendMessage"]
        self.assertEqual(sends[0]["text"], "Наше первое сообщение")
        self.assertIn("AirPods", sends[1]["text"])
        pin = next(payload for method, payload in self.publisher.calls if method == "pinChatMessage")
        self.assertEqual(pin["message_id"], first_id)

    async def test_editing_first_message_keeps_same_message_id(self):
        await self.publisher.set_first_message("Первый текст")
        first_id = self.state.get("first_message")["id"]
        send_count = sum(1 for method, _ in self.publisher.calls if method == "sendMessage")

        await self.publisher.set_first_message("Новый текст")

        self.assertEqual(self.state.get("first_message")["id"], first_id)
        self.assertEqual(sum(1 for method, _ in self.publisher.calls if method == "sendMessage"), send_count)
        self.assertTrue(any(method == "editMessageText" for method, _ in self.publisher.calls))

    async def test_deleted_saved_first_message_is_recreated_even_when_text_is_unchanged(self):
        await self.publisher.set_first_message("Гарантия и выдача")
        old_id = self.state.get("first_message")["id"]
        self.publisher.calls.clear()
        real = self.publisher.api

        async def missing(method, **payload):
            if method == "editMessageText" and int(payload.get("message_id") or 0) == old_id:
                self.publisher.calls.append((method, payload))
                raise RuntimeError("Bad Request: message to edit not found")
            return await real(method, **payload)

        self.publisher.api = missing
        await self.publisher.publish({})

        record = self.state.get("first_message")
        self.assertNotEqual(record["id"], old_id)
        self.assertTrue(record["pinned"])
        self.assertTrue(any(method == "sendMessage" and payload.get("text") == "Гарантия и выдача"
                            for method, payload in self.publisher.calls))
        self.assertTrue(any(method == "pinChatMessage" and payload.get("message_id") == record["id"]
                            for method, payload in self.publisher.calls))


if __name__ == "__main__":
    unittest.main()

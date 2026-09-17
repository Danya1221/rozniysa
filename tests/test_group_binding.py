import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot_publisher import BotAPIPublisher
from config import Settings
from control_group import GroupBindingController
from state import StateStore


class FakePublisher(BotAPIPublisher):
    def __init__(self, *args, chats=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.chats = chats or {}

    async def api(self, method, **payload):
        if method == "getMe":
            return {"id": 999, "username": "control_bot"}
        if method == "getChat":
            chat_id = payload["chat_id"]
            if chat_id not in self.chats:
                raise RuntimeError("chat not found")
            return self.chats[chat_id]
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": True}
        raise AssertionError(method)


class GroupBindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "state.json")
        self.settings = Settings(admin_ids=(42,), send_delay=0)

    def tearDown(self):
        self.temp.cleanup()

    async def test_private_target_is_never_used_as_publish_destination(self):
        publisher = FakePublisher(
            "token", 6781674751, self.state, self.settings,
            chats={6781674751: {"id": 6781674751, "type": "private", "first_name": "supplier"}},
        )
        with self.assertRaisesRegex(RuntimeError, "/bind"):
            await publisher.ensure_target()
        self.assertIsNone(publisher.target)

    async def test_bind_group_overrides_bad_configured_target_and_persists(self):
        group_id = -1001234567890
        chats = {
            6781674751: {"id": 6781674751, "type": "private", "first_name": "supplier"},
            group_id: {"id": group_id, "type": "supergroup", "title": "Розница"},
        }
        publisher = FakePublisher("token", 6781674751, self.state, self.settings, chats=chats)
        await publisher.bind_group(group_id, title="Розница", chat_type="supergroup")
        self.assertEqual(publisher.target, group_id)
        self.assertEqual(self.state.get("publish_target")["chat_id"], group_id)

        restarted = FakePublisher("token", 6781674751, self.state, self.settings, chats=chats)
        self.assertEqual(await restarted.ensure_target(), group_id)
        self.assertEqual(restarted.target_title, "Розница")


class ForwardBindingControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "state.json")
        self.publisher = SimpleNamespace(bind_group=AsyncMock(return_value=-100777))
        self.service = SimpleNamespace(
            publisher=self.publisher,
            state=self.state,
            ready=False,
            settings=Settings(admin_ids=(42,), send_delay=0),
        )
        self.controller = GroupBindingController("TOKEN", self.service, [42])
        self.controller.username = "Pricebottok_bot"
        self.controller.send = AsyncMock()

    def tearDown(self):
        self.temp.cleanup()

    async def test_forwarded_group_message_binds_without_command(self):
        message = {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "любое сообщение",
            "forward_origin": {
                "type": "channel",
                "chat": {"id": -100777, "type": "supergroup", "title": "Розница"},
            },
        }

        await self.controller.handle_message(message)

        self.publisher.bind_group.assert_awaited_once_with(
            -100777, title="Розница", chat_type="supergroup"
        )
        self.assertIn("Группа публикации привязана", self.state.get("last_result"))

    async def test_hidden_group_origin_offers_native_chat_picker(self):
        message = {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "text": "переслано",
            "forward_origin": {"type": "user", "sender_user": {"id": 7}},
        }

        await self.controller.handle_message(message)

        self.publisher.bind_group.assert_not_awaited()
        payload = self.controller.send.await_args.args
        markup = payload[2]
        button = markup["keyboard"][0][0]
        self.assertIn("request_chat", button)
        self.assertTrue(button["request_chat"]["bot_is_member"])

    async def test_chat_shared_result_binds_group(self):
        message = {
            "from": {"id": 42},
            "chat": {"id": 42, "type": "private"},
            "chat_shared": {"request_id": 731, "chat_id": -100888, "title": "Опт"},
        }

        await self.controller.handle_message(message)

        self.publisher.bind_group.assert_awaited_once_with(
            -100888, title="Опт", chat_type="group"
        )


if __name__ == "__main__":
    unittest.main()

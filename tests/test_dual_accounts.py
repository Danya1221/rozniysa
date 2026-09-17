import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings, Source
from control_botapi import BotAPIController
from prices import Item, ParseResult
from runtime import SyncService
from state import StateStore


class DualAccountRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "state.json")
        self.settings = Settings(
            sources=(Source("@hi", label="HI"),),
            off_hours=False,
            session="ONE",
            session_2="TWO",
        )
        self.client1 = SimpleNamespace(
            is_connected=lambda: True,
            connect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=True),
            disconnect=AsyncMock(),
        )
        self.client2 = SimpleNamespace(
            is_connected=lambda: True,
            connect=AsyncMock(),
            is_user_authorized=AsyncMock(return_value=True),
            disconnect=AsyncMock(),
        )
        self.service = SyncService(self.client1, self.settings, self.state)
        self.service.attach_client(self.client2, 2)
        self.service.publisher.publish = AsyncMock(return_value=0)

    def tearDown(self):
        self.temp.cleanup()

    async def test_second_account_reads_only_primary_hi_supplier(self):
        other = Source("@other", label="OTHER")
        self.settings.sources = (self.settings.sources[0], other)
        service = SyncService(self.client1, self.settings, self.state)
        service.attach_client(self.client2, 2)

        primary = [reader.source.peer for reader in service.readers_for_slot(1)]
        secondary = [reader.source.peer for reader in service.readers_for_slot(2)]
        active = [(slot, reader.source.peer) for slot, reader in service.active_reader_entries()]

        self.assertEqual(primary, ["@hi", "@other"])
        self.assertEqual(secondary, ["@hi"])
        self.assertEqual(active, [(1, "@hi"), (1, "@other"), (2, "@hi")])

    async def test_same_hi_variant_from_two_accounts_uses_lower_price(self):
        expensive = Item("iPhone 17 256 Black", Decimal("60000"), "RUB", "iPhone 17")
        cheap = Item("iPhone 17 256 Black", Decimal("57000"), "RUB", "iPhone 17")
        self.service.readers_for_slot(1)[0].fetch = AsyncMock(return_value=ParseResult([expensive], []))
        self.service.readers_for_slot(2)[0].fetch = AsyncMock(return_value=ParseResult([cheap], []))

        with patch("runtime.is_open", return_value=True):
            result = await self.service.sync(force=True)

        self.assertIn("аккаунт 2", result)
        pages = self.service.publisher.publish.call_args.args[0]
        body = "\n".join(pages.values())
        self.assertIn("57 000", body)
        self.assertNotIn("60 000", body)
        cache = self.state.get("sources")
        self.assertIn("@hi", cache)
        self.assertIn("account2:@hi", cache)


class DualAccountControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_login_is_saved_in_separate_slot_and_requests_reconnect(self):
        service = MagicMock()
        service.settings = SimpleNamespace(api_id=123, api_hash="hash", session="", session_2="")
        service.state = MagicMock()
        service.startup_error = "missing"
        service.account_errors = {}
        service.request_reconnect = MagicMock()
        controller = BotAPIController("TOKEN", service, [42])
        controller.send = AsyncMock()
        temp = SimpleNamespace(
            session=SimpleNamespace(save=lambda: "SECOND_SESSION"),
            disconnect=AsyncMock(),
        )
        controller.login_flows[42] = {"stage": "password", "client": temp, "chat_id": 42, "slot": 2}

        await controller.finish_login(42, 42, controller.login_flows[42])

        service.state.set.assert_called_with("session_string_2", "SECOND_SESSION")
        self.assertEqual(service.settings.session_2, "SECOND_SESSION")
        service.request_reconnect.assert_called_once()
        temp.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

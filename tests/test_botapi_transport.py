import tempfile
import unittest
from pathlib import Path

import aiohttp

from bot_publisher import BotAPIPublisher
from config import Settings
from state import StateStore


class FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status

    async def json(self, content_type=None):
        return self.data


class FakeContext:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.closed = False
        self.calls = []

    def post(self, url, json):
        self.calls.append((url, json))
        if not self.outcomes:
            raise AssertionError("No fake HTTP outcome left")
        return FakeContext(self.outcomes.pop(0))

    async def close(self):
        self.closed = True


class BotAPITransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.temp.name) / "state.json")
        self.publisher = BotAPIPublisher("TOKEN", -100777, self.state, Settings(send_delay=0))

    def tearDown(self):
        self.temp.cleanup()

    async def test_edit_reconnects_after_server_disconnected(self):
        first = FakeSession([aiohttp.ServerDisconnectedError("Server disconnected")])
        second = FakeSession([FakeResponse({"ok": True, "result": True})])
        sessions = [first, second]
        self.publisher._new_http = lambda: sessions.pop(0)

        result = await self.publisher.api(
            "editMessageText", chat_id=-100777, message_id=123, text="updated"
        )

        self.assertTrue(result)
        self.assertTrue(first.closed)
        self.assertEqual(len(second.calls), 1)

    async def test_send_message_does_not_blind_retry_ambiguous_disconnect(self):
        first = FakeSession([aiohttp.ServerDisconnectedError("Server disconnected")])
        unused = FakeSession([FakeResponse({"ok": True, "result": {"message_id": 1}})])
        sessions = [first, unused]
        self.publisher._new_http = lambda: sessions.pop(0)

        with self.assertRaisesRegex(RuntimeError, "ServerDisconnectedError"):
            await self.publisher.api("sendMessage", chat_id=-100777, text="hello")

        self.assertTrue(first.closed)
        self.assertEqual(len(unused.calls), 0)

    async def test_edit_retries_bot_api_502(self):
        session = FakeSession([
            FakeResponse({"ok": False, "description": "Bad Gateway"}, status=502),
        ])
        second = FakeSession([FakeResponse({"ok": True, "result": True})])
        sessions = [session, second]
        self.publisher._new_http = lambda: sessions.pop(0)

        result = await self.publisher.api(
            "editMessageText", chat_id=-100777, message_id=123, text="updated"
        )

        self.assertTrue(result)
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()

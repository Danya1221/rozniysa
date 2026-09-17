import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings, Source
from control import Controller
from main import main, run_supplier
from runtime import SyncService
from state import StateStore


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(api_id=123, api_hash="test", bot_token="FAKE_TOKEN",
                                 admin_ids=(42,), state_file=str(Path(self.temp.name) / "state.json"))
        self.state = StateStore(self.settings.state_file)
        self.service = SyncService(None, self.settings, self.state)

    def tearDown(self):
        self.temp.cleanup()

    async def test_supplier_failure_keeps_controller_alive_and_answers_start(self):
        failed = asyncio.Event()
        controller = Controller(MagicMock(), self.service, [42])
        with patch("main.prepare_supplier", new=AsyncMock(side_effect=ValueError("Сессия отозвана"))), \
             patch("main.log.error", side_effect=lambda *args: failed.set()):
            task = asyncio.create_task(run_supplier(self.service, self.settings, controller))
            try:
                await asyncio.wait_for(failed.wait(), timeout=1)
                event = SimpleNamespace(is_private=True, sender_id=42, raw_text="/start", respond=AsyncMock())
                await controller.message(event)
                self.assertIn("Сессия отозвана", event.respond.call_args.args[0])
                self.assertTrue(event.respond.call_args.kwargs["buttons"])
                self.assertFalse(task.done())
            finally:
                self.service.stop_event.set()
                await asyncio.wait_for(task, timeout=1)

    async def test_supplier_can_recover_without_restarting_controller(self):
        client = SimpleNamespace(disconnect=AsyncMock())
        attempts = []

        async def prepare(service, settings, controller):
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("Временный сбой")
            service.attach_client(client)
            service.ready = True
            service.startup_error = None

        async def run():
            self.assertTrue(self.service.ready)
            self.service.stop_event.set()

        self.service.run = run
        with patch("main.prepare_supplier", new=prepare):
            await asyncio.wait_for(run_supplier(self.service, self.settings, retry_seconds=0.001), timeout=1)
        self.assertEqual(len(attempts), 2)
        client.disconnect.assert_awaited_once()

    async def test_real_startup_starts_bot_api_control_before_missing_session_error(self):
        # Exercises main's actual order: HTTP Bot API control starts first, then
        # supplier initialization may fail without killing the control runtime.
        failed = asyncio.Event()
        fake_controller = MagicMock()
        fake_controller.admins = {42}
        fake_controller.start = AsyncMock()
        fake_controller.close = AsyncMock()

        async def control_run():
            await asyncio.Event().wait()

        fake_controller.run = control_run

        with patch("main.Settings.from_env", return_value=self.settings), \
             patch("main.BotAPIController", return_value=fake_controller), \
             patch("main.prepare_supplier", new=AsyncMock(side_effect=ValueError("SESSION_STRING не задана"))), \
             patch("main.log.error", side_effect=lambda *args: failed.set()), \
             patch.object(asyncio.get_running_loop(), "add_signal_handler"):
            task = asyncio.create_task(main())
            try:
                await asyncio.wait_for(failed.wait(), timeout=1)
                fake_controller.start.assert_awaited_once()
                self.assertFalse(task.done())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        fake_controller.close.assert_awaited()

    async def test_fallback_owner_is_never_granted_to_first_message_sender(self):
        controller = Controller(MagicMock(), self.service, [])
        event = SimpleNamespace(is_private=True, sender_id=99, raw_text="/start", respond=AsyncMock())
        await controller.message(event)
        self.assertEqual(controller.admins, set())
        self.assertIn("ADMIN_IDS", event.respond.call_args.args[0])
        self.assertIsNone(event.respond.call_args.kwargs["buttons"])

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from control import Controller


class ControlLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_saves_session_and_updates_runtime_settings(self):
        control_client = MagicMock()
        service = MagicMock()
        service.settings = SimpleNamespace(api_id=123, api_hash="hash", session="")
        service.state = MagicMock()
        service.startup_error = "SESSION_STRING missing"

        temp = SimpleNamespace(
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="code-hash")),
            sign_in=AsyncMock(),
            session=SimpleNamespace(save=lambda: "SAVED_STRING_SESSION"),
        )

        controller = Controller(control_client, service, [42], username="price_control_bot")
        event = SimpleNamespace(is_private=True, sender_id=42, raw_text="/login", respond=AsyncMock())

        with patch("control.TelegramClient", return_value=temp):
            await controller.message(event)
            self.assertIn(42, controller.login_flows)

            event.raw_text = "+79991234567"
            await controller.message(event)
            temp.connect.assert_awaited_once()
            temp.send_code_request.assert_awaited_once_with("+79991234567")
            self.assertEqual(controller.login_flows[42]["stage"], "code")

            event.raw_text = "12345"
            await controller.message(event)

        temp.sign_in.assert_awaited_once_with(
            phone="+79991234567", code="12345", phone_code_hash="code-hash")
        service.state.set.assert_called_with("session_string", "SAVED_STRING_SESSION")
        self.assertEqual(service.settings.session, "SAVED_STRING_SESSION")
        self.assertIsNone(service.startup_error)
        self.assertNotIn(42, controller.login_flows)
        temp.disconnect.assert_awaited_once()

    async def test_login_requires_admin(self):
        control_client = MagicMock()
        service = MagicMock()
        controller = Controller(control_client, service, [42], username="price_control_bot")
        event = SimpleNamespace(is_private=True, sender_id=99, raw_text="/login", respond=AsyncMock())
        await controller.message(event)
        self.assertNotIn(99, controller.login_flows)
        self.assertIn("ADMIN_IDS", event.respond.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

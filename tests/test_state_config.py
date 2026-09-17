import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings, peer
from state import StateStore


class StateTests(unittest.TestCase):
    def test_nested_path_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "state.json"
            store = StateStore(path)
            store.update({"options": {"enabled": False}, "message": 123})
            loaded = StateStore(path)
            self.assertFalse(loaded.get("options")["enabled"])
            self.assertEqual(loaded.get("message"), 123)
            self.assertEqual(list(path.parent.glob("state.json.*")), [])

    def test_corrupt_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{broken")
            with self.assertRaises(RuntimeError):
                StateStore(path)

    def test_failed_write_preserves_memory_and_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.set("id", 1)
            with patch("state.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.set("id", 2)
            self.assertEqual(store.get("id"), 1)
            self.assertEqual(json.loads(store.path.read_text())["id"], 1)

    def test_second_worker_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            one = StateStore(Path(directory) / "state.json")
            two = StateStore(one.path)
            one.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    two.acquire()
            finally:
                one.close()
            two.acquire()
            two.close()


class ConfigTests(unittest.TestCase):
    def test_existing_railway_variable_names(self):
        with patch.dict(os.environ, {
            "API_ID": "123", "API_HASH": "test", "SESSION_STRING": "test-session",
            "TARGET_CHANNEL": "@target", "SUPPLIER_BOT": "@one", "SUPPLIER_BOT_2": "@two",
            "CONTROL_BOT_TOKEN": "fake-legacy-token", "ADMIN_ID": "42",
            "SUPPLIER_QUIET_SECONDS": "4.0", "WORK_START_HOUR": "9", "WORK_END_HOUR": "21",
            "REQUEST_TEXT": "/prices", "REQUEST_TEXT_2": "/prices", "BUTTON_PATH_2": "",
            "CLOSED_TEXT_1": "Сегодня заказы завершены", "CLOSED_TEXT_2": "Прайс будет завтра",
        }, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.bot_token, "fake-legacy-token")
        self.assertNotIn("fake-legacy-token", repr(settings))
        self.assertEqual(settings.admin_ids, (42,))
        self.assertEqual(settings.quiet_seconds, 4.0)
        self.assertEqual((settings.open_hour, settings.close_hour), (9, 21))
        self.assertEqual([s.mode for s in settings.sources], ["bot", "bot"])
        self.assertEqual([s.request for s in settings.sources], ["/prices", "/prices"])
        self.assertEqual(settings.sources[1].buttons, ())
        self.assertEqual(settings.sources[1].closed_text, "Прайс будет завтра")

    def test_canonical_settings_take_priority_over_legacy_values(self):
        with patch.dict(os.environ, {
            "API_ID": "123", "API_HASH": "test",
            "BOT_TOKEN": "canonical", "CONTROL_BOT_TOKEN": "legacy",
            "ADMIN_IDS": "42,43", "ADMIN_ID": "99",
            "PRICE_SETTLE_SECONDS": "5", "SUPPLIER_QUIET_SECONDS": "invalid",
            "OPEN_HOUR": "10", "WORK_START_HOUR": "invalid",
            "CLOSE_HOUR": "20", "WORK_END_HOUR": "invalid",
        }, clear=True):
            settings = Settings.from_env(require_sync=False)
        self.assertEqual(settings.bot_token, "canonical")
        self.assertEqual(settings.admin_ids, (42, 43))
        self.assertEqual(settings.quiet_seconds, 5)
        self.assertEqual((settings.open_hour, settings.close_hour), (10, 20))

    def test_empty_canonical_value_does_not_mask_existing_token(self):
        with patch.dict(os.environ, {
            "API_ID": "123", "API_HASH": "test", "BOT_TOKEN": "  ",
            "CONTROL_BOT_TOKEN": " fake-legacy-token ", "ADMIN_IDS": "", "ADMIN_ID": "42",
        }, clear=True):
            settings = Settings.from_env(require_sync=False)
        self.assertEqual(settings.bot_token, "fake-legacy-token")
        self.assertEqual(settings.admin_ids, (42,))

    def test_explicit_feed_mode_never_changes_to_bot_due_to_request_text(self):
        with patch.dict(os.environ, {
            "API_ID": "123", "API_HASH": "test", "SUPPLIER_BOT_2": "@feed",
            "SOURCE_MODE_2": "feed", "REQUEST_TEXT_2": "/prices",
        }, clear=True):
            settings = Settings.from_env(require_sync=False)
        self.assertEqual(settings.sources[0].mode, "feed")

    def test_second_source_without_command_remains_feed(self):
        with patch.dict(os.environ, {
            "API_ID": "123", "API_HASH": "test", "SUPPLIER_BOT_2": "@feed",
        }, clear=True):
            settings = Settings.from_env(require_sync=False)
        self.assertEqual(settings.sources[0].mode, "feed")

    def test_button_path_also_selects_bot_when_mode_is_not_configured(self):
        with patch.dict(os.environ, {
            "API_ID": "123", "API_HASH": "test", "SUPPLIER_BOT_2": "@two",
            "BUTTON_PATH_2": "Прайс > Актуальный прайс",
        }, clear=True):
            settings = Settings.from_env(require_sync=False)
        self.assertEqual(settings.sources[0].mode, "bot")

    def test_control_configuration_can_start_without_supplier_settings(self):
        with patch.dict(os.environ, {"API_ID": "123", "API_HASH": "test"}, clear=True):
            settings = Settings.from_env(require_sync=False)
            self.assertEqual(settings.sources, ())
            with self.assertRaisesRegex(ValueError, "SESSION_STRING"):
                settings.validate()

    def test_chat_id_is_not_accepted_as_operator_id(self):
        with patch.dict(os.environ, {"API_ID": "123", "API_HASH": "test", "ADMIN_IDS": "-100123"}, clear=True):
            with self.assertRaisesRegex(ValueError, "ADMIN_IDS"):
                Settings.from_env(require_sync=False)

    def test_numeric_and_url_peer(self):
        self.assertEqual(peer("-1001234567890"), -1001234567890)
        self.assertEqual(peer("https://t.me/channel/"), "@channel")

    def test_volume_and_legacy_variable_compatibility(self):
        with patch.dict(os.environ, {
            "API_ID": "123", "API_HASH": "test", "SESSION_STRING": "test",
            "SUPPLIER_BOT": "@supplier", "TARGET_CHANNEL": "-100123",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data",
            "BUTTON_PATH": "Прайс > Актуальный прайс",
        }, clear=True):
            settings = Settings.from_env()
            self.assertEqual(settings.target, -100123)
            self.assertEqual(settings.sources[0].buttons, ("Прайс", "Актуальный прайс"))
            self.assertEqual(settings.state_file, "/data/state.json")

    def test_invalid_number_has_readable_error(self):
        with patch.dict(os.environ, {"API_ID": "oops"}, clear=True):
            with self.assertRaisesRegex(ValueError, "API_ID"):
                Settings.from_env()

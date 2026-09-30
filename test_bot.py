"""Offline tests: never contact Telegram or execute a real shutdown."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import requests

import bot


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(bot, "ADMIN_CHAT_ID", "123"))
        self.enterContext(patch.object(bot, "USER_CHAT_ID", "456"))
        self.send = self.enterContext(patch.object(bot, "send_telegram_message", return_value=True))
        self.shutdown = self.enterContext(patch.object(bot, "shutdown_server", return_value=True))
        self.state = bot.MonitorState()

    def test_initial_mains_is_silent(self):
        bot.process_reading(self.state, 1, 80)
        bot.process_reading(self.state, 1, 80)
        self.send.assert_not_called()
        self.shutdown.assert_not_called()

    def test_exact_transition_messages_once(self):
        for ac, battery in ((1, 80), (0, 79), (0, 79), (1, 78), (1, 78)):
            bot.process_reading(self.state, ac, battery)
        self.assertEqual(self.send.call_args_list, [
            call("⚡ Зникло живлення\nСервер працює від батареї\n🔋 Заряд: 79%"),
            call("⚡ Дома зникло світло", chat_id="456"),
            call("✅ Живлення відновлено\nСервер знову працює від мережі\n🔋 Заряд: 78%"),
            call("✅ Світло дома увімкнули", chat_id="456"),
        ])

    def test_thresholds_once_despite_fluctuations(self):
        for battery in (80, 31, 30, 30, 31, 29, 16, 15, 15, 16, 14, 8, 7, 7, 6):
            bot.process_reading(self.state, 0, battery)
        self.assertEqual(self.send.call_count, 3)
        self.assertTrue(all(not item.kwargs for item in self.send.call_args_list))
        self.shutdown.assert_called_once_with()

    def test_skipped_thresholds_and_low_start(self):
        for battery, expected in ((29, 30), (14, 15), (6, 7), (0, 7)):
            with self.subTest(battery=battery):
                self.send.reset_mock()
                state = bot.MonitorState()
                bot.process_reading(state, 0, battery)
                bot.process_reading(state, 0, battery)
                self.send.assert_called_once()
                self.assertIn(f"{expected}%", self.send.call_args.args[0])

    def test_recovery_resets_all_flags(self):
        bot.process_reading(self.state, 0, 7)
        bot.process_reading(self.state, 1, None)
        self.assertEqual(self.state.warned_thresholds, set())
        self.assertFalse(self.state.shutdown_requested)
        bot.process_reading(self.state, 0, 7)
        self.assertEqual(self.shutdown.call_count, 2)
        self.assertEqual(self.send.call_count, 6)

    def test_low_charge_on_mains_never_shuts_down(self):
        for battery in (30, 15, 7, 0):
            bot.process_reading(self.state, 1, battery)
        self.send.assert_not_called()
        self.shutdown.assert_not_called()

    def test_unknown_battery_still_reports_ac(self):
        bot.process_reading(self.state, 1, 80)
        bot.process_reading(self.state, 0, None)
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("невідомий", self.send.call_args_list[0].args[0])
        self.assertEqual(self.send.call_args, call("⚡ Дома зникло світло", chat_id="456"))
        self.shutdown.assert_not_called()

    def test_empty_user_chat_disables_short_notifications(self):
        with patch.object(bot, "USER_CHAT_ID", ""):
            for ac in (1, 0, 1):
                bot.process_reading(self.state, ac, 80)
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(all(not item.kwargs for item in self.send.call_args_list))

    def test_failed_admin_message_does_not_skip_user(self):
        self.send.side_effect = [False, True]
        bot.process_reading(self.state, 1, 80)
        bot.process_reading(self.state, 0, 80)
        self.assertEqual(self.send.call_args, call("⚡ Дома зникло світло", chat_id="456"))
        self.assertEqual(self.send.call_count, 2)

    def test_failed_user_message_does_not_block_battery_shutdown(self):
        self.send.side_effect = [True, False, True]
        bot.process_reading(self.state, 1, 80)
        bot.process_reading(self.state, 0, 7)
        self.assertEqual(self.send.call_count, 3)
        self.shutdown.assert_called_once()

    def test_failed_telegram_does_not_block_shutdown(self):
        self.send.return_value = False
        bot.process_reading(self.state, 0, 7)
        bot.process_reading(self.state, 0, 7)
        self.send.assert_called_once()
        self.shutdown.assert_called_once()

    def test_failed_shutdown_retries_without_message_duplicates(self):
        self.shutdown.side_effect = [False, True]
        for _ in range(3):
            bot.process_reading(self.state, 0, 7)
        self.assertEqual(self.shutdown.call_count, 2)
        self.send.assert_called_once()


class ReaderTests(unittest.TestCase):
    def test_files_and_bad_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capacity"
            for raw, expected in (("0\n", 0), ("100\n", 100), (" 57\n", 57),
                                  ("", None), ("oops", None), ("-1", None), ("101", None)):
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    self.assertEqual(bot.read_integer(path, 0, 100), expected)
            self.assertIsNone(bot.read_integer(Path(directory) / "missing", 0, 100))

    def test_permission_error(self):
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(bot.read_integer(Path("fake"), 0, 1))

    def test_real_readers_use_correct_paths_and_ranges(self):
        with patch.object(bot, "TEST_MODE", False), patch.object(bot, "read_integer", return_value=1) as read:
            self.assertEqual(bot.read_ac_status(), 1)
            read.assert_called_with(bot.AC_PATH, 0, 1)
            bot.read_battery_level()
            read.assert_called_with(bot.BATTERY_PATH, 0, 100)

    def test_simulation_never_reads_sysfs_and_holds_last_state(self):
        with patch.object(bot, "TEST_MODE", True), patch.object(Path, "read_text", side_effect=AssertionError):
            for step in range(len(bot.TEST_SCENARIO) + 3):
                with patch.object(bot, "test_step", step):
                    self.assertEqual((bot.read_ac_status(), bot.read_battery_level()),
                                     bot.TEST_SCENARIO[min(step, len(bot.TEST_SCENARIO) - 1)])


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(bot, "BOT_TOKEN", "fake-secret"))
        self.enterContext(patch.object(bot, "ADMIN_CHAT_ID", "123"))
        self.post = self.enterContext(patch.object(bot.requests, "post"))

    def test_success_has_timeout_and_payload(self):
        self.post.return_value.json.return_value = {"ok": True}
        self.assertTrue(bot.send_telegram_message("hello"))
        self.post.assert_called_once_with(
            "https://api.telegram.org/botfake-secret/sendMessage",
            json={"chat_id": "123", "text": "hello"}, timeout=(5, 10))

    def test_explicit_user_recipient(self):
        self.post.return_value.json.return_value = {"ok": True}
        self.assertTrue(bot.send_telegram_message("short message", chat_id="456"))
        self.assertEqual(self.post.call_args.kwargs["json"],
                         {"chat_id": "456", "text": "short message"})

    def test_empty_explicit_recipient_does_not_fall_back_to_admin(self):
        with patch.object(bot, "TEST_MODE", False):
            self.assertFalse(bot.send_telegram_message("short message", chat_id=""))
        self.post.assert_not_called()

    def test_network_errors_never_leak_token(self):
        for error in (requests.ConnectionError, requests.Timeout, requests.HTTPError):
            with self.subTest(error=error):
                self.post.side_effect = error("https://api.telegram.org/botfake-secret/sendMessage")
                with self.assertLogs(bot.logger, level="ERROR") as logs:
                    self.assertFalse(bot.send_telegram_message("hello"))
                self.assertNotIn("fake-secret", "".join(logs.output))

    def test_http_failure(self):
        self.post.return_value.raise_for_status.side_effect = requests.HTTPError("403")
        self.assertFalse(bot.send_telegram_message("hello"))

    def test_invalid_api_responses(self):
        for payload in ({"ok": False}, {}, [], None, {"ok": "true"}):
            with self.subTest(payload=payload):
                self.post.return_value.json.return_value = payload
                self.assertFalse(bot.send_telegram_message("hello"))
        self.post.return_value.json.side_effect = ValueError("bad JSON")
        self.assertFalse(bot.send_telegram_message("hello"))

    def test_empty_credentials_dry_run_and_production(self):
        with patch.object(bot, "BOT_TOKEN", ""):
            with patch.object(bot, "TEST_MODE", True):
                self.assertTrue(bot.send_telegram_message("hello"))
            with patch.object(bot, "TEST_MODE", False):
                self.assertFalse(bot.send_telegram_message("hello"))
        self.post.assert_not_called()


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(bot, "TEST_MODE", False))
        self.ac = self.enterContext(patch.object(bot, "read_ac_status", return_value=0))
        self.battery = self.enterContext(patch.object(bot, "read_battery_level", return_value=7))
        self.run = self.enterContext(patch.object(bot.subprocess, "run"))

    def test_dry_run_never_reads_or_executes(self):
        with patch.object(bot, "TEST_MODE", True):
            self.assertTrue(bot.shutdown_server())
        self.ac.assert_not_called()
        self.battery.assert_not_called()
        self.run.assert_not_called()

    def test_command_noninteractive_and_bounded(self):
        self.assertTrue(bot.shutdown_server())
        self.run.assert_called_once_with(["sudo", "-n", "shutdown", "-h", "now"],
                                         check=True, timeout=15)

    def test_recheck_prevents_shutdown(self):
        for ac, battery in ((1, 7), (None, 7), (0, None), (0, 8)):
            with self.subTest(ac=ac, battery=battery):
                self.ac.return_value, self.battery.return_value = ac, battery
                self.assertFalse(bot.shutdown_server())
        self.run.assert_not_called()

    def test_command_failures(self):
        for error in (FileNotFoundError(), subprocess.CalledProcessError(1, "sudo"),
                      subprocess.TimeoutExpired("sudo", 15)):
            with self.subTest(error=error):
                self.run.side_effect = error
                self.assertFalse(bot.shutdown_server())


class MainTests(unittest.TestCase):
    def test_full_simulation_without_side_effects(self):
        with patch.object(bot, "TEST_MODE", True), patch.object(bot, "test_step", 0), \
                patch.object(bot, "BOT_TOKEN", ""), patch.object(bot, "ADMIN_CHAT_ID", ""), \
                patch.object(bot, "USER_CHAT_ID", "456"), \
                patch.object(bot.time, "sleep", side_effect=[None] * 9 + [KeyboardInterrupt]) as sleep, \
                patch.object(bot.requests, "post") as post, patch.object(bot.subprocess, "run") as run:
            bot.main()
            self.assertEqual(sleep.call_count, 10)
            sleep.assert_called_with(10)
            post.assert_not_called()
            run.assert_not_called()

    def test_unknown_ac_skipped_then_recovers(self):
        with patch.object(bot, "TEST_MODE", True), \
                patch.object(bot, "read_ac_status", side_effect=[None, 1]), \
                patch.object(bot, "read_battery_level", return_value=50), \
                patch.object(bot.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                patch.object(bot, "process_reading") as process:
            bot.main()
            process.assert_called_once()
            self.assertEqual(process.call_args.args[1:], (1, 50))

    def test_missing_credentials_exits_with_failure(self):
        with patch.object(bot, "TEST_MODE", False), patch.object(bot, "BOT_TOKEN", ""), \
                self.assertRaises(SystemExit) as error:
            bot.main()
        self.assertEqual(error.exception.code, 1)

    def test_env_loaded_next_to_script_from_another_directory(self):
        # Use a copied module so the user's .env is never read or changed.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(bot.__file__).read_text(encoding="utf-8")
            (root / "bot.py").write_text(source, encoding="utf-8")
            (root / ".env").write_text(
                "TEST_MODE=True\nBOT_TOKEN=\nADMIN_CHAT_ID=123\nUSER_CHAT_ID=456\nCHAT_ID=789\n",
                encoding="utf-8")
            env = dict(os.environ)
            for key in ("TEST_MODE", "BOT_TOKEN", "CHAT_ID", "ADMIN_CHAT_ID", "USER_CHAT_ID",
                        "PYTHON_DOTENV_DISABLED"):
                env.pop(key, None)
            env["PYTHONPATH"] = str(root)
            import sys
            result = subprocess.run([sys.executable, "-c",
                                     "import bot; assert bot.TEST_MODE; "
                                     "assert bot.ADMIN_CHAT_ID == '123'; assert bot.USER_CHAT_ID == '456'"],
                                    cwd=root.parent, env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            (root / ".env").write_text("TEST_MODE=True\nCHAT_ID=789\n", encoding="utf-8")
            result = subprocess.run([sys.executable, "-c",
                                     "import bot; assert bot.ADMIN_CHAT_ID == '789'; "
                                     "assert bot.USER_CHAT_ID == ''"],
                                    cwd=root.parent, env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()

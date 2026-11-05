"""Offline tests: never contact Telegram or execute a real shutdown."""

import json
import os
import subprocess
import tempfile
import threading
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
        self.clock = self.enterContext(patch.object(bot.time, "monotonic", return_value=100.0))
        self.state = bot.MonitorState()

    def test_initial_mains_is_silent(self):
        bot.process_reading(self.state, 1, 80)
        bot.process_reading(self.state, 1, 80)
        self.send.assert_not_called()
        self.shutdown.assert_not_called()

    def test_exact_transition_messages_once(self):
        self.clock.side_effect = [0, 100, 150, 3823, 3900]
        for ac, battery in ((1, 90), (0, 89), (0, 89), (1, 88), (1, 88)):
            bot.process_reading(self.state, ac, battery)
        self.assertEqual(self.send.call_args_list, [
            call("⚡ Зникло живлення\nСервер працює від батареї\n🔋 Заряд: 89%"),
            call("⚡ Дома зникло світло", chat_id="456"),
            call("✅ Живлення відновлено\nСервер знову працює від мережі\n🔋 Заряд: 88%\n⏱ Без живлення: 1 год 2 хв 3 с"),
            call("✅ Світло дома увімкнули\n⏱ Без живлення: 1 год 2 хв 3 с", chat_id="456"),
        ])

    def test_thresholds_once_despite_fluctuations(self):
        for battery in (81, 80, 80, 81, 79, 61, 60, 60, 61, 59, 41, 40, 40, 41, 39,
                        31, 30, 30, 31, 29, 16, 15, 15, 16, 14, 8, 7, 6, 5, 5, 4):
            bot.process_reading(self.state, 0, battery)
        self.assertEqual(self.send.call_count, 6)
        self.assertTrue(all(not item.kwargs for item in self.send.call_args_list))
        self.shutdown.assert_called_once_with()

    def test_skipped_thresholds_and_low_start(self):
        for battery, expected in ((79, 80), (59, 60), (39, 40), (29, 30), (14, 15), (6, 15), (4, 5), (0, 5)):
            with self.subTest(battery=battery):
                self.send.reset_mock()
                state = bot.MonitorState()
                bot.process_reading(state, 0, battery)
                bot.process_reading(state, 0, battery)
                self.send.assert_called_once()
                self.assertIn(f"{expected}%", self.send.call_args.args[0])

    def test_recovery_resets_all_flags(self):
        bot.process_reading(self.state, 0, 5)
        bot.process_reading(self.state, 1, None)
        self.assertEqual(self.state.warned_thresholds, set())
        self.assertFalse(self.state.shutdown_requested)
        bot.process_reading(self.state, 0, 5)
        self.assertEqual(self.shutdown.call_count, 2)
        self.assertEqual(self.send.call_count, 6)

    def test_low_charge_on_mains_never_shuts_down(self):
        for battery in (80, 60, 40, 30, 15, 7, 5, 0):
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
                bot.process_reading(self.state, ac, 90)
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(all(not item.kwargs for item in self.send.call_args_list))

    def test_failed_admin_message_does_not_skip_user(self):
        self.send.side_effect = [False, True]
        bot.process_reading(self.state, 1, 90)
        bot.process_reading(self.state, 0, 90)
        self.assertEqual(self.send.call_args, call("⚡ Дома зникло світло", chat_id="456"))
        self.assertEqual(self.send.call_count, 2)

    def test_failed_user_message_does_not_block_battery_shutdown(self):
        self.send.side_effect = [True, False, True]
        bot.process_reading(self.state, 1, 80)
        bot.process_reading(self.state, 0, 5)
        self.assertEqual(self.send.call_count, 3)
        self.shutdown.assert_called_once()

    def test_failed_telegram_does_not_block_shutdown(self):
        self.send.return_value = False
        bot.process_reading(self.state, 0, 5)
        bot.process_reading(self.state, 0, 5)
        self.send.assert_called_once()
        self.shutdown.assert_called_once()

    def test_failed_shutdown_retries_without_message_duplicates(self):
        self.shutdown.side_effect = [False, True]
        for _ in range(3):
            bot.process_reading(self.state, 0, 5)
        self.assertEqual(self.shutdown.call_count, 2)
        self.send.assert_called_once()

    def test_seven_and_six_percent_do_not_shutdown(self):
        for battery in (7, 6):
            bot.process_reading(self.state, 0, battery)
        self.shutdown.assert_not_called()
        bot.process_reading(self.state, 0, 5)
        self.shutdown.assert_called_once()

    def test_unknown_battery_does_not_shutdown_at_low_charge(self):
        bot.process_reading(self.state, 0, None)
        self.shutdown.assert_not_called()

    def test_start_on_battery_reports_minimum_known_duration(self):
        self.clock.side_effect = [100, 160]
        bot.process_reading(self.state, 0, 90)
        bot.process_reading(self.state, 1, 90)
        self.assertEqual(self.send.call_count, 2)
        for item in self.send.call_args_list:
            self.assertIn("щонайменше 0 год 1 хв 0 с (від запуску моніторингу)", item.args[0])

    def test_next_outage_has_its_own_timer_and_thresholds(self):
        self.clock.side_effect = [0, 10, 70, 100, 130]
        for ac in (1, 0, 1, 0, 1):
            bot.process_reading(self.state, ac, 60)
        recovery = [item.args[0] for item in self.send.call_args_list if item.args[0].startswith("✅")]
        self.assertEqual(len(recovery), 4)
        self.assertIn("0 год 1 хв 0 с", recovery[0])
        self.assertIn("0 год 0 хв 30 с", recovery[2])
        warnings = [item for item in self.send.call_args_list if item.args[0].startswith("🔋")]
        self.assertEqual(len(warnings), 2)
        self.assertIsNone(self.state.outage_started_at)

    def test_missing_battery_and_failed_notifications_keep_timer(self):
        self.clock.side_effect = [0, 100, 120, 190]
        self.send.return_value = False
        for ac, battery in ((1, 90), (0, None), (0, None), (1, None)):
            bot.process_reading(self.state, ac, battery)
        self.assertEqual(self.send.call_count, 4)
        self.assertIn("0 год 1 хв 30 с", self.send.call_args.args[0])


class DurationTests(unittest.TestCase):
    def test_duration_format(self):
        for seconds, expected in ((0, "0 год 0 хв 0 с"), (59.9, "0 год 0 хв 59 с"),
                                  (60, "0 год 1 хв 0 с"), (3600, "1 год 0 хв 0 с"),
                                  (90061, "25 год 1 хв 1 с"), (-1, "0 год 0 хв 0 с")):
            with self.subTest(seconds=seconds):
                self.assertEqual(bot.format_duration(seconds), expected)


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
            json={"chat_id": "123", "text": "hello", "reply_markup": bot.STATUS_KEYBOARD},
            timeout=(5, 10))

    def test_explicit_user_recipient(self):
        self.post.return_value.json.return_value = {"ok": True}
        self.assertTrue(bot.send_telegram_message("short message", chat_id="456"))
        self.assertEqual(self.post.call_args.kwargs["json"],
                         {"chat_id": "456", "text": "short message", "reply_markup": bot.STATUS_KEYBOARD})

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
        self.battery = self.enterContext(patch.object(bot, "read_battery_level", return_value=5))
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
        for ac, battery in ((1, 5), (None, 5), (0, None), (0, 6), (0, 7)):
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


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "state.json"
        self.enterContext(patch.object(bot, "ADMIN_CHAT_ID", "123"))
        self.enterContext(patch.object(bot, "USER_CHAT_ID", "456"))
        self.boot = self.enterContext(patch.object(bot, "read_boot_id", return_value="boot-a"))
        self.clock = self.enterContext(patch.object(bot.time, "monotonic", return_value=100.0))
        self.wall = self.enterContext(patch.object(bot.time, "time", return_value=1000.0))
        self.send = self.enterContext(patch.object(bot, "send_telegram_message", return_value=True))
        self.shutdown = self.enterContext(patch.object(bot, "shutdown_server", return_value=True))

    def start_outage(self, battery=90):
        state = bot.load_state(self.path)
        bot.process_reading(state, 1, 90)
        bot.retry_startup_messages(state)
        bot.process_reading(state, 0, battery)
        self.send.reset_mock()
        return state

    def test_outage_is_on_disk_before_shutdown(self):
        state = self.start_outage()

        def verify_saved():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["outage_started_unix"], 1000)
            self.assertIn(5, saved["warned_thresholds"])
            return True

        self.shutdown.side_effect = verify_saved
        bot.process_reading(state, 0, 5)
        self.shutdown.assert_called_once()
        self.assertFalse(self.path.with_name("state.json.tmp").exists())

    def test_reboot_recovers_duration_and_notifies_both_chats_once(self):
        self.start_outage(5)
        self.boot.return_value = "boot-b"
        self.wall.return_value = 4600.0
        self.clock.return_value = 3.0  # Монотонний годинник після reboot почався заново.
        state = bot.load_state(self.path)
        bot.process_reading(state, 1, 8)
        bot.retry_startup_messages(state)
        self.assertEqual(self.send.call_count, 2)
        admin, user = self.send.call_args_list
        self.assertIn("Сервер увімкнувся", admin.args[0])
        self.assertIn("Живлення відновлено", admin.args[0])
        self.assertIn("приблизно 1 год 0 хв 0 с", admin.args[0])
        self.assertIn("до запуску моніторингу", user.args[0])
        self.assertNotIn("Заряд", user.args[0])
        self.assertEqual(admin.kwargs["chat_id"], "123")
        self.assertEqual(user.kwargs["chat_id"], "456")
        bot.process_reading(state, 1, 9)
        bot.retry_startup_messages(state)
        self.assertEqual(self.send.call_count, 2)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsNone(saved["outage_started_unix"])
        self.assertEqual(saved["warned_thresholds"], [])
        self.assertEqual(saved["pending_startup_messages"], [])

    def test_service_restart_is_not_reported_as_server_boot(self):
        self.start_outage()
        state = bot.load_state(self.path)
        bot.process_reading(state, 0, 90)
        bot.retry_startup_messages(state)
        self.assertIn("Моніторинг запущено", self.send.call_args_list[0].args[0])
        self.assertNotIn("Сервер увімкнувся", self.send.call_args_list[0].args[0])
        self.assertNotIn("Живлення відновлено", self.send.call_args_list[0].args[0])

    def test_start_without_saved_outage_does_not_invent_recovery(self):
        state = bot.load_state(self.path)
        bot.process_reading(state, 1, 80)
        bot.retry_startup_messages(state)
        self.assertEqual(self.send.call_count, 2)
        self.assertFalse(any("відновлено" in c.args[0] for c in self.send.call_args_list))

    def test_thresholds_and_duration_survive_service_restart_on_battery(self):
        self.start_outage(30)
        self.wall.return_value = 1100.0
        state = bot.load_state(self.path)
        bot.process_reading(state, 0, 29)
        bot.retry_startup_messages(state)
        self.send.reset_mock()
        self.wall.return_value = 1180.0
        self.clock.return_value = 180.0
        bot.process_reading(state, 1, 29)
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("приблизно 0 год 3 хв 0 с", self.send.call_args.args[0])

    def test_restored_running_timer_uses_monotonic_clock_after_first_reading(self):
        self.start_outage()
        self.wall.return_value = 1100.0
        state = bot.load_state(self.path)
        bot.process_reading(state, 0, 90)
        bot.retry_startup_messages(state)
        self.send.reset_mock()
        self.wall.return_value = 9999999.0
        self.clock.return_value = 160.0
        bot.process_reading(state, 1, 90)
        self.assertIn("приблизно 0 год 2 хв 40 с", self.send.call_args.args[0])

    def test_failed_shutdown_can_retry_after_service_restart(self):
        state = self.start_outage(5)
        self.shutdown.reset_mock()
        state = bot.load_state(self.path)
        bot.process_reading(state, 0, 5)
        self.shutdown.assert_called_once()
        # Збережений поріг не створює повторного повідомлення про 5%.
        self.send.assert_not_called()

    def test_failed_startup_messages_survive_restart_and_retry(self):
        state = self.start_outage()
        self.boot.return_value = "boot-b"
        state = bot.load_state(self.path)
        bot.process_reading(state, 1, 8)
        self.send.side_effect = [True, False]
        bot.retry_startup_messages(state)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["pending_startup_messages"]), 1)
        self.assertEqual(saved["pending_startup_messages"][0]["chat_id"], "456")
        self.send.reset_mock(side_effect=True)
        state = bot.load_state(self.path)
        bot.process_reading(state, 1, 10)
        bot.retry_startup_messages(state)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.kwargs["chat_id"], "456")
        self.assertEqual(state.pending_startup_messages, [])

    def test_outage_unknown_start_remains_minimum_after_restart(self):
        state = bot.load_state(self.path)
        bot.process_reading(state, 0, 90)
        bot.retry_startup_messages(state)
        self.wall.return_value = 1060.0
        state = bot.load_state(self.path)
        bot.process_reading(state, 1, 90)
        self.assertIn("щонайменше 0 год 1 хв 0 с", state.pending_startup_messages[0]["text"])

    def test_older_unsent_startup_does_not_hide_recovery(self):
        state = bot.load_state(self.path)
        bot.process_reading(state, 0, 90)
        self.wall.return_value = 1060.0
        state = bot.load_state(self.path)
        bot.process_reading(state, 1, 90)
        bot.retry_startup_messages(state)
        recovered = [item for item in self.send.call_args_list if "до запуску моніторингу" in item.args[0]]
        self.assertEqual(len(recovered), 2)

    def test_corrupted_state_does_not_crash_monitor(self):
        for raw in ("bad JSON", "[]", '{"version": 2}',
                    '{"version": 1, "outage_started_unix": "yesterday"}',
                    '{"version": 1, "outage_started_unix": NaN}',
                    '{"version": 1, "warned_thresholds": [true]}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                with self.assertLogs(bot.logger, level="ERROR"):
                    state = bot.load_state(self.path)
                self.assertIsNone(state.outage_started_unix)
                bot.process_reading(state, 1, 90)

    def test_failed_save_does_not_block_shutdown(self):
        state = self.start_outage()
        with patch.object(bot.os, "replace", side_effect=PermissionError):
            with self.assertLogs(bot.logger, level="ERROR"):
                bot.process_reading(state, 0, 5)
        self.shutdown.assert_called_once()

    def test_clock_moving_backwards_is_reported_as_unknown(self):
        self.start_outage()
        state = bot.load_state(self.path)
        self.wall.return_value = 900.0
        bot.process_reading(state, 1, 90)
        self.assertIn("змінився системний час", state.pending_startup_messages[0]["text"])

    def test_same_chat_receives_single_startup_message(self):
        with patch.object(bot, "USER_CHAT_ID", "123"):
            state = bot.load_state(self.path)
            bot.process_reading(state, 1, 90)
            bot.retry_startup_messages(state)
        self.send.assert_called_once()


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(bot, "ADMIN_CHAT_ID", "123"))
        self.enterContext(patch.object(bot, "USER_CHAT_ID", "456"))
        self.enterContext(patch.object(bot, "TEST_MODE", False))
        self.enterContext(patch.object(bot, "read_ac_status", return_value=0))
        self.enterContext(patch.object(bot, "read_battery_level", return_value=62))
        self.enterContext(patch.object(Path, "read_text", return_value="3661.5 999.0"))
        self.send = self.enterContext(patch.object(bot, "send_telegram_message", return_value=True))
        self.api = self.enterContext(patch.object(bot, "telegram_request", return_value={"ok": True}))

    def test_button_returns_fresh_charge_power_and_uptime_to_user(self):
        bot.handle_telegram_update({"callback_query": {
            "id": "cb-1", "data": "battery", "message": {"chat": {"id": 456}},
        }})
        self.api.assert_called_once_with("answerCallbackQuery", {"callback_query_id": "cb-1"})
        self.send.assert_called_once_with(
            "🔋 Поточний заряд: 62%\n⚡ Живлення від батареї\n🖥 Сервер працює: 1 год 1 хв 1 с",
            chat_id="456",
        )

    def test_commands_for_both_configured_chats(self):
        for chat in (123, 456):
            for text in ("/battery", "/status", "/battery@my_power_bot"):
                with self.subTest(chat=chat, text=text):
                    self.send.reset_mock()
                    bot.handle_telegram_update({"message": {"chat": {"id": chat}, "text": text}})
                    self.send.assert_called_once()
                    self.assertEqual(self.send.call_args.kwargs["chat_id"], str(chat))

    def test_start_shows_button_instructions(self):
        bot.handle_telegram_update({"message": {"chat": {"id": 123}, "text": "/start"}})
        self.assertIn("Поточний заряд", self.send.call_args.args[0])

    def test_unconfigured_chats_and_forwarded_buttons_are_ignored(self):
        for chat in (999, "", None):
            bot.handle_telegram_update({"message": {"chat": {"id": chat}, "text": "/battery"}})
            bot.handle_telegram_update({"callback_query": {
                "id": "cb", "data": "battery", "message": {"chat": {"id": chat}},
            }})
        self.send.assert_not_called()
        self.api.assert_not_called()

    def test_unknown_and_malformed_updates_are_ignored(self):
        for update in ({}, {"message": None}, {"message": {"chat": None}},
                       {"callback_query": {}}, {"callback_query": {"message": {}}},
                       {"message": {"chat": {"id": 123}, "text": None}},
                       {"message": {"chat": {"id": 123}, "text": "   "}},
                       {"message": {"chat": {"id": 123}, "text": "/shutdown"}}):
            bot.handle_telegram_update(update)
        self.send.assert_not_called()

    def test_unknown_sensors_are_reported_without_crashing(self):
        with patch.object(bot, "read_ac_status", return_value=None), \
                patch.object(bot, "read_battery_level", return_value=None), \
                patch.object(Path, "read_text", side_effect=OSError):
            message = bot.current_status()
        self.assertIn("заряд: невідомий", message)
        self.assertIn("Стан живлення невідомий", message)
        self.assertNotIn("Сервер працює:", message)

    def test_polling_confirms_processed_updates_with_next_offset(self):
        stop = threading.Event()
        replies = iter([
            {"ok": True, "result": [{"update_id": 40, "message": {"chat": {"id": 123}, "text": "/battery"}}]},
            {"ok": True, "result": []},
        ])

        def receive(method, payload, timeout):
            response = next(replies)
            if "offset" in payload:
                self.assertEqual(payload["offset"], 41)
                stop.set()
            self.assertEqual(method, "getUpdates")
            self.assertGreater(timeout[1], payload["timeout"])
            return response

        self.api.side_effect = receive
        bot.poll_telegram_updates(stop)
        self.assertEqual(self.api.call_count, 2)
        self.send.assert_called_once()

    def test_polling_backs_off_on_network_failure(self):
        stop = threading.Event()
        self.api.return_value = None
        with patch.object(stop, "wait", side_effect=lambda _: stop.set()) as wait:
            bot.poll_telegram_updates(stop)
        wait.assert_called_once_with(5)

    def test_listener_uses_separate_daemon_thread(self):
        stop = threading.Event()
        with patch.object(bot, "BOT_TOKEN", "fake-secret"), patch.object(bot.threading, "Thread") as thread:
            bot.start_command_listener(stop)
        self.assertTrue(thread.call_args.kwargs["daemon"])
        self.assertEqual(thread.call_args.kwargs["target"], bot.poll_telegram_updates)
        thread.return_value.start.assert_called_once()


class MainTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(bot, "BOT_TOKEN", ""))
        self.enterContext(patch.object(bot, "start_command_listener", return_value=None))

    def test_production_startup_loads_outage_and_sends_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = bot.MonitorState(previous_ac=0, outage_started_unix=1000.0,
                                     outage_start_observed=True, boot_id="old-boot", state_path=path)
            bot.save_state(state)
            with patch.object(bot, "STATE_FILE", path), patch.object(bot, "TEST_MODE", False), \
                    patch.object(bot, "BOT_TOKEN", "fake-secret"), \
                    patch.object(bot, "ADMIN_CHAT_ID", "123"), patch.object(bot, "USER_CHAT_ID", "456"), \
                    patch.object(bot, "read_boot_id", return_value="new-boot"), \
                    patch.object(bot.time, "time", return_value=4600.0), \
                    patch.object(bot, "read_ac_status", return_value=1), \
                    patch.object(bot, "read_battery_level", return_value=8), \
                    patch.object(bot.time, "sleep", side_effect=KeyboardInterrupt), \
                    patch.object(bot, "send_telegram_message", return_value=True) as send, \
                    patch.object(bot.subprocess, "run") as run:
                bot.main()
                run.assert_not_called()
                self.assertEqual(send.call_count, 2)
                self.assertIn("Сервер увімкнувся", send.call_args_list[0].args[0])
                self.assertIn("1 год 0 хв 0 с", send.call_args_list[0].args[0])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(saved["outage_started_unix"])
            self.assertEqual(saved["pending_startup_messages"], [])

    def test_full_simulation_without_side_effects(self):
        with patch.object(bot, "TEST_MODE", True), patch.object(bot, "test_step", 0), \
                patch.object(bot, "BOT_TOKEN", ""), patch.object(bot, "ADMIN_CHAT_ID", ""), \
                patch.object(bot, "USER_CHAT_ID", "456"), \
                patch.object(bot.time, "sleep", side_effect=[None] * (len(bot.TEST_SCENARIO) - 1) + [KeyboardInterrupt]) as sleep, \
                patch.object(bot.requests, "post") as post, patch.object(bot.subprocess, "run") as run:
            bot.main()
            self.assertEqual(sleep.call_count, len(bot.TEST_SCENARIO))
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

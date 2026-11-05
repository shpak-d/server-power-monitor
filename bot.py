"""Моніторинг живлення Linux-сервера та запити стану через Telegram."""

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# CHAT_ID залишається запасним варіантом для старого .env.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip() or os.getenv("CHAT_ID", "").strip()
USER_CHAT_ID = os.getenv("USER_CHAT_ID", "").strip()
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() == "true"
CHECK_INTERVAL = 10
CRITICAL_WARNING_THRESHOLD = 5
AC_PATH = Path("/sys/class/power_supply/AC/online")
BATTERY_PATH = Path("/sys/class/power_supply/BAT0/capacity")
STATE_FILE = Path(__file__).with_name("monitor-state.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
UPTIME_PATH = Path("/proc/uptime")
STATUS_KEYBOARD = {"inline_keyboard": [[
    {"text": "🔋 Поточний заряд", "callback_data": "battery"},
]]}
logger = logging.getLogger(__name__)

# Кожен елемент використовується на одній ітерації (кожні 10 секунд).
# Після останнього елемента зберігається останній стан.
TEST_SCENARIO = [
    (1, 90), (0, 89), (0, 80), (0, 60), (0, 40), (0, 30), (0, 30), (0, 15), (0, 7),
    (0, 5), (0, 5), (1, 8), (0, 30), (1, 31),
]
test_step = 0


def read_integer(path: Path, minimum: int, maximum: int) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
        if not minimum <= value <= maximum:
            raise ValueError("значення поза допустимим діапазоном")
        return value
    except (OSError, ValueError) as error:
        logger.error("Не вдалося прочитати %s: %s", path, error)
        return None


def read_ac_status() -> int | None:
    if TEST_MODE:
        return TEST_SCENARIO[min(test_step, len(TEST_SCENARIO) - 1)][0]
    return read_integer(AC_PATH, 0, 1)


def read_battery_level() -> int | None:
    if TEST_MODE:
        return TEST_SCENARIO[min(test_step, len(TEST_SCENARIO) - 1)][1]
    return read_integer(BATTERY_PATH, 0, 100)


def telegram_request(method: str, payload: dict, timeout: tuple = (5, 10)) -> dict | None:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload, timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            logger.error("Telegram API не підтвердив %s", method)
            return None
        return result
    except (requests.RequestException, ValueError) as error:
        # Текст винятку requests може містити URL із токеном бота.
        logger.error("Помилка Telegram API %s (%s)", method, type(error).__name__)
        return None


def send_telegram_message(message: str, chat_id: str | None = None) -> bool:
    if chat_id is None:
        chat_id = ADMIN_CHAT_ID
    if not BOT_TOKEN or not chat_id:
        if TEST_MODE:
            logger.info("TEST_MODE, повідомлення без надсилання:\n%s", message)
            return True
        logger.error("BOT_TOKEN або chat_id отримувача не налаштовано")
        return False

    if telegram_request("sendMessage", {
        "chat_id": chat_id, "text": message, "reply_markup": STATUS_KEYBOARD,
    }) is None:
        return False
    logger.info("Telegram-повідомлення надіслано")
    return True


@dataclass
class MonitorState:
    previous_ac: int | None = None
    warned_thresholds: set[int] = field(default_factory=set)
    outage_started_at: float | None = None
    outage_start_observed: bool = False
    outage_started_unix: float | None = None
    outage_restored: bool = False
    boot_id: str | None = None
    state_path: Path | None = None
    startup_pending: bool = False
    server_rebooted: bool = False
    pending_startup_messages: list[dict] = field(default_factory=list)


def read_boot_id() -> str | None:
    try:
        return BOOT_ID_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def load_state(path: Path) -> MonitorState:
    state = MonitorState(state_path=path, startup_pending=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("невідомий формат стану")
        previous_ac = data.get("previous_ac")
        if previous_ac is not None and (type(previous_ac) is not int or previous_ac not in (0, 1)):
            raise ValueError("некоректний стан AC")
        started = data.get("outage_started_unix")
        if started is not None and (type(started) not in (int, float)
                                    or not math.isfinite(started) or started <= 0):
            raise ValueError("некоректний час відключення")
        thresholds = data.get("warned_thresholds", [])
        if not isinstance(thresholds, list) or any(
            type(t) is not int or t not in (5, 15, 30, 40, 60, 80) for t in thresholds
        ):
            raise ValueError("некоректні пороги")
        pending = data.get("pending_startup_messages", [])
        if not isinstance(pending, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("chat_id"), str)
            or not isinstance(item.get("text"), str) for item in pending
        ):
            raise ValueError("некоректні повідомлення")
        state.previous_ac = previous_ac
        state.outage_started_unix = started
        state.outage_restored = started is not None
        state.outage_start_observed = data.get("outage_start_observed") is True
        state.warned_thresholds = set(thresholds) if started is not None else set()
        state.boot_id = data.get("boot_id") if isinstance(data.get("boot_id"), str) else None
        state.pending_startup_messages = pending
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        logger.error("Не вдалося відновити стан (%s); починаємо нове спостереження",
                     type(error).__name__)
    current_boot = read_boot_id()
    state.server_rebooted = bool(current_boot and state.boot_id and current_boot != state.boot_id)
    state.boot_id = current_boot
    return state


def save_state(state: MonitorState) -> None:
    if state.state_path is None:
        return
    data = {
        "version": 1,
        "previous_ac": state.previous_ac,
        "outage_started_unix": state.outage_started_unix,
        "outage_start_observed": state.outage_start_observed,
        "warned_thresholds": sorted(state.warned_thresholds),
        "boot_id": state.boot_id,
        "pending_startup_messages": state.pending_startup_messages,
    }
    temporary = state.state_path.with_name(state.state_path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state.state_path)
    except OSError as error:
        logger.error("Не вдалося зберегти стан відключення (%s)", type(error).__name__)


def retry_startup_messages(state: MonitorState) -> None:
    remaining = []
    allowed_chats = {ADMIN_CHAT_ID, USER_CHAT_ID} - {""}
    for item in state.pending_startup_messages:
        if item["chat_id"] in allowed_chats and not send_telegram_message(
            item["text"], chat_id=item["chat_id"]
        ):
            remaining.append(item)
    if remaining != state.pending_startup_messages:
        state.pending_startup_messages = remaining
        save_state(state)


def format_duration(seconds: float) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} год {minutes} хв {seconds} с"


def outage_info(state: MonitorState, observed_at: float, on_startup: bool = False) -> str:
    if state.outage_started_at is not None:
        elapsed = observed_at - state.outage_started_at
    elif state.outage_started_unix is not None:
        elapsed = time.time() - state.outage_started_unix
    else:
        return "⏱ Без живлення: невідомо"
    if elapsed < 0:
        return "⏱ Тривалість невідома: змінився системний час"
    duration = format_duration(elapsed)
    if not state.outage_start_observed:
        duration = f"щонайменше {duration} (від запуску моніторингу)"
    if on_startup:
        return f"⏱ Від зникнення живлення до запуску моніторингу: приблизно {duration}"
    if state.outage_restored:
        duration = f"приблизно {duration}"
    return f"⏱ Без живлення: {duration}"


def queue_startup_messages(state: MonitorState, ac: int, charge: str, observed_at: float) -> None:
    title = "🟢 Сервер увімкнувся" if state.server_rebooted else "🟢 Моніторинг запущено"
    power = "🔌 Сервер працює від мережі" if ac == 1 else "⚡ Сервер працює від батареї"
    recovery = ac == 1 and state.outage_started_unix is not None
    admin = f"{title}\n{power}\n🔋 Заряд: {charge}"
    user = f"{title}\n" + ("🔌 Вдома є світло" if ac == 1 else "⚡ Вдома немає світла")
    if recovery:
        info = outage_info(state, observed_at, on_startup=True)
        admin += f"\n✅ Живлення відновлено\n{info}"
        user = f"✅ Світло дома увімкнули\n🟢 Сервер працює\n{info}"
    # Зберігаємо невдалі повідомлення, щоб відправити після появи інтернету.
    if not state.pending_startup_messages or state.server_rebooted or recovery:
        if ADMIN_CHAT_ID:
            state.pending_startup_messages.append({"chat_id": ADMIN_CHAT_ID, "text": admin})
        if USER_CHAT_ID and USER_CHAT_ID != ADMIN_CHAT_ID:
            state.pending_startup_messages.append({"chat_id": USER_CHAT_ID, "text": user})


def process_reading(state: MonitorState, ac: int, battery: int | None) -> None:
    """Обробити один вимір; стан зберігається між ітераціями."""
    # Монотонний годинник не залежить від переведення системного часу.
    observed_at = time.monotonic()
    changed = state.previous_ac != ac or state.startup_pending
    if ac == 0 and state.outage_started_at is None and state.outage_started_unix is not None:
        elapsed = time.time() - state.outage_started_unix
        if elapsed >= 0:
            # Після відновлення таймера далі знову використовуємо монотонний час.
            state.outage_started_at = observed_at - elapsed
    if ac == 0 and state.outage_started_unix is None:
        state.outage_started_at = observed_at
        state.outage_started_unix = time.time()
        state.outage_start_observed = state.previous_ac == 1 and not state.startup_pending
        # Обов'язково до Telegram: запис має пережити вимкнення сервера.
        save_state(state)
    charge = f"{battery}%" if battery is not None else "невідомий"
    if state.startup_pending:
        queue_startup_messages(state, ac, charge, observed_at)
        state.startup_pending = False
    elif state.previous_ac is None:
        logger.info("Початковий стан: AC=%s, заряд=%s", ac, charge)
    elif ac != state.previous_ac:
        if ac == 0:
            send_telegram_message(
                f"⚡ Зникло живлення\nСервер працює від батареї\n🔋 Заряд: {charge}"
            )
            if USER_CHAT_ID:
                send_telegram_message("⚡ Дома зникло світло", chat_id=USER_CHAT_ID)
        else:
            info = outage_info(state, observed_at)
            send_telegram_message(
                f"✅ Живлення відновлено\nСервер знову працює від мережі\n🔋 Заряд: {charge}\n{info}"
            )
            if USER_CHAT_ID:
                send_telegram_message(f"✅ Світло дома увімкнули\n{info}", chat_id=USER_CHAT_ID)
    state.previous_ac = ac

    if ac == 1:
        state.warned_thresholds.clear()
        state.outage_started_at = None
        state.outage_started_unix = None
        state.outage_restored = False
        state.outage_start_observed = False
        if changed:
            save_state(state)
        return
    if battery is None:
        if changed:
            save_state(state)
        return

    # <= враховує пропущений відсоток між вимірами та запуск на низькому заряді.
    # При різкому падінні надсилаємо тільки найтерміновіше попередження.
    for threshold, message in (
        (CRITICAL_WARNING_THRESHOLD,
         "Критичний заряд батареї 5%. Автоматичне вимкнення сервера відбудеться при 4%"),
        (15, "🚨 Критичний заряд батареї: 15% або нижче"),
        (30, "⚠️ Низький заряд батареї: 30% або нижче"),
        (40, "🔋 Заряд батареї знизився до 40% або нижче"),
        (60, "🔋 Заряд батареї знизився до 60% або нижче"),
        (80, "🔋 Заряд батареї знизився до 80% або нижче"),
    ):
        if battery <= threshold and threshold not in state.warned_thresholds:
            send_telegram_message(message if threshold == CRITICAL_WARNING_THRESHOLD
                                  else f"{message}\n🔋 Заряд: {battery}%")
            state.warned_thresholds.update(
                t for t in (CRITICAL_WARNING_THRESHOLD, 15, 30, 40, 60, 80) if t >= threshold
            )
            changed = True
            break

    if changed:
        save_state(state)


def current_status() -> str:
    ac = read_ac_status()
    battery = read_battery_level()
    power = {1: "🔌 Живлення від мережі", 0: "⚡ Живлення від батареї"}.get(
        ac, "❓ Стан живлення невідомий"
    )
    charge = f"{battery}%" if battery is not None else "невідомий"
    message = f"🔋 Поточний заряд: {charge}\n{power}"
    if not TEST_MODE:
        try:
            uptime = float(UPTIME_PATH.read_text(encoding="utf-8").split()[0])
            if math.isfinite(uptime) and uptime >= 0:
                message += f"\n🖥 Сервер працює: {format_duration(uptime)}"
        except (OSError, ValueError, IndexError):
            pass
    return message


def handle_telegram_update(update: dict) -> None:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
    else:
        callback = None
        message = update.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
        return
    chat_id = str(message["chat"].get("id", ""))
    if not chat_id or chat_id not in {ADMIN_CHAT_ID, USER_CHAT_ID}:
        return
    if callback is not None:
        callback_id = callback.get("id")
        if isinstance(callback_id, str):
            telegram_request("answerCallbackQuery", {"callback_query_id": callback_id})
        if callback.get("data") != "battery":
            return
        text = "/battery"
    else:
        text = message.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return
    command = text.split()[0].split("@")[0].lower()
    if command in ("/start", "/help"):
        send_telegram_message(
            "Натисніть «🔋 Поточний заряд», щоб перевірити батарею та живлення.\n"
            "/battery — заряд і стан живлення\n/status — стан сервера", chat_id=chat_id,
        )
    elif command in ("/battery", "/status"):
        send_telegram_message(current_status(), chat_id=chat_id)


def poll_telegram_updates(stop: threading.Event) -> None:
    offset = None
    while not stop.is_set():
        payload = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        response = telegram_request("getUpdates", payload, timeout=(5, 25))
        updates = response.get("result") if response is not None else None
        if not isinstance(updates, list):
            stop.wait(5)
            continue
        for update in updates:
            if stop.is_set():
                return
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                continue
            offset = update["update_id"] + 1
            handle_telegram_update(update)


def start_command_listener(stop: threading.Event) -> threading.Thread | None:
    if not BOT_TOKEN:
        return None
    listener = threading.Thread(target=poll_telegram_updates, args=(stop,), daemon=True,
                                name="telegram-commands")
    listener.start()
    return listener


def main() -> None:
    global test_step
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    if not TEST_MODE and (not BOT_TOKEN or not ADMIN_CHAT_ID):
        logger.error("Заповніть BOT_TOKEN та ADMIN_CHAT_ID у .env")
        raise SystemExit(1)
    logger.info("Моніторинг запущено; TEST_MODE=%s", TEST_MODE)
    state = (MonitorState(startup_pending=True) if TEST_MODE else load_state(STATE_FILE))
    stop = threading.Event()
    listener = start_command_listener(stop)
    try:
        while True:
            ac = read_ac_status()
            battery = read_battery_level()
            if ac is not None:
                process_reading(state, ac, battery)
            else:
                logger.warning("Ітерацію пропущено: стан AC невідомий")
            retry_startup_messages(state)
            if TEST_MODE:
                test_step += 1
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Моніторинг зупинено")
    finally:
        stop.set()
        if listener is not None:
            listener.join(timeout=1)


if __name__ == "__main__":
    main()

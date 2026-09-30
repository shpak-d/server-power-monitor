"""Односторонні Telegram-сповіщення про живлення Linux-сервера."""

import logging
import os
import subprocess
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
AC_PATH = Path("/sys/class/power_supply/AC/online")
BATTERY_PATH = Path("/sys/class/power_supply/BAT0/capacity")
logger = logging.getLogger(__name__)

# Кожен елемент використовується на одній ітерації (кожні 10 секунд).
# Після останнього елемента зберігається останній стан.
TEST_SCENARIO = [
    (1, 90), (0, 89), (0, 80), (0, 60), (0, 40), (0, 30), (0, 30), (0, 15), (0, 7),
    (0, 7), (1, 8), (0, 30), (1, 31),
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


def send_telegram_message(message: str, chat_id: str | None = None) -> bool:
    if chat_id is None:
        chat_id = ADMIN_CHAT_ID
    if not BOT_TOKEN or not chat_id:
        if TEST_MODE:
            logger.info("TEST_MODE, повідомлення без надсилання:\n%s", message)
            return True
        logger.error("BOT_TOKEN або chat_id отримувача не налаштовано")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=(5, 10),
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            logger.error("Telegram API не підтвердив надсилання")
            return False
    except (requests.RequestException, ValueError) as error:
        # Текст винятку requests може містити URL із токеном бота.
        logger.error("Помилка Telegram API (%s)", type(error).__name__)
        return False
    logger.info("Telegram-повідомлення надіслано")
    return True


def shutdown_server() -> bool:
    if TEST_MODE:
        logger.warning("TEST_MODE: автоматичне вимкнення лише змодельовано")
        return True

    # За час запиту Telegram живлення могло повернутися.
    ac = read_ac_status()
    battery = read_battery_level()
    if ac != 0 or battery is None or battery > 7:
        logger.warning("Вимкнення скасовано: умови не підтверджено")
        return False
    try:
        # -n забороняє очікувати пароль у фоновому systemd-сервісі.
        subprocess.run(["sudo", "-n", "shutdown", "-h", "now"],
                       check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        logger.error("Не вдалося вимкнути сервер: %s", error)
        return False
    logger.warning("Команду вимкнення виконано")
    return True


@dataclass
class MonitorState:
    previous_ac: int | None = None
    warned_thresholds: set[int] = field(default_factory=set)
    shutdown_requested: bool = False
    outage_started_at: float | None = None
    outage_start_observed: bool = False


def format_duration(seconds: float) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} год {minutes} хв {seconds} с"


def process_reading(state: MonitorState, ac: int, battery: int | None) -> None:
    """Обробити один вимір; стан зберігається між ітераціями."""
    # Монотонний годинник не залежить від переведення системного часу.
    observed_at = time.monotonic()
    if ac == 0 and state.outage_started_at is None:
        state.outage_started_at = observed_at
        state.outage_start_observed = state.previous_ac == 1
    charge = f"{battery}%" if battery is not None else "невідомий"
    if state.previous_ac is None:
        logger.info("Початковий стан: AC=%s, заряд=%s", ac, charge)
    elif ac != state.previous_ac:
        if ac == 0:
            send_telegram_message(
                f"⚡ Зникло живлення\nСервер працює від батареї\n🔋 Заряд: {charge}"
            )
            if USER_CHAT_ID:
                send_telegram_message("⚡ Дома зникло світло", chat_id=USER_CHAT_ID)
        else:
            if state.outage_started_at is None:
                duration = "невідомо"
            else:
                duration = format_duration(observed_at - state.outage_started_at)
                if not state.outage_start_observed:
                    duration = f"щонайменше {duration} (від запуску моніторингу)"
            outage_info = f"⏱ Без живлення: {duration}"
            send_telegram_message(
                f"✅ Живлення відновлено\nСервер знову працює від мережі\n🔋 Заряд: {charge}\n{outage_info}"
            )
            if USER_CHAT_ID:
                send_telegram_message(f"✅ Світло дома увімкнули\n{outage_info}", chat_id=USER_CHAT_ID)
    state.previous_ac = ac

    if ac == 1:
        state.warned_thresholds.clear()
        state.shutdown_requested = False
        state.outage_started_at = None
        state.outage_start_observed = False
        return
    if battery is None:
        return

    # <= враховує пропущений відсоток між вимірами та запуск на низькому заряді.
    # При різкому падінні надсилаємо тільки найтерміновіше попередження.
    for threshold, message in (
        (7, "🛑 Заряд батареї 7% або нижче\nСервер автоматично вимикається"),
        (15, "🚨 Критичний заряд батареї: 15% або нижче"),
        (30, "⚠️ Низький заряд батареї: 30% або нижче"),
        (40, "🔋 Заряд батареї знизився до 40% або нижче"),
        (60, "🔋 Заряд батареї знизився до 60% або нижче"),
        (80, "🔋 Заряд батареї знизився до 80% або нижче"),
    ):
        if battery <= threshold and threshold not in state.warned_thresholds:
            send_telegram_message(f"{message}\n🔋 Заряд: {battery}%")
            state.warned_thresholds.update(t for t in (7, 15, 30, 40, 60, 80) if t >= threshold)
            break

    # Збій Telegram не повинен перешкоджати безпечному вимкненню.
    if battery <= 7 and not state.shutdown_requested:
        state.shutdown_requested = shutdown_server()


def main() -> None:
    global test_step
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    if not TEST_MODE and (not BOT_TOKEN or not ADMIN_CHAT_ID):
        logger.error("Заповніть BOT_TOKEN та ADMIN_CHAT_ID у .env")
        raise SystemExit(1)
    logger.info("Моніторинг запущено; TEST_MODE=%s", TEST_MODE)
    state = MonitorState()
    try:
        while True:
            ac = read_ac_status()
            battery = read_battery_level()
            if ac is not None:
                process_reading(state, ac, battery)
            else:
                logger.warning("Ітерацію пропущено: стан AC невідомий")
            if TEST_MODE:
                test_step += 1
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Моніторинг зупинено")


if __name__ == "__main__":
    main()

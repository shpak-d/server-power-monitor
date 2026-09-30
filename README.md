# Моніторинг живлення домашнього Linux-сервера

Python 3.11+. Скрипт кожні 10 секунд читає стан мережевого живлення та батареї й надсилає сповіщення Telegram. Команди Telegram не слухає.

## Налаштування .env

Виконуйте команди з директорії `server-power-monitor`. Скопіюйте `.env.example` у `.env` і заповніть `BOT_TOKEN` та `CHAT_ID`. Токен видає Telegram-бот @BotFather. Для особистих сповіщень спочатку відкрийте свого бота в Telegram і натисніть Start. Бот повинен мати доступ до вказаного чату.

```dotenv
BOT_TOKEN=ваш_токен
CHAT_ID=ваш_chat_id
TEST_MODE=false
```

`.env` виключений із Git. Він завантажується з директорії `bot.py`; вже задані змінні середовища мають пріоритет. Не публікуйте токен.

## Windows / PyCharm: тестовий режим

У PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

У `.env` встановіть `TEST_MODE=True`, потім запустіть:

```powershell
.\.venv\Scripts\python.exe bot.py
```

У PyCharm виберіть інтерпретатор `.venv\Scripts\python.exe` та запустіть `bot.py`. Можна також задати `TEST_MODE=True` у змінних середовища конфігурації запуску.

Тестовий режим не читає `/sys` та не запускає shutdown. Якщо токен або chat ID порожні, повідомлення виводяться лише в лог. Якщо обидва заповнені, повідомлення дійсно надсилаються в Telegram.

Сценарій `TEST_SCENARIO` в `bot.py`: мережа → батарея → 30% → повтор 30% → 15% → 7% → повтор 7% → повернення мережі → нове відключення при 30% → мережа. Один крок кожні 10 секунд; після завершення зберігається останній стан. Зупинка — Ctrl+C. Сценарій можна змінювати.

## Linux Mint

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Потрібен Python 3.11 або новіший; якщо модуль venv відсутній, встановіть пакет venv для своєї версії Python. Заповніть секрети в `.env`, встановіть `TEST_MODE=false` і запустіть:

```bash
.venv/bin/python bot.py
```

Джерела даних:

- `/sys/class/power_supply/AC/online`: `1` — мережа, `0` — батарея.
- `/sys/class/power_supply/BAT0/capacity`: заряд від 0 до 100.

Перевірка вручну:

```bash
cat /sys/class/power_supply/AC/online
cat /sys/class/power_supply/BAT0/capacity
```

На іншому обладнанні назви можуть відрізнятися. За потреби змініть `AC_PATH` і `BATTERY_PATH` у `bot.py`.

## Автоматичне вимкнення та sudo

При заряді **7% або нижче на батареї** скрипт надсилає повідомлення, повторно перевіряє живлення й запускає `sudo -n shutdown -h now`. Опція `-n` забороняє запит пароля: systemd не має інтерактивного термінала.

Для користувача сервісу налаштуйте дозвіл на цю команду без пароля. Знайдіть шлях:

```bash
command -v shutdown
sudo visudo -f /etc/sudoers.d/server-power-monitor
```

Додайте рядок, замінивши `YOUR_USER` і шлях на фактичні:

```sudoers
YOUR_USER ALL=(root) NOPASSWD: /usr/sbin/shutdown -h now
```

Збережіть файл через `visudo`, який перевіряє синтаксис. Можна перевірити дозволи командою `sudo -l`, не вимикаючи комп'ютер. Якщо команда вимкнення не вдалася, помилка потрапить у лог, а наступна ітерація повторить спробу без повторного Telegram-повідомлення.

## Автозапуск через systemd

Відредагуйте `server-power-monitor.service`: замініть `YOUR_USER`, `WorkingDirectory` та шляхи в `ExecStart` на реальні абсолютні шляхи Linux. Для шляхів із пробілами візьміть значення `WorkingDirectory` та кожен шлях у `ExecStart` у подвійні лапки. Сервіс використовує Python із `.venv`; `.env` читає сам скрипт.

```bash
sudo cp server-power-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-power-monitor.service
sudo systemctl status server-power-monitor.service
journalctl -u server-power-monitor.service -f
```

`Restart=always` перезапускає процес після завершення з паузою 10 секунд. Для зупинки:

```bash
sudo systemctl stop server-power-monitor.service
```

Після зміни `.env` перезапустіть сервіс через `sudo systemctl restart server-power-monitor.service`.

## Поведінка та перевірка логіки

Автоматичні тести (без звернень до Telegram та без реального вимкнення):

```bash
python -m unittest -v
```

Використовуйте Python із встановленими залежностями: `.venv/bin/python` на Linux або `.\.venv\Scripts\python.exe` на Windows. GitHub Actions повторює тести на Linux і Windows з Python 3.11–3.13 та перевіряє синтаксис systemd-сервісу, не запускаючи його.

- Початковий стан записується в лог. Сповіщення про зникнення/повернення мережі надсилаються лише при зміні AC.
- Пороги перевіряються як `<=`, тому пропуск точного відсотка не завадить попередженню. При різкому падінні або запуску з низьким зарядом надсилається тільки найтерміновіше попередження.
- Кожен поріг спрацьовує один раз за період роботи від батареї. Повернення мережі скидає прапорці. Стан зберігається в пам'яті: після перезапуску попередження можуть повторитися.
- При помилці читання AC ітерація пропускається. Якщо заряд невідомий, зміни AC все одно повідомляються, але вимкнення не виконується.
- Мережеві помилки та помилки Telegram логуються, процес продовжує роботу. Запити мають timeout. Черги повторного надсилання немає: повідомлення, яке не вдалося надіслати, може бути втрачено. Вимкнення не залежить від успіху Telegram.
- `process_reading(MonitorState(), ac, battery)` дозволяє перевіряти переходи окремо від циклу. У тестах підміняйте `send_telegram_message` та `shutdown_server` через `unittest.mock`; функції читання та Telegram також можна перевіряти окремо.

systemd запускає моніторинг після завантаження ОС, але сам не вмикає вимкнений ноутбук після повернення електрики — це залежить від налаштувань BIOS/UEFI та обладнання.

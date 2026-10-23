# Check_merchant_bb_bot

[![CI](https://github.com/klouner/Check_merchant_bb_bot/actions/workflows/ci.yml/badge.svg)](https://github.com/klouner/Check_merchant_bb_bot/actions/workflows/ci.yml)

Telegram-бот для P2P-трейдеров Bybit. Ты добавляешь ники мерчантов, бот следит за стаканом **USDT/RUB**
и сразу пишет, когда кто-то из них выставил новое объявление или поменял курс либо лимиты.

Бот работает только с публичным P2P-API Bybit: ключи и аккаунт Bybit не нужны.
Чтобы запустить свою копию, хватит токена от [@BotFather](https://t.me/BotFather).

- [Как пользоваться ботом](#как-пользоваться-ботом)
- [Быстрый старт: свой бот за 5 минут](#быстрый-старт-свой-бот-за-5-минут)
- [Настройки (.env)](#настройки-env)
- [Обновление, бэкап, логи](#обновление-бэкап-логи)
- [Автодеплой через GitHub Actions](#автодеплой-через-github-actions)
- [Частые проблемы](#частые-проблемы)
- [Как устроен](#как-устроен) · [Разработка](#разработка)

---

## Как пользоваться ботом

1. Открой своего бота в Telegram и нажми **Start**.
2. `/settings` → **➕ Добавить мерчанта** → пришли ник так, как он написан на Bybit (регистр не важен).
3. Готово: при появлении или изменении объявления этого мерчанта придёт сообщение такого вида:

```
📢 SomeMerchant · 🟢 Покупка · 🟢 онлайн
Курс: 81.20 ₽ · #4 в стакане
Диапазон: 5 000 – 300 000 ₽ · доступно 12 500 USDT
Оплата: СБП, Т-Банк
Сделок за 30д: 951 (99%) · ✅ проверен
💬 Только переводы с карты на своё имя…
         [ ℹ️ Подробнее ]  [ 🔗 Bybit ]
```

**ℹ️ Подробнее** показывает сводку по мерчанту: все его текущие объявления с местом в стакане,
онлайн-статус, требования к контрагенту и коридор курса за 30 дней из истории бота.
**🔗 Bybit** открывает профиль мерчанта на сайте.

### Команды

| Команда | Что делает |
|---|---|
| `/settings` | Меню: список мерчантов (ℹ️ сводка, ❌ удалить), добавление, фильтры, пауза, выгрузка в Excel |
| `/info ник` | Сводка по любому мерчанту, даже если его нет в твоём списке |
| `/stats` | Сколько уведомлений пришло сегодня, всего и по мерчантам за 24 часа |
| `/pause` | Поставить уведомления на паузу или снять с неё (список мерчантов сохраняется) |
| `/help` | Короткая справка по фильтрам |
| `/cancel` | Отменить ввод ника |

Меню команд бот регистрирует в Telegram сам при запуске.

### Фильтры (`/settings`)

| Кнопка | Значения | Что делает |
|---|---|---|
| Ордера до 10к | ON / OFF | OFF скрывает объявления с максимумом меньше 10 000 ₽ |
| Исключать фиксы | ON / OFF | ON скрывает объявления, где минимум равен максимуму |
| Тип | все / покупка / продажа | Какую сторону стакана присылать |
| Только онлайн | ON / OFF | ON скрывает объявления мерчантов, которые сейчас офлайн |
| Место в стакане | любое / топ-10 / топ-30 / топ-100 | Только объявления не ниже выбранного места в выдаче Bybit |
| Снятие объявлений | ON / OFF | ON присылает ⚫️, когда мерчант убрал объявление |
| Пауза | | Уведомления не приходят, список мерчантов остаётся |
| 📊 Выгрузить Excel | | Журнал всех присланных тебе объявлений |

Фильтры у каждого пользователя свои. Одного мерчанта могут отслеживать сразу несколько человек.

---

## Быстрый старт: свой бот за 5 минут

### 0. Подготовка

1. В [@BotFather](https://t.me/BotFather) выполни `/newbot` и скопируй токен вида `123456789:AA...`.
2. *(Необязательно)* Служебный чат, куда бот пишет о новых пользователях, добавлении и удалении мерчантов
   и о недоступности Bybit API. Создай группу, добавь в неё бота и узнай ID группы (он начинается с `-100`).
   Узнать ID можно через [@getmyid_bot](https://t.me/getmyid_bot) или
   `https://api.telegram.org/bot<ТОКЕН>/getUpdates` после любого сообщения в группе.
3. Сервер или компьютер, который работает постоянно. Подойдёт любой VPS с 512 МБ RAM.
   Важно: у Bybit есть региональные ограничения, поэтому проверь, что API доступен с IP сервера
   (см. [Частые проблемы](#частые-проблемы)).

### Вариант 1: Docker (рекомендуется)

Нужны git и Docker с плагином compose (`curl -fsSL https://get.docker.com | sh`).

```bash
git clone https://github.com/klouner/Check_merchant_bb_bot.git
cd Check_merchant_bb_bot
cp .env.example .env
nano .env                     # впиши BOT_TOKEN, остальное по желанию
docker compose up -d --build
docker compose logs -f        # должно появиться «Бот запущен»
```

База и лог лежат в `./data` и сохраняются при пересборке. Контейнер сам поднимается после
перезагрузки сервера (`restart: unless-stopped`).

### Вариант 2: Python + systemd (Linux без Docker)

Нужен Python 3.10 или новее.

```bash
sudo git clone https://github.com/klouner/Check_merchant_bb_bot.git /opt/Check_merchant_bb_bot
cd /opt/Check_merchant_bb_bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env
sudo cp deploy/Check_merchant_bb_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now Check_merchant_bb_bot
journalctl -u Check_merchant_bb_bot -f
```

Если ставишь в другую папку, поправь пути в `deploy/Check_merchant_bb_bot.service`.
В юните есть подсказка, как запускать бота не от root.

### Вариант 3: локально (Windows / macOS / Linux)

Подходит, чтобы попробовать бота или что-то в нём поменять.

```bash
git clone https://github.com/klouner/Check_merchant_bb_bot.git
cd Check_merchant_bb_bot
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
python main.py
```

> Один токен может использовать только одна запущенная копия бота. Если запустить вторую,
> Telegram вернёт ошибку `409 Conflict`.

---

## Настройки (.env)

| Переменная | Обязательна | По умолчанию | Описание |
|---|---|---|---|
| `BOT_TOKEN` | да | | Токен от @BotFather |
| `ADMIN_GROUP_ID` | нет | пусто | ID чата для служебных уведомлений. Пусто — бот их не отправляет |
| `ADMIN_CONTACT` | нет | пусто | Контакт для вопросов (например, `@your_username`), показывается в `/start` и `/help` |
| `CHECK_INTERVAL` | нет | `10` | Секунд между опросами Bybit, минимум 3 |
| `DATA_DIR` | нет | папка с кодом | Где хранить `merchants.db` и `bot.log`. В Docker это `/app/data` |
| `MERCHANTS_DB_PATH`, `BOT_LOG_PATH` | нет | `DATA_DIR/…` | Точные пути к базе и логу, если нужны отдельно |

Переменные окружения важнее, чем `.env`. Старый `config.py` с `BOT_TOKEN` и `ADMIN_GROUP_ID` тоже
читается, так что ранние установки продолжают работать.

---

## Обновление, бэкап, логи

**Обновить:**

```bash
git pull
docker compose up -d --build                       # Docker
# или: .venv/bin/pip install -r requirements.txt && sudo systemctl restart Check_merchant_bb_bot
```

Новые колонки в базе бот добавляет сам при запуске, данные пользователей сохраняются.

**Бэкап.** Всё состояние хранится в одном файле SQLite: `data/merchants.db` в Docker или `merchants.db`
рядом с кодом. Для бэкапа остановить бота и скопировать файл. Журнал наблюдений (`offer_logs`)
растёт со временем. Если база стала слишком большой, её можно почистить, остановив бота:

```bash
sqlite3 data/merchants.db "DELETE FROM offer_logs WHERE timestamp < datetime('now','-90 days'); VACUUM;"
```

**Логи:** `docker compose logs -f`, `journalctl -u Check_merchant_bb_bot -f` или файл `bot.log`
(ротация 5 МБ × 3 файла).

---

## Автодеплой через GitHub Actions

Необязательно. Workflow [`ci.yml`](.github/workflows/ci.yml) на каждый push и pull request прогоняет
тесты и проверяет сборку Docker-образа. Чтобы при push в `main` бот ещё и сам обновлялся на твоём сервере:

1. Сделай fork репозитория.
2. На сервере установи git и Docker, создай папку деплоя с файлом `.env` (как в «Варианте 1»).
3. В fork открой **Settings → Secrets and variables → Actions**:
   - **Secrets:** `DEPLOY_HOST` (IP), `DEPLOY_USER`, `DEPLOY_SSH_KEY` (приватный ключ, публичная часть
     добавлена в `~/.ssh/authorized_keys` на сервере), при необходимости `DEPLOY_PORT`;
   - **Variables:** `DEPLOY_ENABLED` = `true`, при необходимости `DEPLOY_PATH`
     (по умолчанию `~/Check_merchant_bb_bot`).

Job `deploy` по SSH делает `git reset --hard origin/main` и `docker compose up -d --build`.
Файлы `.env` и `data/` при этом не трогаются. Если fork приватный, серверу нужен доступ к нему
на чтение (deploy key или токен).

---

## Частые проблемы

| Симптом | Причина и решение |
|---|---|
| `BOT_TOKEN не задан` | Нет `.env` или в нём пустой токен |
| `409 Conflict: terminated by other getUpdates request` | Этот токен уже использует другая копия бота. Останови её |
| В логе `Ошибка bybit_check_all … 403` / таймауты, админам пришло «Bybit API не отвечает» | Bybit не отвечает с IP сервера (региональная блокировка или защита от ботов). Проверь `curl -s -X POST https://api2.bybit.com/fiat/otc/item/online -H 'content-type: application/json' -d '{"tokenId":"USDT","currencyId":"RUB","side":"1","size":"10","page":"1"}'`. Если не помогает, смени сервер или регион |
| Уведомления не приходят | Проверь ник через `/info ник`, фильтры и паузу в `/settings`. Сразу после запуска бот молча запоминает уже висящие объявления и присылает только новые изменения |
| Бот не пишет в служебный чат | Бот не добавлен в группу или в `ADMIN_GROUP_ID` нет `-100` в начале |

---

## Как устроен

- [`checker.py`](checker.py) раз в `CHECK_INTERVAL` секунд забирает весь стакан BUY и SELL с
  `api2.bybit.com`, страницами по 300 объявлений (это максимум для Bybit).
- [`main.py`](main.py) сверяет ники с подписками и применяет личные фильтры. Уведомления складываются
  в очередь, отдельный поток отправляет их с защитой от флуда (не чаще 1 сообщения в секунду на
  пользователя). Повторы отсекаются по хэшу `id|price|min|max`, который хранится 7 дней.
- [`database.py`](database.py) — SQLite в режиме WAL. Миграции старых баз выполняются при запуске.
- [`app_config.py`](app_config.py) читает настройки из окружения, `.env` или `config.py`.

**Какие данные хранит бот:** Telegram ID и username пользователей, их список мерчантов, фильтры и
журнал присланных объявлений. Всё это лежит только в твоей базе SQLite. Если указан `ADMIN_GROUP_ID`,
в служебный чат приходят username пользователей, которые запустили бота или поменяли список мерчантов.

## Разработка

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests        # офлайн, без реального Telegram и Bybit
```

Pull request'ы приветствуются. CI прогоняет тесты на Python 3.10 и 3.12 и собирает Docker-образ.

## Лицензия

[MIT](LICENSE). Бот не связан с Bybit. Используй на свой риск и соблюдай правила площадки.

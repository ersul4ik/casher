# Cacher — трекер трат из банковских пушей

Банк присылает пуш на iPhone → шорткат отправляет сумму боту → бот спрашивает категорию
кнопками → отчёты за день, неделю и месяц.

Сервис многопользовательский: у каждого свои траты, категории, валюта, часовой пояс и личный
токен для шортката. Чужие данные недоступны даже при знании id записи.

```
Пуш банка на iPhone
   ↓  Shortcuts, триггер «Когда я получаю уведомление»
   ↓  POST https://<host>/spend  +  заголовок X-Token: <личный токен>
Бот (aiogram + aiohttp в одном процессе)
   ↓  пишет трату в Postgres со статусом «без категории»
   ↓  «💸 Новая трата: 1470 KGS. Куда записать?» + кнопки категорий
Нажатие → «✅ 1470 KGS → Кафе»
```

## Стек

| Что | Чем |
|---|---|
| Бот | Python 3.10+, aiogram 3 |
| HTTP-приём трат | aiohttp (тот же процесс, тот же порт) |
| База | Postgres (asyncpg) |
| Хостинг | Render free + Neon free — см. [docs/DEPLOY.md](docs/DEPLOY.md) |

## Как развернуть

Полный пошаговый гайд со ссылками и тем, где что брать: **[docs/DEPLOY.md](docs/DEPLOY.md)**.
Настройка шортката на iPhone: **[docs/SHORTCUT.md](docs/SHORTCUT.md)**.

## Локальный запуск

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # вписать BOT_TOKEN и DATABASE_URL
set -a && source .env && set +a
.venv/bin/python scripts/check_db.py   # проверить связь с базой
.venv/bin/python -m app.main      # без BASE_URL бот работает на long polling
```

Схема базы создаётся сама при старте.

## Команды бота

| Команда | Что делает |
|---|---|
| `/start` | регистрация, нижнее меню с кнопками |
| `/report` | меню отчётов: сегодня, вчера, неделя, прошлая неделя, месяц, прошлый месяц |
| `/day` `/week` `/month` | сразу нужный период; стрелки `←` `→` листают периоды назад и вперёд |
| `/pending` | разметить траты, оставшиеся без категории |
| `/last` | последние 10 трат |
| `/cats` | категории: добавить, убрать |
| `/export` | выгрузка всех трат в CSV |
| `/settings`, `/currency USD`, `/tz +6` | валюта и часовой пояс |
| `/token`, `/newtoken` | токен для шортката и его перевыпуск |
| `/stats` | сводка по всем пользователям, только для `ADMIN_IDS` |

Трату можно завести и руками: написать боту `1470` или `1470 кафе` — во втором случае
категория подставится сама по началу названия.

## Приём трат: `POST /spend`

```bash
curl -X POST https://<host>/spend \
  -H "X-Token: <личный токен из /token>" \
  -H "Content-Type: application/json" \
  -d '{"amount": 1470.00, "currency": "KGS", "raw": "Успещная операция по QR. Сумма: 1470.00 KGS"}'
```

Поля `amount` и `currency` необязательные: если прислать только `raw`, бот сам достанет сумму
и валюту из текста пуша. Это заметно упрощает шорткат.

| Поле | Обязательно | Комментарий |
|---|---|---|
| `raw` | нет* | полный текст пуша, сохраняется целиком |
| `amount` | нет* | число; если не задано, берётся из `raw` |
| `currency` | нет | по умолчанию — валюта пользователя |
| `occurred_at` | нет | ISO-8601, если время операции отличается от времени запроса |
| `source` | нет | метка источника, по умолчанию `shortcut` |

\* хотя бы одно из `raw` / `amount` должно позволить определить сумму.

Ответы: `200 {"status":"ok","id":…}`, `200 {"status":"duplicate",…}` для повторного пуша с той
же суммой в пределах 90 секунд, `401` без токена, `403` с чужим токеном, `400` если сумму
определить не удалось.

`GET /health` — healthcheck и точка для пинговалки, которая не даёт бесплатному инстансу заснуть.

## Тесты

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Интеграционные тесты требуют Postgres и пропускаются без него:

```bash
docker run -d --name cacher-pg -e POSTGRES_PASSWORD=pass -e POSTGRES_DB=cacher \
  -p 55432:5432 postgres:16-alpine
export TEST_DATABASE_URL=postgresql://postgres:pass@localhost:55432/cacher
.venv/bin/python -m unittest discover -s tests -t .
```

## Структура

```
app/
  main.py       точка входа: aiohttp-сервер + бот в одном процессе
  config.py     переменные окружения
  db.py         пул asyncpg и все запросы
  schema.sql    таблицы users / categories / spends
  webapi.py     POST /spend, GET /health, парсинг суммы из текста пуша
  handlers.py   команды, кнопки, ручной ввод трат
  keyboards.py  клавиатуры
  reports.py    границы периодов и текст отчётов
  notifier.py   вопрос «куда записать?»
tests/          юнит- и интеграционные тесты
docs/           DEPLOY.md и SHORTCUT.md
```

## Схема данных

`users` — `tg_id`, `api_token`, `currency`, `tz_minutes`, `created_at`, `last_seen_at`.
`categories` — свои у каждого пользователя, уникальны без учёта регистра, удаление = архивация.
`spends` — `user_id`, `category_id`, `amount NUMERIC(14,2)`, `currency`, `status`
(`pending` / `done` / `ignored`), `source`, `raw`, `occurred_at`, `created_at`, `categorized_at`.

Сырой текст пуша хранится рядом с распарсенной суммой: если банк поменяет формат, историю
можно будет пересчитать.

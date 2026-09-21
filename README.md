# Cacher — a spend tracker fed by bank push notifications

The bank sends a push to the iPhone → a Shortcuts automation forwards the amount to the bot →
the bot asks for a category with buttons → day, week and month reports come out of it.

The service is multi-user: everyone gets their own spends, categories, tags, currency, time zone
and personal shortcut token. Another user's data stays out of reach even if the record id is known.

```
Bank push on the iPhone
   ↓  Shortcuts, "When I Get a Notification" trigger
   ↓  POST https://<host>/spend  +  header X-Token: <personal token>
Bot (aiogram + aiohttp in one process)
   ↓  stores the spend in Postgres as uncategorised
   ↓  "💸 Новая трата: 1470 KGS. Куда записать?" + category buttons
Tap → "✅ 1470 KGS → Кафе"
```

The bot speaks Russian to its users; the code, comments and this file are in English.

## Stack

| Part | Choice |
|---|---|
| Bot | Python 3.10+, aiogram 3 |
| Spend intake over HTTP | aiohttp, same process and same port |
| Database | Postgres via asyncpg |
| Hosting | Render free tier + Neon free tier — see [docs/DEPLOY.md](docs/DEPLOY.md) |

## Deployment

Step-by-step guide, including where every credential comes from: **[docs/DEPLOY.md](docs/DEPLOY.md)**.
Setting up the iPhone shortcut: **[docs/SHORTCUT.md](docs/SHORTCUT.md)**.
Both are written in Russian, for the person running this instance.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                   # fill in BOT_TOKEN and DATABASE_URL
set -a && source .env && set +a
.venv/bin/python scripts/check_db.py   # verify the database connection
.venv/bin/python -m app.main           # without BASE_URL the bot runs on long polling
```

The schema is created on start, so there is nothing to migrate by hand.

## Bot commands

| Command | What it does |
|---|---|
| `/start` | registration, plus the bottom keyboard |
| `/report` | report menu: today, yesterday, this and last week, this and last month |
| `/day` `/week` `/month` | jump straight to a period; `←` `→` page through earlier ones |
| `/pending` | tag the spends still waiting for a category |
| `/last` | recent spends: open one by its number to tag, annotate or delete it |
| `/cats` | categories: add, remove |
| `/export` | CSV export of everything |
| `/settings`, `/currency USD`, `/tz +6` | currency and time zone |
| `/token`, `/newtoken` | the shortcut token, and reissuing it |
| `/stats` | service-wide summary, `ADMIN_IDS` only |

A spend can also be entered by hand: send `1470`, or `1470 кафе` to have the category matched by
the start of its name.

## Where spends come from

Two kinds of push, two Shortcuts automations — see [docs/SHORTCUT.md](docs/SHORTCUT.md):

| Payment | Pushed by | Text |
|---|---|---|
| QR payments, transfers | the bank's app | `Успещная операция по QR. Сумма: 1470.00 KGS` |
| Apple Pay card payments | **Wallet** | `OPTIMA BANK OJSC` / `Arabesk Bishkek` / `470,00 KGS` |

The shortcut does not need to filter anything: the bot drops pushes about failed operations and
refuses to read account or phone numbers as amounts. Apple Pay pushes also name the shop, and the
bot remembers which category that shop usually gets, offering it first with a ⭐ next time.

## Categories and tags

A spend has exactly one category and any number of tags. The category answers "where did it go"
(Магазин), tags answer "on what exactly" (вода, кофе, курут).

Once a category is set, the spend turns into a card with four buttons:

| Button | What it does |
|---|---|
| 🏷 Теги | toggle tags with buttons; the most used come first |
| 📝 Описание | free-text note; sending `-` clears it |
| ✏️ Категория | pick the category again |
| 🗑 Удалить | delete the spend, after a confirmation |

An open card also listens for tags typed as one line — `вода, кофе, курут`. A name already in
use attaches that tag whatever its case, an unknown one is created on the spot, so tagging costs
a single message instead of a trip through the 🏷 button. A line that reads like a new spend
(`350 кофейня`) is still a new spend; after the explicit **➕ Новые теги** button it is not, so a
tag may start with a digit there. A command or a menu tap ends the listening.

The same card opens for any past spend: `/last`, then tap its number.

Every report has a **🏷 По тегам** button — the same period broken down by tag instead of
category. A spend carrying several tags counts towards each of them, so the tag totals can exceed
the overall total; the report says so outright.

## Spend intake: `POST /spend`

```bash
curl -X POST https://<host>/spend \
  -H "X-Token: <personal token from /token>" \
  -H "Content-Type: application/json" \
  -d '{"amount": 1470.00, "currency": "KGS", "raw": "Успещная операция по QR. Сумма: 1470.00 KGS"}'
```

`amount` and `currency` are optional: sending only `raw` lets the bot pull the amount and the
currency out of the push text itself, which keeps the shortcut down to a single action.

| Field | Required | Notes |
|---|---|---|
| `raw` | no* | the full push text, stored verbatim |
| `amount` | no* | a number; taken from `raw` when absent |
| `currency` | no | defaults to the user's currency |
| `merchant` | no | shop name; recovered from `raw` when the push carries one |
| `occurred_at` | no | ISO-8601, when the operation time differs from the request time |
| `source` | no | free-form origin label, `shortcut` by default |

\* at least one of `raw` / `amount` has to yield an amount.

Responses: `200 {"status":"ok","id":…}`; `200 {"status":"duplicate",…}` for a repeated push with
the same amount within 90 seconds; `200 {"status":"skipped",…}` for a push reporting a failed or
cancelled operation; `401` without a token, `403` with someone else's, `400` when no amount could
be determined.

`GET /health` — healthcheck, the target of the keep-alive ping, and it reports the deployed
commit so a missing deploy is easy to tell from a code problem.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Integration tests need Postgres and are skipped without it:

```bash
docker run -d --name cacher-pg -e POSTGRES_PASSWORD=pass -e POSTGRES_DB=cacher \
  -p 55432:5432 postgres:16-alpine
export TEST_DATABASE_URL=postgresql://postgres:pass@localhost:55432/cacher
.venv/bin/python -m unittest discover -s tests -t .
```

## Layout

```
app/
  main.py       entry point: aiohttp server and bot in one process
  config.py     environment variables
  db.py         asyncpg pool and every query
  schema.sql    users / categories / spends / tags
  webapi.py     POST /spend, GET /health, amount parsing out of push text
  handlers.py   commands, buttons, manual entry
  keyboards.py  keyboards
  reports.py    period boundaries and report text
  notifier.py   the "which category?" prompt
tests/          unit and integration tests
docs/           DEPLOY.md and SHORTCUT.md
```

## Data model

`users` — `tg_id`, `api_token`, `currency`, `tz_minutes`, `created_at`, `last_seen_at`.
`categories` — per user, unique case-insensitively; deleting archives rather than drops.
`spends` — `user_id`, `category_id`, `amount NUMERIC(14,2)`, `currency`, `status`
(`pending` / `done` / `ignored`), `source`, `raw`, `merchant`, `note`, `occurred_at`,
`created_at`, `categorized_at`.
`tags` + `spend_tags` — a user's tags and their many-to-many link to spends. Deleting a spend
drops its links; the tags themselves survive for future spends.

The schema is applied on every start and is fully idempotent, so new tables appear on a running
service by themselves — no separate migration step.

The raw push text is kept next to the parsed amount: if the bank changes its format, history can
be recomputed.

## Roadmap

Ordered by value, not by effort. Nothing here is a new kind of report — the point is to cut the
work the user has to do.

### Auto-categorisation — refinements

The basics ship already: the bot suggests a category from the user's own history, marking it with
a ⭐ on a wide button of its own. The merchant wins when known; otherwise the amount decides, and
only once it has been filed the same way at least twice.

What is still worth adding:

- **Time of day.** 350 in the morning is coffee, the same 350 in the evening is not.
- **Silent auto-fill for the obvious cases.** After a merchant has been filed the same way many
  times, record it without asking and let the card be corrected afterwards.

### Limits and alerts

A monthly limit per category and one overall, reported at the moment a spend is recorded rather
than in a separate report:

```
✅ 1 470 KGS → Кафе
⚠️ Кафе: 8 200 из 10 000 за месяц (82%)
```

Worth including the pace, not just the share: "15 days of 30 gone, 82% of the limit spent" catches
an overrun while it can still be corrected. Set with something like `/limit кафе 10000`.

### Editing the amount

Currently impossible — a wrong amount can only be deleted and re-entered. Small fix, obvious gap.

### Reminder about uncategorised spends

An evening nudge when spends are still waiting for a category. The keep-alive cron already exists,
so this needs one more endpoint and no new infrastructure.

### Later

- **Subscriptions.** Spot an amount that repeats monthly and warn before it is charged again.
  Needs a couple of months of history before it can work at all.
- **Currency conversion.** Convert foreign-currency spends into the main currency at the national
  bank rate, so totals stop splitting per currency.
- **Income tracking.** Incoming transfers are currently discarded via the "не расход" button;
  storing them would allow an in/out balance.

### Deliberately not planned

- **Receipt OCR and voice input.** Fashionable, but needs a paid LLM API for a rare case.
- **Bank account aggregation.** There is no open banking in Kyrgyzstan; pushes are all there is.
- **Web dashboard.** Duplicates the bot and adds a second service to keep alive.

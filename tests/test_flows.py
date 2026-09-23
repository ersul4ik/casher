"""Whole-dialog checks: an update goes through the real dispatcher, Telegram is a stub.

These cover the wiring that database tests cannot see — which message the bot reads as
tags and which one it still reads as a new spend, what the calendar offers to tap.

Requires TEST_DATABASE_URL, see test_integration.py.
"""

from __future__ import annotations

import os
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app import db
from app.config import Config
from app.main import build_dispatcher
from app.reports import month_name, today_local

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
TG_ID = 4242
TZ_MINUTES = 360

CONFIG = Config(
    bot_token="123456:AAHnotARealTokenButLongEnoughForAiogram",
    database_url=TEST_DSN,
    base_url=None,
    webhook_secret="secret",
    port=8080,
    default_currency="KGS",
    default_tz_minutes=TZ_MINUTES,
    admin_ids=frozenset(),
    dedup_window_seconds=90,
)

CHAT = Chat(id=TG_ID, type="private")
FROM = User(id=TG_ID, is_bot=False, first_name="Эрик")


_dispatcher = None


def dispatcher_for(pool):
    """One dispatcher for the whole module: the router cannot attach to a second one.

    Each test still gets its own pool and an empty FSM storage.
    """
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = build_dispatcher(pool, CONFIG)
    _dispatcher["pool"] = pool
    _dispatcher.fsm.storage = MemoryStorage()
    return _dispatcher


class StubBot(Bot):
    """Answers every API call locally and remembers the texts the user would have seen."""

    def __init__(self) -> None:
        super().__init__(
            CONFIG.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        self.texts: list[str] = []
        self.markups: list[object] = []
        self.alerts: list[str | None] = []
        self.methods: list[str] = []
        # Telegram stops allowing edits once a message is two days old.
        self.refuse_edits = False

    async def __call__(self, method, request_timeout=None):  # type: ignore[override]
        # Only what lands in the chat; a callback answer is a toast, not a message.
        name = type(method).__name__
        self.methods.append(name)
        if name == "EditMessageText" and self.refuse_edits:
            raise TelegramBadRequest(
                method=method, message="Bad Request: message can't be edited"
            )
        if name in {"SendMessage", "EditMessageText"}:
            self.texts.append(method.text)
            self.markups.append(method.reply_markup)
            return Message(
                message_id=len(self.texts),
                date=datetime.now(timezone.utc),
                chat=CHAT,
                text=method.text,
            )
        if name == "AnswerCallbackQuery":
            self.alerts.append(method.text)
        return True

    @property
    def last(self) -> str:
        return self.texts[-1] if self.texts else ""

    @property
    def last_markup(self):
        return self.markups[-1] if self.markups else None


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL is not set")
class BotFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await db.create_pool(TEST_DSN)
        await self.pool.execute(
            "DROP TABLE IF EXISTS spend_tags, tags, spends, categories, users CASCADE"
        )
        await db.apply_schema(self.pool)
        self.bot = StubBot()
        self.dp = dispatcher_for(self.pool)
        self.update_id = 0

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        await self.pool.close()

    async def send(self, text: str) -> None:
        self.update_id += 1
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=self.update_id,
                message=Message(
                    message_id=self.update_id,
                    date=datetime.now(timezone.utc),
                    chat=CHAT,
                    from_user=FROM,
                    text=text,
                ),
            ),
        )

    async def tap(self, data: str) -> None:
        self.update_id += 1
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=self.update_id,
                callback_query=CallbackQuery(
                    id=str(self.update_id),
                    from_user=FROM,
                    chat_instance="stub",
                    data=data,
                    message=Message(
                        message_id=self.update_id,
                        date=datetime.now(timezone.utc),
                        chat=CHAT,
                        text="карточка",
                    ),
                ),
            ),
        )

    async def user_id(self) -> int:
        return await self.pool.fetchval("SELECT id FROM users WHERE tg_id = $1", TG_ID)

    async def add_spend_and_categorise(self, text: str = "480") -> int:
        """Type a spend, tap the first category, and return the spend id."""
        await self.send(text)
        user_id = await self.user_id()
        spend_id = await self.pool.fetchval(
            "SELECT id FROM spends WHERE user_id = $1 ORDER BY id DESC LIMIT 1", user_id
        )
        if await self.pool.fetchval("SELECT category_id FROM spends WHERE id = $1", spend_id) is None:
            category_id = (await db.list_categories(self.pool, user_id))[0]["id"]
            await self.tap(f"cat:{spend_id}:{category_id}")
        return spend_id

    async def tags_of(self, spend_id: int) -> list[str]:
        return [tag["name"] for tag in await db.tags_for_spend(self.pool, spend_id)]

    async def test_tags_are_typed_right_after_the_category(self) -> None:
        spend_id = await self.add_spend_and_categorise()
        self.assertIn("через запятую", self.bot.last)

        await self.send("вода, кофе")
        self.assertEqual(await self.tags_of(spend_id), ["вода", "кофе"])
        # Both were unknown a moment ago, so the bot created them.
        self.assertEqual(len(await db.list_tags(self.pool, await self.user_id())), 2)

        # The card keeps listening, and a name it already knows is not duplicated.
        await self.send("Кофе, курут")
        self.assertEqual(await self.tags_of(spend_id), ["вода", "кофе", "курут"])
        self.assertEqual(len(await db.list_tags(self.pool, await self.user_id())), 3)

    async def test_a_spend_typed_with_its_category_also_takes_tags(self) -> None:
        # "480 кафе" needs no category tap at all, so the card it opens must listen too.
        spend_id = await self.add_spend_and_categorise("480 кафе")
        self.assertIn("через запятую", self.bot.last)
        await self.send("вода")
        self.assertEqual(await self.tags_of(spend_id), ["вода"])

    async def test_tags_are_typed_straight_into_the_picker(self) -> None:
        spend_id = await self.add_spend_and_categorise()
        await self.tap(f"tags:{spend_id}")
        self.assertIn("через запятую", self.bot.last)

        await self.send("курут")
        self.assertEqual(await self.tags_of(spend_id), ["курут"])

    async def test_the_next_spend_is_not_swallowed_as_a_tag(self) -> None:
        first = await self.add_spend_and_categorise("480 кафе")
        await self.send("120 магазин")

        user_id = await self.user_id()
        self.assertEqual(await self.tags_of(first), [])
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM tags"), 0)
        amounts = [
            str(row["amount"])
            for row in await self.pool.fetch(
                "SELECT amount FROM spends WHERE user_id = $1 ORDER BY id", user_id
            )
        ]
        self.assertEqual(amounts, ["480.00", "120.00"])

    async def test_the_new_tags_button_accepts_a_tag_starting_with_a_digit(self) -> None:
        spend_id = await self.add_spend_and_categorise()
        await self.tap(f"tgnew:{spend_id}")
        await self.send("5 литров")
        self.assertEqual(await self.tags_of(spend_id), ["5 литров"])
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM spends"), 1)

    async def test_the_calendar_marks_days_and_opens_one(self) -> None:
        await self.add_spend_and_categorise("480 кафе")
        today = today_local(TZ_MINUTES)

        await self.send("📅 Дата")
        self.assertIn(month_name(today.year, today.month), self.bot.last)
        markup = self.bot.last_markup
        days = {
            button.callback_data: button.text
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("cd:")
        }
        # Today is bracketed and marked, since a spend has just been made.
        self.assertEqual(days[f"cd:{today.year}-{today.month}-{today.day}"], f"[{today.day}]•")
        # A day without spends carries no dot.
        earlier = [text for data, text in days.items() if not text.endswith("•")]
        self.assertTrue(earlier or today.day == 1)

        await self.tap(f"cd:{today.year}-{today.month}-{today.day}")
        self.assertIn("480", self.bot.last)

    async def test_the_calendar_refuses_a_day_that_has_not_happened(self) -> None:
        await self.send("📅 Дата")
        tomorrow = today_local(TZ_MINUTES) + timedelta(days=1)
        await self.tap(f"cd:{tomorrow.year}-{tomorrow.month}-{tomorrow.day}")
        self.assertEqual(self.bot.alerts[-1], "Этот день ещё не наступил")

    async def test_yesterday_button_reports_the_day_before(self) -> None:
        await self.send("📊 Вчера")
        yesterday = today_local(TZ_MINUTES) - timedelta(days=1)
        self.assertIn(str(yesterday.day), self.bot.last)

    async def test_every_button_on_every_screen_answers(self) -> None:
        """Walk the whole interface, tapping everything that is drawn.

        A button Telegram shows must do something. If a handler raises or simply forgets
        to answer the callback, the button spins for half a minute and the bot looks
        frozen — which is exactly the kind of dead end that is invisible in a unit test.
        Destructive buttons are pressed too: deleting the thing a later button points at
        is itself a case worth surviving.
        """
        await self.add_spend_and_categorise("480 кафе")
        await self.send("вода, кофе")
        await self.send("1470")
        for entry in ("📊 День", "📊 Вчера", "📅 Дата", "🧾 Последние", "📁 Категории", "🏷 Теги"):
            await self.send(entry)
        await self.send("/report")
        await self.send("/pending")

        queued: list[str] = []
        seen: set[str] = set()
        # Paging back through reports and days never runs out, so a few of each kind is
        # enough; what matters is that no kind of button goes untried.
        per_kind: Counter[str] = Counter()

        def kind_of(data: str) -> str:
            return data.split(":", 1)[0]

        def collect() -> None:
            for markup in self.bot.markups:
                for row in getattr(markup, "inline_keyboard", []):
                    for button in row:
                        data = button.callback_data
                        if not data or data in seen or per_kind[kind_of(data)] >= 3:
                            continue
                        seen.add(data)
                        per_kind[kind_of(data)] += 1
                        queued.append(data)

        collect()
        pressed: set[str] = set()
        while queued:
            data = queued.pop(0)
            before = len(self.bot.methods)
            await self.tap(data)
            pressed.add(kind_of(data))
            self.assertIn(
                "AnswerCallbackQuery",
                self.bot.methods[before:],
                f"кнопка {data} ничего не ответила — она будет крутиться",
            )
            collect()

        # Every kind of button the bot can draw was tapped, and every one of them replied.
        self.assertEqual(
            pressed,
            {
                "rep", "rtag", "cal", "cd", "cm", "noop",
                "cat", "catadd", "catlist", "catsum", "catdel",
                "taglist", "tagsum", "tagdel", "tags", "tg", "tgnew",
                "sp", "skip", "edit", "new", "note", "del", "delask", "delyes",
            },
        )

    async def test_the_spinner_stops_before_the_screen_is_redrawn(self) -> None:
        # The spinner on a tapped button is what reads as "the bot froze", and it stops
        # only on the answer. Whatever the redraw costs, the answer goes first.
        await self.send("1470")
        spend_id = await self.pool.fetchval("SELECT id FROM spends ORDER BY id DESC LIMIT 1")
        category_id = (await db.list_categories(self.pool, await self.user_id()))[0]["id"]

        before = len(self.bot.methods)
        await self.tap(f"cat:{spend_id}:{category_id}")
        after = self.bot.methods[before:]
        self.assertLess(
            after.index("AnswerCallbackQuery"),
            after.index("EditMessageText"),
            "карточка перерисовывается раньше, чем гаснет спиннер",
        )

    async def test_an_unedittable_message_still_answers(self) -> None:
        # Telegram refuses to edit its own message after two days. A button on an old
        # report must still do something rather than raise and spin forever.
        spend_id = await self.add_spend_and_categorise()
        self.bot.refuse_edits = True
        before = len(self.bot.methods)

        await self.tap(f"sp:{spend_id}")
        # The edit was refused, so the card arrived as a new message, and the button
        # was answered rather than left spinning.
        after = self.bot.methods[before:]
        self.assertEqual(after, ["EditMessageText", "SendMessage", "AnswerCallbackQuery"])
        self.assertIn("480", self.bot.last)

    async def test_a_broken_button_stops_spinning(self) -> None:
        # Nothing the bot draws looks like this, but a handler that throws for any
        # reason must not leave the button spinning with no explanation.
        with self.assertRaises(ValueError):
            await self.tap("cat:непонятно:1")
        self.assertEqual(self.bot.alerts[-1], "Что-то сломалось, попробуй ещё раз")

    async def test_a_menu_tap_ends_the_tag_listening(self) -> None:
        spend_id = await self.add_spend_and_categorise()
        await self.send("🧾 Последние")
        await self.send("вода")

        # The menu tap cleared the dialog, so "вода" is no longer read as a tag.
        self.assertEqual(await self.tags_of(spend_id), [])
        self.assertIn("Не понял", self.bot.last)


if __name__ == "__main__":
    unittest.main()

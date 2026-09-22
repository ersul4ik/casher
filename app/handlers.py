"""Telegram bot handlers.

Reply texts are what the user reads, so they stay in Russian.
"""

from __future__ import annotations

import csv
import html
import io
import re
from calendar import monthrange
from contextlib import suppress
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import asyncpg
from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, TelegramObject

from . import db, keyboards, notifier
from .config import Config
from .reports import (
    build_entity_summary,
    build_report,
    build_tag_report,
    day_offset,
    money,
    month_name,
    month_offset,
    today_local,
    user_tz,
)

router = Router()

MANUAL_SPEND_RE = re.compile(r"^\s*(\d+(?:[.,]\d{1,2})?)\s*(.*)$", re.DOTALL)
MAX_CATEGORY_NAME = 32
MAX_TAG_NAME = 24
MAX_TAGS_PER_MESSAGE = 5
MAX_NOTE = 200
LAST_LIMIT = 10

# Shown under a spend card so the shortcut is discoverable: no button tap needed.
TAG_INPUT_HINT = (
    "<i>Теги можно просто написать через запятую: <code>вода, кофе</code>. "
    "Знакомые подхвачу, новые заведу сам.</i>"
)


def parse_tag_names(text: str | None) -> list[str]:
    """Split a typed line into tag names: 'вода, кофе,,  Кофе ' → ['вода', 'кофе']."""
    seen: set[str] = set()
    names: list[str] = []
    for part in (text or "").split(","):
        name = part.strip()[:MAX_TAG_NAME]
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names[:MAX_TAGS_PER_MESSAGE]


class Flow(StatesGroup):
    category_name = State()
    tag_names = State()
    note_text = State()


class CallbackSafetyNet(BaseMiddleware):
    """Stop the spinner on a button even when the handler behind it fails.

    Telegram keeps a tapped button spinning until the bot answers the callback, so an
    unhandled error reads as a frozen bot and hides what actually went wrong. The error
    still propagates to be logged; the user gets told to try again.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            if isinstance(event, CallbackQuery):
                with suppress(Exception):
                    await event.answer("Что-то сломалось, попробуй ещё раз", show_alert=True)
            raise


class UserMiddleware(BaseMiddleware):
    """Create or refresh the user row and hand it to handlers as data['user']."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        # A command or a menu tap always aborts an unfinished dialog, such as typing a
        # category name. Menu buttons arrive as plain text, so without this the caption
        # itself ends up stored as the note.
        state: FSMContext | None = data.get("state")
        if state is not None and isinstance(event, Message):
            text = event.text or ""
            if text.startswith("/") or keyboards.is_menu_button(text):
                await state.clear()

        config: Config = data["config"]
        user, created = await db.get_or_create_user(
            data["pool"],
            tg_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            default_currency=config.default_currency,
            default_tz_minutes=config.default_tz_minutes,
        )
        data["user"] = user
        data["is_new_user"] = created
        return await handler(event, data)


# --- helpers -----------------------------------------------------------------


def _local(moment: datetime, user: asyncpg.Record) -> datetime:
    return moment.astimezone(user_tz(user["tz_minutes"]))


def _spend_text(
    spend: asyncpg.Record, tags: list[asyncpg.Record], user: asyncpg.Record
) -> str:
    """One card describing a spend: amount, category, tags, note, time."""
    amount = f"{money(spend['amount'])} {html.escape(spend['currency'])}"
    if spend["status"] == "ignored":
        head = f"🚫 {amount} → <b>не расход</b>"
    elif spend["category_name"]:
        head = f"✅ {amount} → <b>{html.escape(spend['category_name'])}</b>"
    else:
        head = f"💸 {amount} — <b>без категории</b>"

    lines = [head]
    if spend["merchant"]:
        lines.append(f"📍 {html.escape(spend['merchant'])}")
    if tags:
        lines.append("🏷 " + ", ".join(html.escape(tag["name"]) for tag in tags))
    if spend["note"]:
        lines.append(f"📝 {html.escape(spend['note'])}")
    lines.append(f"<i>{_local(spend['occurred_at'], user).strftime('%d.%m %H:%M')}</i>")
    return "\n".join(lines)


def _last_list_text(spends: list[asyncpg.Record], user: asyncpg.Record) -> str:
    lines = ["🧾 <b>Последние траты</b>", ""]
    for number, spend in enumerate(spends, start=1):
        when = _local(spend["occurred_at"], user).strftime("%d.%m %H:%M")
        if spend["status"] == "ignored":
            tail = "🚫 не расход"
        elif spend["category_name"]:
            tail = html.escape(spend["category_name"])
        else:
            tail = "⏳ без категории"
        lines.append(
            f"<b>{number}.</b> <code>{when}</code>  <b>{money(spend['amount'])}</b> "
            f"{html.escape(spend['currency'])} — {tail}"
        )
    lines.append("")
    lines.append("<i>Нажми номер, чтобы открыть трату: теги, описание, удаление.</i>")
    return "\n".join(lines)


async def _await_tags(state: FSMContext, spend_id: int, *, asked: bool = False) -> None:
    """Let the next typed line become tags for this spend.

    `asked` marks the state set by the "новые теги" button: there a line is tags even
    if it starts with a digit, while a card armed on its own must still let a new
    spend through — see on_tag_names.
    """
    await state.set_state(Flow.tag_names)
    await state.update_data(spend_id=spend_id, tags_asked=asked)


async def _show_spend(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    spend_id: int,
    state: FSMContext | None = None,
    spend: asyncpg.Record | None = None,
) -> bool:
    """Redraw the spend card in place. False means the spend is gone.

    With a state the card also takes typed tags, and says so. A caller that has just
    written the row passes it in: one round trip less on the tap the user waits for.
    """
    if spend is None:
        spend = await db.get_spend(pool, user["id"], spend_id)
    if spend is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return False
    tags = await db.tags_for_spend(pool, spend_id)
    text = _spend_text(spend, tags, user)
    if state is not None:
        await _await_tags(state, spend_id)
        text = f"{text}\n\n{TAG_INPUT_HINT}"
    await _safe_edit(cb, text, keyboards.spend_actions(spend_id))
    return True


async def _show_tag_picker(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    spend_id: int,
    state: FSMContext,
) -> None:
    spend = await db.get_spend(pool, user["id"], spend_id)
    if spend is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    tags = await db.list_tags(pool, user["id"])
    selected = {tag["id"] for tag in await db.tags_for_spend(pool, spend_id)}
    hint = (
        "Отметь теги кнопками или напиши их через запятую."
        if tags
        else "Тегов пока нет. Напиши первые через запятую — например: вода, кофе, курут."
    )
    await _await_tags(state, spend_id)
    await _safe_edit(
        cb,
        f"{_spend_text(spend, [], user)}\n\n{hint}",
        keyboards.tag_picker(spend_id, tags, selected),
    )


# Telegram refuses to edit a message once it is two days old, and there is no way to
# know that in advance: a report from last week looks exactly like one from a minute ago.
_UNEDITABLE = ("can't be edited", "message to edit not found", "MESSAGE_ID_INVALID")


async def _safe_edit(cb: CallbackQuery, text: str, markup=None) -> None:
    """Redraw the message behind a button, or send a new one when it cannot be redrawn.

    Without the fallback an old screen turns into a dead end: every button on it raises,
    the callback goes unanswered, and the tap looks like the bot has frozen.
    """
    message = cb.message if isinstance(cb.message, Message) else None
    if message is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            reason = str(exc)
            if "message is not modified" in reason:
                return
            if not any(known in reason for known in _UNEDITABLE):
                raise
    await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup)


def _setup_instructions(config: Config, api_token: str) -> str:
    return (
        "🔑 <b>Личный токен для шортката</b>\n\n"
        f"URL: <code>{html.escape(config.spend_url)}</code>\n"
        f"Заголовок <code>X-Token</code>: <code>{html.escape(api_token)}</code>\n\n"
        "Этот токен — как пароль: по нему трата записывается именно тебе. "
        "Если он утёк, выпусти новый командой /newtoken.\n\n"
        "Настройка шортката на iPhone описана в docs/SHORTCUT.md."
    )


# --- commands ----------------------------------------------------------------


@router.message(Command("start"))
async def cmd_start(
    msg: Message, user: asyncpg.Record, is_new_user: bool, config: Config, state: FSMContext
) -> None:
    await state.clear()
    greeting = "Привет! Это трекер трат." if is_new_user else "С возвращением."
    await msg.answer(
        f"{greeting}\n\n"
        "Как это работает: банк присылает пуш на iPhone → шорткат отправляет сумму сюда → "
        "я спрашиваю категорию кнопками → в конце дня/недели/месяца выдаю отчёт.\n\n"
        "Трату можно завести и руками — просто напиши сумму, например <code>350 кофейня</code>.\n\n"
        f"Твой chat_id: <code>{msg.chat.id}</code>\n\n"
        "Команды: /report /pending /last /cats /export /settings /token /help",
        reply_markup=keyboards.main_menu(),
    )
    if is_new_user:
        await msg.answer(_setup_instructions(config, user["api_token"]))


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "<b>Что умею</b>\n\n"
        "📊 /report — отчёты за день, неделю, месяц с переключением периодов\n"
        "/day /yesterday /week /month — сразу нужный период\n"
        "📅 /date — календарь: выбрать любое число, точка у дня значит, что были траты\n"
        "🏷 В любом отчёте есть кнопка «По тегам» — сколько ушло на воду, кофе, курут\n"
        "⏳ /pending — разметить траты, оставшиеся без категории\n"
        "🧾 /last — последние траты: открыть по номеру, поставить теги, "
        "дописать описание или удалить\n"
        "📁 /cats — категории: нажми на название, чтобы увидеть траты по ней\n"
        "🏷 /tags — то же самое по тегам\n"
        "⬇️ /export — выгрузка всех трат в CSV\n"
        "⚙️ /settings — валюта и часовой пояс\n"
        "🔑 /token — токен для шортката, /newtoken — выпустить новый\n\n"
        "Трату руками: <code>1470</code> или <code>1470 кафе</code>.",
        reply_markup=keyboards.main_menu(),
    )


@router.message(Command("token"))
async def cmd_token(msg: Message, user: asyncpg.Record, config: Config) -> None:
    await msg.answer(_setup_instructions(config, user["api_token"]))


@router.message(Command("newtoken"))
async def cmd_new_token(
    msg: Message, user: asyncpg.Record, pool: asyncpg.Pool, config: Config
) -> None:
    token = await db.rotate_token(pool, user["id"])
    await msg.answer(
        "Старый токен больше не работает — не забудь вписать новый в шорткат.\n\n"
        + _setup_instructions(config, token)
    )


@router.message(Command("settings"))
async def cmd_settings(msg: Message, user: asyncpg.Record) -> None:
    hours = user["tz_minutes"] / 60
    sign = "+" if hours >= 0 else ""
    await msg.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"Валюта по умолчанию: <b>{html.escape(user['currency'])}</b>\n"
        f"Часовой пояс: <b>UTC{sign}{hours:g}</b>\n\n"
        "Изменить: <code>/currency USD</code>, <code>/tz +6</code>",
    )


@router.message(Command("currency"))
async def cmd_currency(
    msg: Message, command: CommandObject, user: asyncpg.Record, pool: asyncpg.Pool
) -> None:
    value = (command.args or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", value):
        await msg.answer("Нужен трёхбуквенный код, например: <code>/currency KGS</code>")
        return
    await db.update_settings(pool, user["id"], currency=value)
    await msg.answer(f"Валюта по умолчанию: <b>{value}</b>")


@router.message(Command("tz"))
async def cmd_tz(
    msg: Message, command: CommandObject, user: asyncpg.Record, pool: asyncpg.Pool
) -> None:
    raw = (command.args or "").strip().replace("UTC", "")
    try:
        hours = float(raw)
    except ValueError:
        await msg.answer("Смещение в часах, например: <code>/tz +6</code>")
        return
    if not -12 <= hours <= 14:
        await msg.answer("Смещение должно быть от -12 до +14.")
        return
    await db.update_settings(pool, user["id"], tz_minutes=int(hours * 60))
    await msg.answer(f"Часовой пояс: <b>UTC{'+' if hours >= 0 else ''}{hours:g}</b>")


# --- reports -----------------------------------------------------------------


async def _send_report(
    msg: Message, pool: asyncpg.Pool, user: asyncpg.Record, kind: str, offset: int = 0
) -> None:
    text = await build_report(pool, user, kind, offset)
    await msg.answer(text, reply_markup=keyboards.report_nav(kind, offset))


@router.message(Command("report"))
async def cmd_report(msg: Message) -> None:
    await msg.answer("За какой период?", reply_markup=keyboards.report_menu())


@router.message(Command("day"))
@router.message(F.text == keyboards.BTN_DAY)
async def cmd_day(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    await _send_report(msg, pool, user, "day")


@router.message(Command("yesterday"))
@router.message(F.text == keyboards.BTN_YESTERDAY)
async def cmd_yesterday(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    await _send_report(msg, pool, user, "day", 1)


@router.message(Command("week"))
@router.message(F.text == keyboards.BTN_WEEK)
async def cmd_week(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    await _send_report(msg, pool, user, "week")


@router.message(Command("month"))
@router.message(F.text == keyboards.BTN_MONTH)
async def cmd_month(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    await _send_report(msg, pool, user, "month")


@router.callback_query(F.data.startswith("rep:"))
async def cb_report(cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    _, kind, raw_offset = cb.data.split(":", 2)
    offset = int(raw_offset)
    text = await build_report(pool, user, kind, offset)
    await _safe_edit(cb, text, keyboards.report_nav(kind, offset))
    await cb.answer()


@router.callback_query(F.data.startswith("rtag:"))
async def cb_tag_report(cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    _, kind, raw_offset = cb.data.split(":", 2)
    offset = int(raw_offset)
    text = await build_tag_report(pool, user, kind, offset)
    await _safe_edit(cb, text, keyboards.report_nav(kind, offset, by_tag=True))
    await cb.answer()


# --- calendar ----------------------------------------------------------------


CALENDAR_HINT = (
    "Выбери день — покажу траты за него.\n"
    "<i>Точка у числа значит, что в этот день что-то потрачено.</i>"
)


def _shift_month(year: int, month: int, step: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + step
    return total // 12, total % 12 + 1


async def _calendar(
    pool: asyncpg.Pool, user: asyncpg.Record, year: int, month: int
) -> tuple[str, Any]:
    tz_minutes = user["tz_minutes"]
    tz = user_tz(tz_minutes)
    today = today_local(tz_minutes)
    last_day = monthrange(year, month)[1]

    start_local = datetime(year, month, 1, tzinfo=tz)
    end_local = datetime(*_shift_month(year, month, 1), 1, tzinfo=tz)
    marked = await db.days_with_spends(
        pool,
        user["id"],
        start=start_local.astimezone(timezone.utc),
        end=end_local.astimezone(timezone.utc),
        tz_minutes=tz_minutes,
    )

    current = (year, month) == (today.year, today.month)
    title = month_name(year, month)
    markup = keyboards.calendar_grid(
        year,
        month,
        title=title,
        marked=marked,
        today=today.day if current else None,
        last_day=last_day,
        # Tomorrow has nothing to show, so the rest of the month is not tappable.
        max_day=today.day if current else None,
        prev_month=_shift_month(year, month, -1),
        next_month=None if current else _shift_month(year, month, 1),
    )
    return f"📅 <b>{title}</b>\n\n{CALENDAR_HINT}", markup


def _parse_month(raw: str, user: asyncpg.Record) -> tuple[int, int] | None:
    if raw == "now":
        today = today_local(user["tz_minutes"])
        return today.year, today.month
    try:
        year, month = (int(part) for part in raw.split("-", 1))
    except ValueError:
        return None
    return (year, month) if 1 <= month <= 12 and 2000 <= year <= 2100 else None


@router.message(Command("date"))
@router.message(F.text == keyboards.BTN_DATE)
async def cmd_calendar(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    text, markup = await _calendar(pool, user, *_parse_month("now", user))
    await msg.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("cal:"))
async def cb_calendar(cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    month = _parse_month(cb.data.split(":", 1)[1], user)
    if month is None:
        await cb.answer("Не разобрал месяц", show_alert=True)
        return
    text, markup = await _calendar(pool, user, *month)
    await _safe_edit(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("cd:"))
async def cb_calendar_day(cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    try:
        year, month, day = (int(part) for part in cb.data.split(":", 1)[1].split("-"))
        picked = date(year, month, day)
    except ValueError:
        await cb.answer("Не разобрал дату", show_alert=True)
        return

    offset = day_offset(picked, user["tz_minutes"])
    if offset < 0:
        await cb.answer("Этот день ещё не наступил", show_alert=True)
        return
    text = await build_report(pool, user, "day", offset)
    await _safe_edit(cb, text, keyboards.report_nav("day", offset))
    await cb.answer()


@router.callback_query(F.data.startswith("cm:"))
async def cb_calendar_month(
    cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    month = _parse_month(cb.data.split(":", 1)[1], user)
    if month is None:
        await cb.answer("Не разобрал месяц", show_alert=True)
        return
    offset = month_offset(*month, user["tz_minutes"])
    if offset < 0:
        await cb.answer("Этот месяц ещё не наступил", show_alert=True)
        return
    text = await build_report(pool, user, "month", offset)
    await _safe_edit(cb, text, keyboards.report_nav("month", offset))
    await cb.answer()


# --- tagging spends ----------------------------------------------------------


@router.message(Command("pending"))
@router.message(F.text == keyboards.BTN_PENDING)
async def cmd_pending(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    rows = await db.list_pending(pool, user["id"])
    if not rows:
        await msg.answer("Всё размечено 👌")
        return
    categories = await db.list_categories(pool, user["id"])
    for spend in rows:
        when = _local(spend["occurred_at"], user).strftime("%d.%m %H:%M")
        suggested_id = await db.suggest_category(
            pool, user["id"], merchant=spend["merchant"], amount=spend["amount"]
        )
        suggested_name = next((c["name"] for c in categories if c["id"] == suggested_id), None)
        await msg.answer(
            f"{notifier.spend_prompt(spend, suggested_name)}\n<i>{when}</i>",
            reply_markup=keyboards.category_picker(spend["id"], categories, suggested_id),
        )


@router.message(Command("last"))
@router.message(F.text == keyboards.BTN_LAST)
async def cmd_last(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    rows = await db.last_spends(pool, user["id"], LAST_LIMIT)
    if not rows:
        await msg.answer("Трат пока нет.")
        return
    await msg.answer(_last_list_text(rows, user), reply_markup=keyboards.spend_list(rows))


@router.callback_query(F.data.startswith("cat:"))
async def cb_pick_category(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    _, raw_spend, raw_category = cb.data.split(":", 2)
    spend = await db.set_category(pool, user["id"], int(raw_spend), int(raw_category))
    if spend is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    # Tagging usually follows the category, so the card is already listening for tags.
    await _show_spend(cb, pool, user, spend["id"], state, spend)
    await cb.answer(spend["category_name"])


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    spend_id = int(cb.data.split(":", 1)[1])
    spend = await db.mark_ignored(pool, user["id"], spend_id)
    if spend is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    # Nothing left to tag here, so the card stops listening for tags.
    await state.clear()
    await _show_spend(cb, pool, user, spend_id, spend=spend)
    await cb.answer("Не расход")


@router.callback_query(F.data.startswith("sp:"))
async def cb_open_spend(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    await _show_spend(cb, pool, user, int(cb.data.split(":", 1)[1]), state)
    await cb.answer()


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    spend_id = int(cb.data.split(":", 1)[1])
    spend = await db.reopen_spend(pool, user["id"], spend_id)
    if spend is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    # The card is gone until a category is picked again; typed text is not tags now.
    await state.clear()
    categories = await db.list_categories(pool, user["id"])
    await _safe_edit(
        cb, notifier.spend_prompt(spend), keyboards.category_picker(spend_id, categories)
    )
    await cb.answer()




@router.callback_query(F.data.startswith("del:"))
async def cb_delete_untagged(
    cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    """Deleting a spend that has no category yet — no confirmation needed."""
    spend_id = int(cb.data.split(":", 1)[1])
    if await db.delete_spend(pool, user["id"], spend_id):
        await _safe_edit(cb, "🗑 Запись удалена")
        await cb.answer("Удалено")
        return
    await cb.answer("Запись не найдена", show_alert=True)


@router.callback_query(F.data.startswith("delask:"))
async def cb_delete_ask(cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    spend_id = int(cb.data.split(":", 1)[1])
    spend = await db.get_spend(pool, user["id"], spend_id)
    if spend is None:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await _safe_edit(
        cb,
        f"Удалить эту трату?\n\n{_spend_text(spend, [], user)}",
        keyboards.confirm_delete(spend_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("delyes:"))
async def cb_delete_confirmed(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    spend_id = int(cb.data.split(":", 1)[1])
    # There is no card left to type tags into.
    await state.clear()
    if await db.delete_spend(pool, user["id"], spend_id):
        await _safe_edit(cb, "🗑 Трата удалена")
        await cb.answer("Удалено")
    else:
        await cb.answer("Запись не найдена", show_alert=True)


# --- tags --------------------------------------------------------------------


@router.callback_query(F.data.startswith("tags:"))
async def cb_tags(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    await _show_tag_picker(cb, pool, user, int(cb.data.split(":", 1)[1]), state)
    await cb.answer()


@router.callback_query(F.data.startswith("tg:"))
async def cb_toggle_tag(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    _, raw_spend, raw_tag = cb.data.split(":", 2)
    spend_id = int(raw_spend)
    added = await db.toggle_tag(pool, user["id"], spend_id, int(raw_tag))
    if added is None:
        await cb.answer("Тег не найден", show_alert=True)
        return
    await _show_tag_picker(cb, pool, user, spend_id, state)
    await cb.answer("Отмечен" if added else "Снят")


@router.callback_query(F.data.startswith("tgnew:"))
async def cb_new_tags(cb: CallbackQuery, state: FSMContext) -> None:
    spend_id = int(cb.data.split(":", 1)[1])
    await _await_tags(state, spend_id, asked=True)
    await cb.message.answer(
        "Какие теги добавить? Можно несколько через запятую:\n"
        "<code>вода, кофе, курут</code>\n"
        "Отменить — нажать кнопку меню."
    )
    await cb.answer()


@router.message(StateFilter(Flow.tag_names))
async def on_tag_names(
    msg: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    config: Config,
) -> None:
    data = await state.get_data()
    text = (msg.text or "").strip()

    # The card listens for tags on its own, so a line that reads like a new spend
    # ("350 кофейня") has to stay a spend. After the "новые теги" button it does not:
    # there the answer was asked for, and a tag may well start with a number.
    if not data.get("tags_asked") and MANUAL_SPEND_RE.match(text):
        await state.clear()
        await on_plain_text(msg, state, pool, user, config)
        return

    names = parse_tag_names(text)
    if not names:
        await msg.answer("Не разобрал теги. Пример: <code>вода, кофе</code>")
        return

    spend_id = data["spend_id"]
    await db.attach_tags_by_name(pool, user["id"], spend_id, names)

    spend = await db.get_spend(pool, user["id"], spend_id)
    if spend is None:
        await state.clear()
        await msg.answer("Теги сохранил, но саму трату уже не нашёл.")
        return
    tags = await db.tags_for_spend(pool, spend_id)
    # Still listening: more tags can follow without tapping anything.
    await _await_tags(state, spend_id)
    await msg.answer(
        _spend_text(spend, tags, user), reply_markup=keyboards.spend_actions(spend_id)
    )


@router.callback_query(F.data.startswith("note:"))
async def cb_note(cb: CallbackQuery, state: FSMContext) -> None:
    spend_id = int(cb.data.split(":", 1)[1])
    await state.set_state(Flow.note_text)
    await state.update_data(spend_id=spend_id)
    await cb.message.answer(
        "Напиши описание траты — что именно купил.\n"
        "Стереть прежнее — отправить <code>-</code>, отменить — нажать кнопку меню."
    )
    await cb.answer()


@router.message(StateFilter(Flow.note_text))
async def on_note_text(
    msg: Message, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    data = await state.get_data()
    await state.clear()
    text = (msg.text or "").strip()[:MAX_NOTE]
    spend = await db.set_note(pool, user["id"], data["spend_id"], None if text == "-" else text)
    if spend is None:
        await msg.answer("Трату уже не нашёл — возможно, она удалена.")
        return
    spend = await db.get_spend(pool, user["id"], data["spend_id"])
    tags = await db.tags_for_spend(pool, data["spend_id"])
    await msg.answer(
        _spend_text(spend, tags, user), reply_markup=keyboards.spend_actions(spend["id"])
    )


@router.callback_query(F.data.startswith("new:"))
async def cb_new_category(cb: CallbackQuery, state: FSMContext) -> None:
    spend_id = int(cb.data.split(":", 1)[1])
    await state.set_state(Flow.category_name)
    await state.update_data(spend_id=spend_id)
    await cb.message.answer("Название новой категории? Например: Такси")
    await cb.answer()


# --- categories --------------------------------------------------------------


CATEGORY_LIST_TEXT = (
    "📁 <b>Категории</b>\n\nНажми на название — покажу, сколько на неё ушло. "
    "Корзина прячет категорию из кнопок, уже размеченные траты остаются на месте."
)
TAG_LIST_TEXT = (
    "🏷 <b>Теги</b>\n\nНажми на тег — покажу, сколько на него ушло. "
    "Корзина удаляет тег вместе с его отметками на тратах, сами траты остаются."
)


EMPTY_LIST_TEXT = {
    "cat": "📁 <b>Категории</b>\n\nНе осталось ни одной — добавь первую кнопкой ниже.",
    "tag": "🏷 <b>Теги</b>\n\nТегов больше нет. Новые ставятся под тратой, кнопкой «🏷 Теги».",
}


async def _entity_list(pool: asyncpg.Pool, user: asyncpg.Record, kind: str):
    if kind == "tag":
        entities = await db.list_tags(pool, user["id"], limit=50)
        full = TAG_LIST_TEXT
    else:
        entities = await db.list_categories(pool, user["id"])
        full = CATEGORY_LIST_TEXT
    return (full if entities else EMPTY_LIST_TEXT[kind]), entities


@router.message(Command("cats"))
@router.message(F.text == keyboards.BTN_CATEGORIES)
async def cmd_categories(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    text, categories = await _entity_list(pool, user, "cat")
    await msg.answer(text, reply_markup=keyboards.entity_manager("cat", categories))


@router.message(Command("tags"))
@router.message(F.text == keyboards.BTN_TAGS)
async def cmd_tags(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    text, tags = await _entity_list(pool, user, "tag")
    if not tags:
        await msg.answer(
            "Тегов пока нет. Они ставятся под тратой кнопкой «🏷 Теги» — "
            "например, <i>вода</i>, <i>кофе</i>, <i>курут</i>."
        )
        return
    await msg.answer(text, reply_markup=keyboards.entity_manager("tag", tags))


@router.callback_query(F.data.in_({"catlist", "taglist"}))
async def cb_entity_list(cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    kind = "tag" if cb.data.startswith("tag") else "cat"
    text, entities = await _entity_list(pool, user, kind)
    await _safe_edit(cb, text, keyboards.entity_manager(kind, entities))
    await cb.answer()


@router.callback_query(F.data.startswith(("catsum:", "tagsum:")))
async def cb_entity_summary(
    cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    kind, raw_id = cb.data.split("sum:", 1)
    text = await build_entity_summary(pool, user, kind, int(raw_id))
    if text is None:
        await cb.answer("Не нашёл", show_alert=True)
        return
    await _safe_edit(cb, text, keyboards.entity_summary_nav(kind, int(raw_id)))
    await cb.answer()


@router.callback_query(F.data == "catadd")
async def cb_category_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.category_name)
    await state.update_data(spend_id=None)
    await cb.message.answer("Название новой категории?")
    await cb.answer()


@router.callback_query(F.data.startswith(("catdel:", "tagdel:")))
async def cb_entity_delete(cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    kind, raw_id = cb.data.split("del:", 1)
    entity_id = int(raw_id)
    if kind == "tag":
        name = await db.delete_tag(pool, user["id"], entity_id)
    else:
        name = await db.archive_category(pool, user["id"], entity_id)

    text, entities = await _entity_list(pool, user, kind)
    await _safe_edit(cb, text, keyboards.entity_manager(kind, entities))
    await cb.answer(f"Убрал: {name}" if name else "Не нашёл")


@router.message(StateFilter(Flow.category_name))
async def on_category_name(
    msg: Message, state: FSMContext, pool: asyncpg.Pool, user: asyncpg.Record
) -> None:
    name = (msg.text or "").strip()[:MAX_CATEGORY_NAME]
    if not name:
        await msg.answer("Пустое название не подойдёт. Ещё раз?")
        return
    data = await state.get_data()
    await state.clear()

    category = await db.add_category(pool, user["id"], name)
    spend_id = data.get("spend_id")
    if spend_id is None:
        await msg.answer(f"Добавил категорию <b>{html.escape(category['name'])}</b>.")
        return

    spend = await db.set_category(pool, user["id"], spend_id, category["id"])
    if spend is None:
        await msg.answer(
            f"Категорию <b>{html.escape(category['name'])}</b> добавил, "
            "но трату уже не нашёл — размечу через /pending."
        )
        return
    tags = await db.tags_for_spend(pool, spend_id)
    await _await_tags(state, spend_id)
    await msg.answer(
        f"{_spend_text(spend, tags, user)}\n\nКатегория добавлена в кнопки.\n{TAG_INPUT_HINT}",
        reply_markup=keyboards.spend_actions(spend_id),
    )


# --- export ------------------------------------------------------------------


@router.message(Command("export"))
@router.message(F.text == keyboards.BTN_EXPORT)
async def cmd_export(msg: Message, pool: asyncpg.Pool, user: asyncpg.Record) -> None:
    rows = await db.export_rows(pool, user["id"])
    if not rows:
        await msg.answer("Экспортировать пока нечего.")
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "datetime", "amount", "currency", "category", "merchant",
            "tags", "note", "status", "source", "raw",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                _local(row["occurred_at"], user).strftime("%Y-%m-%d %H:%M:%S"),
                f"{row['amount']:.2f}",
                row["currency"],
                row["category"],
                row["merchant"],
                row["tags"],
                row["note"],
                row["status"],
                row["source"],
                row["raw"],
            ]
        )
    # utf-8-sig: without the BOM Excel mangles Cyrillic text.
    payload = buffer.getvalue().encode("utf-8-sig")
    await msg.answer_document(
        BufferedInputFile(payload, filename=f"spends_{msg.from_user.id}.csv"),
        caption=f"{len(rows)} записей",
    )


# --- admin summary -----------------------------------------------------------


@router.message(Command("stats"))
async def cmd_stats(msg: Message, pool: asyncpg.Pool, config: Config) -> None:
    if msg.from_user.id not in config.admin_ids:
        return
    stats = await db.admin_stats(pool)
    lines = [
        "🛠 <b>Сводка по сервису</b>",
        "",
        f"Пользователей: <b>{stats['users_total']}</b> (активных за 7 дней: {stats['users_active']})",
        f"Трат всего: <b>{stats['spends_total']}</b>, без категории: {stats['pending_total']}",
    ]
    if stats["by_currency"]:
        lines += ["", "<b>Обороты</b>"]
        for row in stats["by_currency"]:
            lines.append(f"{html.escape(row['currency'])}: {money(row['total'])} ({row['n']} шт)")
    if stats["top_users"]:
        lines += ["", "<b>Топ по числу трат</b>"]
        for row in stats["top_users"]:
            label = row["username"] or row["first_name"] or row["tg_id"]
            lines.append(f"{html.escape(str(label))} — {row['n']}")
    await msg.answer("\n".join(lines))


# --- manual entry ------------------------------------------------------------


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.message(F.text)
async def on_plain_text(
    msg: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    config: Config,
) -> None:
    match = MANUAL_SPEND_RE.match(msg.text)
    if not match:
        await msg.answer("Не понял. Напиши сумму, например <code>350 кофейня</code>, или /help")
        return

    try:
        amount = Decimal(match.group(1).replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        await msg.answer("Не разобрал сумму. Пример: <code>350 кофейня</code>")
        return
    if amount <= 0:
        await msg.answer("Сумма должна быть больше нуля.")
        return

    hint = match.group(2).strip()
    category = None
    if hint:
        for candidate in await db.list_categories(pool, user["id"]):
            if candidate["name"].lower().startswith(hint.lower()):
                category = candidate
                break

    spend = await db.create_spend(
        pool,
        user_id=user["id"],
        amount=amount,
        currency=user["currency"],
        raw=msg.text.strip(),
        source="manual",
        category_id=category["id"] if category else None,
    )
    if category:
        stored = await db.get_spend(pool, user["id"], spend["id"])
        await _await_tags(state, spend["id"])
        await msg.answer(
            f"{_spend_text(stored, [], user)}\n\n{TAG_INPUT_HINT}",
            reply_markup=keyboards.spend_actions(spend["id"]),
        )
    else:
        await notifier.ask_category(msg.bot, pool, user, spend)

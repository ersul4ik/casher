"""Bot keyboards: categories under a spend, report navigation, the bottom menu.

Button captions are user-facing, so they stay in Russian.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import asyncpg
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .reports import PERIOD_TITLES

BTN_DAY = "📊 День"
BTN_YESTERDAY = "📊 Вчера"
BTN_WEEK = "📊 Неделя"
BTN_MONTH = "📊 Месяц"
BTN_DATE = "📅 Дата"
BTN_PENDING = "⏳ Без категории"
BTN_CATEGORIES = "📁 Категории"
BTN_TAGS = "🏷 Теги"
BTN_LAST = "🧾 Последние"
# Off the keyboard since it was needed about once a year, but /export still works — and
# so does this caption, because Telegram keeps showing the old keyboard until a new one
# arrives, and someone will tap it tomorrow.
BTN_EXPORT = "⬇️ CSV"

MENU_BUTTONS = frozenset(
    {
        BTN_DAY,
        BTN_YESTERDAY,
        BTN_WEEK,
        BTN_MONTH,
        BTN_DATE,
        BTN_PENDING,
        BTN_CATEGORIES,
        BTN_TAGS,
        BTN_LAST,
        BTN_EXPORT,
    }
)


def is_menu_button(text: str | None) -> bool:
    """Menu taps arrive as ordinary text, so a dialog waiting for input must let them through."""
    return bool(text) and text.strip() in MENU_BUTTONS


def main_menu() -> ReplyKeyboardMarkup:
    """Three rows: what happened just now, longer periods, and the reference lists."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_DAY),
                KeyboardButton(text=BTN_YESTERDAY),
                KeyboardButton(text=BTN_DATE),
            ],
            [
                KeyboardButton(text=BTN_WEEK),
                KeyboardButton(text=BTN_MONTH),
                KeyboardButton(text=BTN_LAST),
            ],
            [
                KeyboardButton(text=BTN_PENDING),
                KeyboardButton(text=BTN_CATEGORIES),
                KeyboardButton(text=BTN_TAGS),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Сумма и категория, например: 350 кофейня",
    )


def category_picker(
    spend_id: int, categories: list[asyncpg.Record], suggested_id: int | None = None
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    # The guess gets a wide button of its own on top; the rest keep their usual order.
    suggested = next((c for c in categories if c["id"] == suggested_id), None)
    if suggested is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⭐ {suggested['name']}",
                    callback_data=f"cat:{spend_id}:{suggested['id']}",
                )
            ]
        )
        categories = [c for c in categories if c["id"] != suggested_id]

    for category in categories:
        row.append(
            InlineKeyboardButton(
                text=category["name"], callback_data=f"cat:{spend_id}:{category['id']}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="➕ Новая категория", callback_data=f"new:{spend_id}")])
    rows.append(
        [
            InlineKeyboardButton(text="🚫 Не расход", callback_data=f"skip:{spend_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{spend_id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def spend_actions(spend_id: int) -> InlineKeyboardMarkup:
    """What can still be done to a spend once its category is set."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏷 Теги", callback_data=f"tags:{spend_id}"),
                InlineKeyboardButton(text="📝 Описание", callback_data=f"note:{spend_id}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Категория", callback_data=f"edit:{spend_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delask:{spend_id}"),
            ],
        ]
    )


def tag_picker(
    spend_id: int, tags: list[asyncpg.Record], selected: set[int]
) -> InlineKeyboardMarkup:
    """Tags toggle on and off; a checkmark shows what is already attached."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for tag in tags:
        mark = "✅ " if tag["id"] in selected else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{tag['name']}", callback_data=f"tg:{spend_id}:{tag['id']}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="➕ Новые теги", callback_data=f"tgnew:{spend_id}")]
    )
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"sp:{spend_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duplicate_choice(spend_id: int, amount: Decimal) -> InlineKeyboardMarkup:
    """Offered when the same amount arrives twice in a row: one of them, or both."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Она же", callback_data=f"sp:{spend_id}"),
                InlineKeyboardButton(text="➕ Ещё одна такая", callback_data=f"dup:{amount}"),
            ]
        ]
    )


def confirm_delete(spend_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"delyes:{spend_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"sp:{spend_id}"),
            ]
        ]
    )


def spend_list(spends: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    """Numbered buttons matching the lines of the /last listing."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for number, spend in enumerate(spends, start=1):
        row.append(InlineKeyboardButton(text=str(number), callback_data=f"sp:{spend['id']}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_nav(kind: str, offset: int, *, by_tag: bool = False) -> InlineKeyboardMarkup:
    prefix = "rtag" if by_tag else "rep"
    nav = [
        InlineKeyboardButton(text="←", callback_data=f"{prefix}:{kind}:{offset + 1}"),
        InlineKeyboardButton(text=PERIOD_TITLES[kind], callback_data="noop"),
    ]
    if offset > 0:
        nav.append(
            InlineKeyboardButton(text="→", callback_data=f"{prefix}:{kind}:{offset - 1}")
        )

    switch = [
        InlineKeyboardButton(text=title, callback_data=f"{prefix}:{other}:0")
        for other, title in PERIOD_TITLES.items()
        if other != kind
    ]
    if by_tag:
        mode = [InlineKeyboardButton(text="📊 По категориям", callback_data=f"rep:{kind}:{offset}")]
    else:
        mode = [InlineKeyboardButton(text="🏷 По тегам", callback_data=f"rtag:{kind}:{offset}")]
    return InlineKeyboardMarkup(inline_keyboard=[nav, switch, mode])


def report_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="rep:day:0"),
                InlineKeyboardButton(text="Неделя", callback_data="rep:week:0"),
                InlineKeyboardButton(text="Месяц", callback_data="rep:month:0"),
            ],
            [
                InlineKeyboardButton(text="Вчера", callback_data="rep:day:1"),
                InlineKeyboardButton(text="Прошлая неделя", callback_data="rep:week:1"),
                InlineKeyboardButton(text="Прошлый месяц", callback_data="rep:month:1"),
            ],
            [InlineKeyboardButton(text="📅 Выбрать дату", callback_data="cal:now")],
        ]
    )


# A calendar cell that is not a day: Telegram needs some text, and a dim dot keeps the
# grid aligned without reading as a number.
CALENDAR_BLANK = "·"
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def calendar_grid(
    year: int,
    month: int,
    *,
    title: str,
    marked: set[int],
    today: int | None,
    last_day: int,
    max_day: int | None,
    prev_month: tuple[int, int] | None,
    next_month: tuple[int, int] | None,
) -> InlineKeyboardMarkup:
    """A month of tappable days.

    ``marked`` are the days that have spends — the whole point of showing a grid rather
    than asking for a date. ``max_day`` cuts off the future: the current month has no
    tomorrow worth tapping. ``last_day`` is how long the month is, and the leading blanks
    line the first day up under its weekday, counting from Monday.
    """
    blank = InlineKeyboardButton(text=CALENDAR_BLANK, callback_data="noop")

    nav = [
        InlineKeyboardButton(
            text="‹", callback_data=f"cal:{prev_month[0]}-{prev_month[1]}"
        )
        if prev_month
        else blank,
        InlineKeyboardButton(text=title, callback_data=f"cm:{year}-{month}"),
        InlineKeyboardButton(
            text="›", callback_data=f"cal:{next_month[0]}-{next_month[1]}"
        )
        if next_month
        else blank,
    ]
    rows = [nav, [InlineKeyboardButton(text=name, callback_data="noop") for name in WEEKDAYS]]

    # Monday of the week the 1st falls into, as a day number that may be negative.
    first_weekday = date(year, month, 1).weekday()
    cells: list[InlineKeyboardButton] = [blank] * first_weekday
    for day in range(1, last_day + 1):
        # The rest of the current month is still ahead; drawing it as dead cells would
        # only add empty rows, so the grid simply ends at today.
        if max_day is not None and day > max_day:
            break
        caption = f"[{day}]" if day == today else str(day)
        if day in marked:
            caption += "•"
        cells.append(InlineKeyboardButton(text=caption, callback_data=f"cd:{year}-{month}-{day}"))
    while len(cells) % 7:
        cells.append(blank)

    rows += [cells[i : i + 7] for i in range(0, len(cells), 7)]
    rows.append([InlineKeyboardButton(text="📊 Месяц целиком", callback_data=f"cm:{year}-{month}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def entity_manager(kind: str, entities: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    """The list of categories or tags: tap a name for its totals, the bin to remove it."""
    rows = [
        [
            InlineKeyboardButton(
                text=entity["name"], callback_data=f"{kind}sum:{entity['id']}"
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"{kind}del:{entity['id']}"),
        ]
        for entity in entities
    ]
    if kind == "cat":
        rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data="catadd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def entity_summary_nav(kind: str, entity_id: int) -> InlineKeyboardMarkup:
    back = "📁 К категориям" if kind == "cat" else "🏷 К тегам"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=back, callback_data=f"{kind}list"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"{kind}del:{entity_id}"),
            ]
        ]
    )

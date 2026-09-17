"""Bot keyboards: categories under a spend, report navigation, the bottom menu.

Button captions are user-facing, so they stay in Russian.
"""

from __future__ import annotations

import asyncpg
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .reports import PERIOD_TITLES

BTN_DAY = "📊 День"
BTN_WEEK = "📊 Неделя"
BTN_MONTH = "📊 Месяц"
BTN_PENDING = "⏳ Без категории"
BTN_CATEGORIES = "📁 Категории"
BTN_LAST = "🧾 Последние"
BTN_EXPORT = "⬇️ CSV"

MENU_BUTTONS = frozenset(
    {BTN_DAY, BTN_WEEK, BTN_MONTH, BTN_PENDING, BTN_CATEGORIES, BTN_LAST, BTN_EXPORT}
)


def is_menu_button(text: str | None) -> bool:
    """Menu taps arrive as ordinary text, so a dialog waiting for input must let them through."""
    return bool(text) and text.strip() in MENU_BUTTONS


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DAY), KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_MONTH)],
            [KeyboardButton(text=BTN_PENDING), KeyboardButton(text=BTN_LAST)],
            [KeyboardButton(text=BTN_CATEGORIES), KeyboardButton(text=BTN_EXPORT)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Сумма и категория, например: 350 кофейня",
    )


def category_picker(spend_id: int, categories: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
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
        ]
    )


def categories_manager(categories: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=category["name"], callback_data="noop"),
            InlineKeyboardButton(text="🗑", callback_data=f"catdel:{category['id']}"),
        ]
        for category in categories
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data="catadd")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

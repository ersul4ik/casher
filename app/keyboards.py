"""Клавиатуры бота: категории под тратой, навигация по отчётам, нижнее меню."""

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


def after_pick(spend_id: int) -> InlineKeyboardMarkup:
    """Под схлопнутой тратой оставляем возможность переразметить её."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{spend_id}")]
        ]
    )


def report_nav(kind: str, offset: int) -> InlineKeyboardMarkup:
    nav = [
        InlineKeyboardButton(text="←", callback_data=f"rep:{kind}:{offset + 1}"),
        InlineKeyboardButton(text=PERIOD_TITLES[kind], callback_data="noop"),
    ]
    if offset > 0:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"rep:{kind}:{offset - 1}"))

    switch = [
        InlineKeyboardButton(text=title, callback_data=f"rep:{other}:0")
        for other, title in PERIOD_TITLES.items()
        if other != kind
    ]
    return InlineKeyboardMarkup(inline_keyboard=[nav, switch])


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

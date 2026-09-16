"""Отправка вопроса «куда записать?» — общая для веб-хука и ручного ввода."""

from __future__ import annotations

import html

import asyncpg
from aiogram import Bot

from . import db, keyboards
from .reports import money


def spend_prompt(spend: asyncpg.Record) -> str:
    return (
        f"💸 <b>Новая трата: {money(spend['amount'])} {html.escape(spend['currency'])}</b>\n"
        "Куда записать?"
    )


async def ask_category(
    bot: Bot, pool: asyncpg.Pool, user: asyncpg.Record, spend: asyncpg.Record
) -> None:
    categories = await db.list_categories(pool, user["id"])
    message = await bot.send_message(
        user["tg_id"],
        spend_prompt(spend),
        reply_markup=keyboards.category_picker(spend["id"], categories),
    )
    await db.attach_message_id(pool, spend["id"], message.message_id)

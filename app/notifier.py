"""Asking "which category?" — shared by the webhook and by manual entry."""

from __future__ import annotations

import html

import asyncpg
from aiogram import Bot

from . import db, keyboards
from .reports import money


def spend_prompt(spend: asyncpg.Record, suggested_name: str | None = None) -> str:
    lines = [
        f"💸 <b>Новая трата: {money(spend['amount'])} {html.escape(spend['currency'])}</b>"
    ]
    if spend["merchant"]:
        lines.append(f"📍 {html.escape(spend['merchant'])}")
    if suggested_name:
        lines.append(f"Обычно это <b>{html.escape(suggested_name)}</b> — подтвердить?")
    else:
        lines.append("Куда записать?")
    return "\n".join(lines)


async def ask_category(
    bot: Bot, pool: asyncpg.Pool, user: asyncpg.Record, spend: asyncpg.Record
) -> None:
    categories = await db.list_categories(pool, user["id"])
    suggested_id = await db.suggest_category(
        pool, user["id"], merchant=spend["merchant"], amount=spend["amount"]
    )
    suggested_name = next(
        (c["name"] for c in categories if c["id"] == suggested_id), None
    )
    message = await bot.send_message(
        user["tg_id"],
        spend_prompt(spend, suggested_name),
        reply_markup=keyboards.category_picker(spend["id"], categories, suggested_id),
    )
    await db.attach_message_id(pool, spend["id"], message.message_id)

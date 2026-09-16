"""Доступ к Postgres: пул соединений и все запросы трекера."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_CATEGORIES = ("Кафе", "Обед", "Магазин", "Кофейня")

# Параметры строки подключения, которые кладут psycopg/Neon, но не понимает asyncpg.
_UNSUPPORTED_DSN_PARAMS = ("channel_binding", "gssencmode", "target_session_attrs")


def normalize_dsn(dsn: str) -> tuple[str, dict[str, Any]]:
    """Приводит DSN к тому, что понимает asyncpg.

    Neon и Supabase выдают строку в формате psycopg: со схемой ``postgresql+asyncpg``,
    с ``channel_binding=require`` и иногда с ``pgbouncer=true``. asyncpg на первых двух
    падает, а с pgbouncer требует отключённого кеша подготовленных выражений.
    """
    scheme, netloc, path, query, fragment = urlsplit(dsn.strip())
    if "+" in scheme:  # postgresql+asyncpg -> postgresql
        scheme = scheme.split("+", 1)[0]

    params = dict(parse_qsl(query, keep_blank_values=True))
    extra: dict[str, Any] = {}
    if params.pop("pgbouncer", "").lower() == "true":
        extra["statement_cache_size"] = 0
    for name in _UNSUPPORTED_DSN_PARAMS:
        params.pop(name, None)

    return urlunsplit((scheme, netloc, path, urlencode(params), fragment)), extra


async def create_pool(dsn: str) -> asyncpg.Pool:
    clean_dsn, extra = normalize_dsn(dsn)
    return await asyncpg.create_pool(clean_dsn, min_size=1, max_size=5, **extra)


async def apply_schema(pool: asyncpg.Pool) -> None:
    await pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


# --- пользователи ------------------------------------------------------------


def new_api_token() -> str:
    return secrets.token_urlsafe(24)


async def get_or_create_user(
    pool: asyncpg.Pool,
    *,
    tg_id: int,
    username: str | None,
    first_name: str | None,
    default_currency: str,
    default_tz_minutes: int,
) -> tuple[asyncpg.Record, bool]:
    """Возвращает (пользователь, признак что он создан только что)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            created = await conn.fetchrow(
                """
                INSERT INTO users (tg_id, username, first_name, api_token, currency, tz_minutes)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tg_id) DO NOTHING
                RETURNING *
                """,
                tg_id,
                username,
                first_name,
                new_api_token(),
                default_currency,
                default_tz_minutes,
            )
            if created is not None:
                await conn.executemany(
                    "INSERT INTO categories (user_id, name, pos) VALUES ($1, $2, $3)",
                    [(created["id"], name, i) for i, name in enumerate(DEFAULT_CATEGORIES)],
                )
                return created, True

            existing = await conn.fetchrow(
                """
                UPDATE users
                   SET username = $2, first_name = $3, last_seen_at = now()
                 WHERE tg_id = $1
                RETURNING *
                """,
                tg_id,
                username,
                first_name,
            )
            return existing, False


async def get_user_by_token(pool: asyncpg.Pool, token: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM users WHERE api_token = $1 AND is_active", token
    )


async def get_user_by_tg(pool: asyncpg.Pool, tg_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)


async def rotate_token(pool: asyncpg.Pool, user_id: int) -> str:
    return await pool.fetchval(
        "UPDATE users SET api_token = $2 WHERE id = $1 RETURNING api_token",
        user_id,
        new_api_token(),
    )


async def update_settings(
    pool: asyncpg.Pool,
    user_id: int,
    *,
    currency: str | None = None,
    tz_minutes: int | None = None,
) -> None:
    await pool.execute(
        """
        UPDATE users
           SET currency   = COALESCE($2, currency),
               tz_minutes = COALESCE($3, tz_minutes)
         WHERE id = $1
        """,
        user_id,
        currency,
        tz_minutes,
    )


# --- категории ---------------------------------------------------------------


async def list_categories(pool: asyncpg.Pool, user_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT id, name, pos FROM categories
         WHERE user_id = $1 AND NOT is_archived
         ORDER BY pos, id
        """,
        user_id,
    )


async def add_category(pool: asyncpg.Pool, user_id: int, name: str) -> asyncpg.Record:
    """Создаёт категорию или возвращает одноимённую существующую (в т.ч. из архива)."""
    return await pool.fetchrow(
        """
        INSERT INTO categories (user_id, name, pos)
        VALUES ($1, $2, 100)
        ON CONFLICT (user_id, lower(name))
        DO UPDATE SET is_archived = FALSE
        RETURNING id, name, pos
        """,
        user_id,
        name,
    )


async def archive_category(pool: asyncpg.Pool, user_id: int, category_id: int) -> str | None:
    """Прячет категорию из кнопок. Уже размеченные траты сохраняют ссылку на неё."""
    return await pool.fetchval(
        """
        UPDATE categories SET is_archived = TRUE
         WHERE id = $1 AND user_id = $2
        RETURNING name
        """,
        category_id,
        user_id,
    )


# --- траты -------------------------------------------------------------------


async def find_recent_duplicate(
    pool: asyncpg.Pool,
    user_id: int,
    amount: Decimal,
    currency: str,
    window_seconds: int,
) -> asyncpg.Record | None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
    return await pool.fetchrow(
        """
        SELECT id FROM spends
         WHERE user_id = $1 AND amount = $2 AND currency = $3 AND occurred_at > $4
         ORDER BY occurred_at DESC
         LIMIT 1
        """,
        user_id,
        amount,
        currency,
        cutoff,
    )


async def create_spend(
    pool: asyncpg.Pool,
    *,
    user_id: int,
    amount: Decimal,
    currency: str,
    raw: str | None,
    source: str,
    occurred_at: datetime | None = None,
    category_id: int | None = None,
) -> asyncpg.Record:
    status = "done" if category_id is not None else "pending"
    return await pool.fetchrow(
        """
        INSERT INTO spends (user_id, category_id, amount, currency, raw, source,
                            occurred_at, status, categorized_at)
        VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7, now()), $8,
                CASE WHEN $2::bigint IS NULL THEN NULL ELSE now() END)
        RETURNING *
        """,
        user_id,
        category_id,
        amount,
        currency,
        raw,
        source,
        occurred_at,
        status,
    )


async def attach_message_id(pool: asyncpg.Pool, spend_id: int, message_id: int) -> None:
    await pool.execute(
        "UPDATE spends SET tg_message_id = $2 WHERE id = $1", spend_id, message_id
    )


async def get_spend(pool: asyncpg.Pool, user_id: int, spend_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT s.*, c.name AS category_name
          FROM spends s
          LEFT JOIN categories c ON c.id = s.category_id
         WHERE s.id = $1 AND s.user_id = $2
        """,
        spend_id,
        user_id,
    )


async def set_category(
    pool: asyncpg.Pool, user_id: int, spend_id: int, category_id: int
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        UPDATE spends s
           SET category_id = $3, status = 'done', categorized_at = now()
          FROM categories c
         WHERE s.id = $1 AND s.user_id = $2 AND c.id = $3 AND c.user_id = $2
        RETURNING s.*, c.name AS category_name
        """,
        spend_id,
        user_id,
        category_id,
    )


async def mark_ignored(pool: asyncpg.Pool, user_id: int, spend_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        UPDATE spends
           SET status = 'ignored', category_id = NULL, categorized_at = now()
         WHERE id = $1 AND user_id = $2
        RETURNING *, NULL::text AS category_name
        """,
        spend_id,
        user_id,
    )


async def reopen_spend(pool: asyncpg.Pool, user_id: int, spend_id: int) -> asyncpg.Record | None:
    """Возврат траты в статус «без категории» — кнопка «Изменить»."""
    return await pool.fetchrow(
        """
        UPDATE spends
           SET status = 'pending', category_id = NULL, categorized_at = NULL
         WHERE id = $1 AND user_id = $2
        RETURNING *, NULL::text AS category_name
        """,
        spend_id,
        user_id,
    )


async def delete_spend(pool: asyncpg.Pool, user_id: int, spend_id: int) -> bool:
    result = await pool.execute(
        "DELETE FROM spends WHERE id = $1 AND user_id = $2", spend_id, user_id
    )
    return result.endswith(" 1")


async def list_pending(pool: asyncpg.Pool, user_id: int, limit: int = 10) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT * FROM spends
         WHERE user_id = $1 AND status = 'pending'
         ORDER BY occurred_at DESC
         LIMIT $2
        """,
        user_id,
        limit,
    )


async def count_pending(pool: asyncpg.Pool, user_id: int) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM spends WHERE user_id = $1 AND status = 'pending'", user_id
    )


async def last_spends(pool: asyncpg.Pool, user_id: int, limit: int = 10) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT s.*, c.name AS category_name
          FROM spends s
          LEFT JOIN categories c ON c.id = s.category_id
         WHERE s.user_id = $1
         ORDER BY s.occurred_at DESC
         LIMIT $2
        """,
        user_id,
        limit,
    )


# --- отчёты ------------------------------------------------------------------


async def report_by_category(
    pool: asyncpg.Pool, user_id: int, start: datetime, end: datetime
) -> list[asyncpg.Record]:
    """Суммы по (валюта, категория) за период. Только размеченные траты."""
    return await pool.fetch(
        """
        SELECT s.currency,
               COALESCE(c.name, 'Без категории') AS category,
               sum(s.amount) AS total,
               count(*)      AS n
          FROM spends s
          LEFT JOIN categories c ON c.id = s.category_id
         WHERE s.user_id = $1
           AND s.status = 'done'
           AND s.occurred_at >= $2
           AND s.occurred_at < $3
         GROUP BY s.currency, COALESCE(c.name, 'Без категории')
         ORDER BY s.currency, total DESC
        """,
        user_id,
        start,
        end,
    )


async def totals_by_currency(
    pool: asyncpg.Pool, user_id: int, start: datetime, end: datetime
) -> dict[str, Decimal]:
    rows = await pool.fetch(
        """
        SELECT currency, sum(amount) AS total
          FROM spends
         WHERE user_id = $1 AND status = 'done' AND occurred_at >= $2 AND occurred_at < $3
         GROUP BY currency
        """,
        user_id,
        start,
        end,
    )
    return {row["currency"]: row["total"] for row in rows}


async def export_rows(pool: asyncpg.Pool, user_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT s.occurred_at, s.amount, s.currency, s.status, s.source,
               COALESCE(c.name, '') AS category, COALESCE(s.raw, '') AS raw
          FROM spends s
          LEFT JOIN categories c ON c.id = s.category_id
         WHERE s.user_id = $1
         ORDER BY s.occurred_at
        """,
        user_id,
    )


# --- админская сводка --------------------------------------------------------


async def admin_stats(pool: asyncpg.Pool) -> dict[str, Any]:
    users_total = await pool.fetchval("SELECT count(*) FROM users")
    users_active = await pool.fetchval(
        "SELECT count(*) FROM users WHERE last_seen_at > now() - interval '7 days'"
    )
    spends_total = await pool.fetchval("SELECT count(*) FROM spends")
    pending_total = await pool.fetchval("SELECT count(*) FROM spends WHERE status = 'pending'")
    by_currency = await pool.fetch(
        """
        SELECT currency, sum(amount) AS total, count(*) AS n
          FROM spends WHERE status = 'done'
         GROUP BY currency ORDER BY total DESC
        """
    )
    top_users = await pool.fetch(
        """
        SELECT u.tg_id, u.username, u.first_name, count(s.id) AS n
          FROM users u
          LEFT JOIN spends s ON s.user_id = u.id
         GROUP BY u.id
         ORDER BY n DESC
         LIMIT 5
        """
    )
    return {
        "users_total": users_total,
        "users_active": users_active,
        "spends_total": spends_total,
        "pending_total": pending_total,
        "by_currency": by_currency,
        "top_users": top_users,
    }

#!/usr/bin/env python3
"""Проверка связи с базой: подключается, создаёт схему, показывает, что получилось.

    cd /Users/eriksultanaliev/work/own/cacher
    set -a && source .env && set +a
    .venv/bin/python scripts/check_db.py

Строка подключения нигде не печатается — только хост, чтобы было видно, та ли это база.
"""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db  # noqa: E402


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL не задан. Проверь, что .env заполнен и подгружен:")
        print("  set -a && source .env && set +a")
        return 1

    host = urlsplit(db.normalize_dsn(dsn)[0]).hostname or "?"
    print(f"Подключаюсь к {host} …")

    try:
        pool = await db.create_pool(dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"Не подключился: {type(exc).__name__}: {exc}")
        print(
            "Проверь строку подключения: верен ли хост и не остался ли в ней "
            "плейсхолдер вместо настоящего пароля."
        )
        return 1

    try:
        version = await pool.fetchval("SHOW server_version")
        print(f"Связь есть, Postgres {version}")

        await db.apply_schema(pool)
        tables = await pool.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        print("Таблицы:", ", ".join(row["tablename"] for row in tables) or "нет")

        users = await pool.fetchval("SELECT count(*) FROM users")
        spends = await pool.fetchval("SELECT count(*) FROM spends")
        print(f"Пользователей: {users}, трат: {spends}")
        print("\nБаза готова к работе.")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

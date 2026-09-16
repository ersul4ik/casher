#!/usr/bin/env python3
"""Database connectivity check: connects, applies the schema, reports what it found.

    cd /Users/eriksultanaliev/work/own/cacher
    set -a && source .env && set +a
    .venv/bin/python scripts/check_db.py

The connection string is never printed — only the host, so you can tell which database it is.
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
        print("DATABASE_URL is not set. Make sure .env is filled in and sourced:")
        print("  set -a && source .env && set +a")
        return 1

    host = urlsplit(db.normalize_dsn(dsn)[0]).hostname or "?"
    print(f"Connecting to {host} …")

    try:
        pool = await db.create_pool(dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"Connection failed: {type(exc).__name__}: {exc}")
        print(
            "Check the connection string: the host, and whether a placeholder is still "
            "sitting there instead of the real password."
        )
        return 1

    try:
        version = await pool.fetchval("SHOW server_version")
        print(f"Connected, Postgres {version}")

        await db.apply_schema(pool)
        tables = await pool.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        print("Tables:", ", ".join(row["tablename"] for row in tables) or "none")

        users = await pool.fetchval("SELECT count(*) FROM users")
        spends = await pool.fetchval("SELECT count(*) FROM spends")
        print(f"Users: {users}, spends: {spends}")
        print("\nDatabase is ready.")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

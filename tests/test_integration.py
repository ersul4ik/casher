"""Интеграционные тесты на настоящем Postgres.

Без TEST_DATABASE_URL пропускаются. Поднять базу одной командой:

    docker run -d --name cacher-pg -e POSTGRES_PASSWORD=pass -e POSTGRES_DB=cacher \
        -p 55432:5432 postgres:16-alpine
    export TEST_DATABASE_URL=postgresql://postgres:pass@localhost:55432/cacher
    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import os
import unittest
from datetime import timedelta
from decimal import Decimal

from app import db
from app.reports import build_report, resolve_period

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
BISHKEK = 360


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL не задан")
class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await db.create_pool(TEST_DSN)
        await self.pool.execute("DROP TABLE IF EXISTS spends, categories, users CASCADE")
        await db.apply_schema(self.pool)
        # Повторное применение схемы не должно падать — она выполняется при каждом старте.
        await db.apply_schema(self.pool)

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def make_user(self, tg_id: int):
        user, created = await db.get_or_create_user(
            self.pool,
            tg_id=tg_id,
            username=f"user{tg_id}",
            first_name="Тест",
            default_currency="KGS",
            default_tz_minutes=BISHKEK,
        )
        return user, created

    async def test_registration_creates_default_categories_once(self) -> None:
        user, created = await self.make_user(1001)
        self.assertTrue(created)
        categories = await db.list_categories(self.pool, user["id"])
        self.assertEqual([c["name"] for c in categories], list(db.DEFAULT_CATEGORIES))

        again, created_again = await self.make_user(1001)
        self.assertFalse(created_again)
        self.assertEqual(again["id"], user["id"])
        self.assertEqual(len(await db.list_categories(self.pool, user["id"])), 4)

    async def test_tokens_are_unique_and_resolve_to_owner(self) -> None:
        first, _ = await self.make_user(1002)
        second, _ = await self.make_user(1003)
        self.assertNotEqual(first["api_token"], second["api_token"])

        found = await db.get_user_by_token(self.pool, first["api_token"])
        self.assertEqual(found["id"], first["id"])
        self.assertIsNone(await db.get_user_by_token(self.pool, "мусор"))

        rotated = await db.rotate_token(self.pool, first["id"])
        self.assertIsNone(await db.get_user_by_token(self.pool, first["api_token"]))
        self.assertIsNotNone(await db.get_user_by_token(self.pool, rotated))

    async def test_spend_lifecycle(self) -> None:
        user, _ = await self.make_user(1004)
        spend = await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("1470.00"),
            currency="KGS",
            raw="Успещная операция по QR. Сумма: 1470.00 KGS",
            source="shortcut",
        )
        self.assertEqual(spend["status"], "pending")
        self.assertEqual(await db.count_pending(self.pool, user["id"]), 1)

        category = (await db.list_categories(self.pool, user["id"]))[0]
        updated = await db.set_category(self.pool, user["id"], spend["id"], category["id"])
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["category_name"], category["name"])
        self.assertEqual(await db.count_pending(self.pool, user["id"]), 0)

        reopened = await db.reopen_spend(self.pool, user["id"], spend["id"])
        self.assertEqual(reopened["status"], "pending")

        ignored = await db.mark_ignored(self.pool, user["id"], spend["id"])
        self.assertEqual(ignored["status"], "ignored")
        # «Не расход» не должен попадать в отчёт.
        period = resolve_period("month", 0, BISHKEK)
        rows = await db.report_by_category(self.pool, user["id"], period.start, period.end)
        self.assertEqual(rows, [])

    async def test_deduplication_window(self) -> None:
        user, _ = await self.make_user(1005)
        await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("1470.00"),
            currency="KGS",
            raw=None,
            source="shortcut",
        )
        duplicate = await db.find_recent_duplicate(
            self.pool, user["id"], Decimal("1470.00"), "KGS", 90
        )
        self.assertIsNotNone(duplicate)

        # Другая сумма, другая валюта и другой пользователь дублями не считаются.
        self.assertIsNone(
            await db.find_recent_duplicate(self.pool, user["id"], Decimal("1470.01"), "KGS", 90)
        )
        self.assertIsNone(
            await db.find_recent_duplicate(self.pool, user["id"], Decimal("1470.00"), "USD", 90)
        )
        other, _ = await self.make_user(1006)
        self.assertIsNone(
            await db.find_recent_duplicate(self.pool, other["id"], Decimal("1470.00"), "KGS", 90)
        )

    async def test_users_do_not_see_each_others_data(self) -> None:
        alice, _ = await self.make_user(1007)
        bob, _ = await self.make_user(1008)
        alice_cat = (await db.list_categories(self.pool, alice["id"]))[0]
        bob_cat = (await db.list_categories(self.pool, bob["id"]))[0]

        alice_spend = await db.create_spend(
            self.pool,
            user_id=alice["id"],
            amount=Decimal("500.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=alice_cat["id"],
        )
        await db.create_spend(
            self.pool,
            user_id=bob["id"],
            amount=Decimal("700.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=bob_cat["id"],
        )

        period = resolve_period("month", 0, BISHKEK)
        alice_rows = await db.report_by_category(self.pool, alice["id"], period.start, period.end)
        self.assertEqual(sum(r["total"] for r in alice_rows), Decimal("500.00"))

        # Боб не может тронуть чужую трату даже зная её id.
        self.assertIsNone(await db.get_spend(self.pool, bob["id"], alice_spend["id"]))
        self.assertIsNone(
            await db.set_category(self.pool, bob["id"], alice_spend["id"], bob_cat["id"])
        )
        self.assertFalse(await db.delete_spend(self.pool, bob["id"], alice_spend["id"]))
        # И не может приписать чужую категорию своей трате.
        bob_spend = await db.create_spend(
            self.pool,
            user_id=bob["id"],
            amount=Decimal("10.00"),
            currency="KGS",
            raw=None,
            source="manual",
        )
        self.assertIsNone(
            await db.set_category(self.pool, bob["id"], bob_spend["id"], alice_cat["id"])
        )

    async def test_reports_respect_period_boundaries(self) -> None:
        user, _ = await self.make_user(1009)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        today = resolve_period("day", 0, BISHKEK)

        # Одна трата сегодня, одна — сорок дней назад.
        await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("100.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=category["id"],
            occurred_at=today.start + timedelta(hours=1),
        )
        await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("900.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=category["id"],
            occurred_at=today.start - timedelta(days=40),
        )

        day_rows = await db.report_by_category(self.pool, user["id"], today.start, today.end)
        self.assertEqual(sum(r["total"] for r in day_rows), Decimal("100.00"))

        text = await build_report(self.pool, user, "day", 0)
        self.assertIn("Итого", text)
        self.assertIn("100", text)
        self.assertNotIn("900", text)

        empty = await build_report(self.pool, user, "day", 3)
        self.assertIn("Трат за этот период нет", empty)

    async def test_report_splits_currencies(self) -> None:
        user, _ = await self.make_user(1010)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        for amount, currency in ((Decimal("100.00"), "KGS"), (Decimal("7.00"), "USD")):
            await db.create_spend(
                self.pool,
                user_id=user["id"],
                amount=amount,
                currency=currency,
                raw=None,
                source="manual",
                category_id=category["id"],
            )
        text = await build_report(self.pool, user, "month", 0)
        self.assertIn("KGS", text)
        self.assertIn("USD", text)

    async def test_archived_category_hidden_but_history_kept(self) -> None:
        user, _ = await self.make_user(1011)
        category = await db.add_category(self.pool, user["id"], "Такси")
        spend = await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("250.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=category["id"],
        )
        self.assertEqual(await db.archive_category(self.pool, user["id"], category["id"]), "Такси")
        names = [c["name"] for c in await db.list_categories(self.pool, user["id"])]
        self.assertNotIn("Такси", names)

        stored = await db.get_spend(self.pool, user["id"], spend["id"])
        self.assertEqual(stored["category_name"], "Такси")

        # Повторное добавление возвращает ту же категорию из архива, дубля не создаётся.
        restored = await db.add_category(self.pool, user["id"], "такси")
        self.assertEqual(restored["id"], category["id"])
        self.assertIn("Такси", [c["name"] for c in await db.list_categories(self.pool, user["id"])])

    async def test_export_rows(self) -> None:
        user, _ = await self.make_user(1012)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("42.00"),
            currency="KGS",
            raw="Сумма: 42.00 KGS",
            source="shortcut",
            category_id=category["id"],
        )
        rows = await db.export_rows(self.pool, user["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], category["name"])
        self.assertEqual(rows[0]["status"], "done")

    async def test_admin_stats(self) -> None:
        user, _ = await self.make_user(1013)
        await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("10.00"),
            currency="KGS",
            raw=None,
            source="manual",
        )
        stats = await db.admin_stats(self.pool)
        self.assertEqual(stats["users_total"], 1)
        self.assertEqual(stats["spends_total"], 1)
        self.assertEqual(stats["pending_total"], 1)

    async def test_cascade_delete_user(self) -> None:
        user, _ = await self.make_user(1014)
        await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("5.00"),
            currency="KGS",
            raw=None,
            source="manual",
        )
        await self.pool.execute("DELETE FROM users WHERE id = $1", user["id"])
        left = await self.pool.fetchval("SELECT count(*) FROM spends WHERE user_id = $1", user["id"])
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()

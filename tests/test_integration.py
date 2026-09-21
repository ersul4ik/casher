"""Integration tests against a real Postgres.

Skipped unless TEST_DATABASE_URL is set. Spin a database up with:

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
from app.reports import build_entity_summary, build_report, build_tag_report, resolve_period

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
BISHKEK = 360


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL is not set")
class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await db.create_pool(TEST_DSN)
        await self.pool.execute("DROP TABLE IF EXISTS spend_tags, tags, spends, categories, users CASCADE")
        await db.apply_schema(self.pool)
        # Re-applying the schema must not fail: it runs on every start.
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
        # An ignored spend must stay out of the report.
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

        # A different amount, currency or user is not a duplicate.
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

        # Bob cannot touch someone else's spend even knowing its id.
        self.assertIsNone(await db.get_spend(self.pool, bob["id"], alice_spend["id"]))
        self.assertIsNone(
            await db.set_category(self.pool, bob["id"], alice_spend["id"], bob_cat["id"])
        )
        self.assertFalse(await db.delete_spend(self.pool, bob["id"], alice_spend["id"]))
        # Nor can he tag his own spend with someone else's category.
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

        # One spend today, one forty days ago.
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

        # Adding it again restores the archived row instead of creating a duplicate.
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

    async def test_tags_attach_toggle_and_stay_per_user(self) -> None:
        alice, _ = await self.make_user(1020)
        bob, _ = await self.make_user(1021)
        category = (await db.list_categories(self.pool, alice["id"]))[0]
        spend = await db.create_spend(
            self.pool,
            user_id=alice["id"],
            amount=Decimal("120.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=category["id"],
        )

        water = await db.ensure_tag(self.pool, alice["id"], "вода")
        # Same name in another case is the same tag, not a second one.
        again = await db.ensure_tag(self.pool, alice["id"], "Вода")
        self.assertEqual(again["id"], water["id"])

        self.assertTrue(await db.attach_tag(self.pool, alice["id"], spend["id"], water["id"]))
        # Attaching twice is a no-op rather than an error.
        self.assertFalse(await db.attach_tag(self.pool, alice["id"], spend["id"], water["id"]))
        self.assertEqual(
            [t["name"] for t in await db.tags_for_spend(self.pool, spend["id"])], ["вода"]
        )

        self.assertIs(await db.toggle_tag(self.pool, alice["id"], spend["id"], water["id"]), False)
        self.assertEqual(await db.tags_for_spend(self.pool, spend["id"]), [])
        self.assertIs(await db.toggle_tag(self.pool, alice["id"], spend["id"], water["id"]), True)

        # Bob's tag cannot reach Alice's spend, and Bob cannot touch hers.
        bob_tag = await db.ensure_tag(self.pool, bob["id"], "кофе")
        self.assertFalse(await db.attach_tag(self.pool, alice["id"], spend["id"], bob_tag["id"]))
        self.assertIsNone(await db.toggle_tag(self.pool, bob["id"], spend["id"], water["id"]))
        self.assertIsNone(await db.delete_tag(self.pool, bob["id"], water["id"]))

    async def test_typed_tags_reuse_known_names_and_create_the_rest(self) -> None:
        alice, _ = await self.make_user(1030)
        bob, _ = await self.make_user(1031)
        spend = await db.create_spend(
            self.pool,
            user_id=alice["id"],
            amount=Decimal("480.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=(await db.list_categories(self.pool, alice["id"]))[0]["id"],
        )
        water = await db.ensure_tag(self.pool, alice["id"], "вода")

        # "Вода" is the tag she already has; the other two are new.
        attached = await db.attach_tags_by_name(
            self.pool, alice["id"], spend["id"], ["Вода", "кофе", "курут"]
        )
        self.assertEqual(sorted(attached), ["вода", "кофе", "курут"])
        self.assertEqual(
            [t["name"] for t in await db.tags_for_spend(self.pool, spend["id"])],
            ["вода", "кофе", "курут"],
        )
        # The known name keeps its stored spelling instead of turning into a twin.
        self.assertEqual(len(await db.list_tags(self.pool, alice["id"])), 3)
        self.assertIn(water["id"], {t["id"] for t in await db.list_tags(self.pool, alice["id"])})

        # Sending the same line again attaches nothing new and reports nothing.
        self.assertEqual(
            await db.attach_tags_by_name(self.pool, alice["id"], spend["id"], ["вода", "кофе"]),
            [],
        )
        self.assertEqual(len(await db.tags_for_spend(self.pool, spend["id"])), 3)

        # An empty line is not a database call at all.
        self.assertEqual(await db.attach_tags_by_name(self.pool, alice["id"], spend["id"], []), [])

        # Bob cannot tag Alice's spend, even though his own tag gets created.
        self.assertEqual(
            await db.attach_tags_by_name(self.pool, bob["id"], spend["id"], ["чай"]), []
        )
        self.assertEqual(len(await db.tags_for_spend(self.pool, spend["id"])), 3)

    async def test_tag_report_and_untagged_count(self) -> None:
        user, _ = await self.make_user(1022)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        water = await db.ensure_tag(self.pool, user["id"], "вода")
        coffee = await db.ensure_tag(self.pool, user["id"], "кофе")

        async def spend(amount: str, tags: list) -> None:
            row = await db.create_spend(
                self.pool,
                user_id=user["id"],
                amount=Decimal(amount),
                currency="KGS",
                raw=None,
                source="manual",
                category_id=category["id"],
            )
            for tag in tags:
                await db.attach_tag(self.pool, user["id"], row["id"], tag["id"])

        await spend("50.00", [water])
        await spend("70.00", [water, coffee])
        await spend("30.00", [])

        period = resolve_period("month", 0, BISHKEK)
        rows = await db.report_by_tag(self.pool, user["id"], period.start, period.end)
        totals = {row["tag"]: row["total"] for row in rows}
        self.assertEqual(totals["вода"], Decimal("120.00"))
        # A spend carrying two tags counts towards both.
        self.assertEqual(totals["кофе"], Decimal("70.00"))

        self.assertEqual(
            await db.count_untagged(self.pool, user["id"], period.start, period.end), 1
        )

        text = await build_tag_report(self.pool, user, "month", 0)
        self.assertIn("вода", text)
        self.assertIn("Без тегов: 1", text)

        empty = await build_tag_report(self.pool, user, "day", 5)
        self.assertIn("ничего не отмечено тегами", empty)

    async def test_note_can_be_set_and_cleared(self) -> None:
        user, _ = await self.make_user(1023)
        spend = await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("15.00"),
            currency="KGS",
            raw=None,
            source="manual",
        )
        updated = await db.set_note(self.pool, user["id"], spend["id"], "две бутылки воды")
        self.assertEqual(updated["note"], "две бутылки воды")
        cleared = await db.set_note(self.pool, user["id"], spend["id"], None)
        self.assertIsNone(cleared["note"])

        other, _ = await self.make_user(1024)
        self.assertIsNone(await db.set_note(self.pool, other["id"], spend["id"], "чужое"))

    async def test_deleting_spend_removes_its_tag_links(self) -> None:
        user, _ = await self.make_user(1025)
        spend = await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("40.00"),
            currency="KGS",
            raw=None,
            source="manual",
        )
        tag = await db.ensure_tag(self.pool, user["id"], "курут")
        await db.attach_tag(self.pool, user["id"], spend["id"], tag["id"])

        self.assertTrue(await db.delete_spend(self.pool, user["id"], spend["id"]))
        left = await self.pool.fetchval(
            "SELECT count(*) FROM spend_tags WHERE spend_id = $1", spend["id"]
        )
        self.assertEqual(left, 0)
        # The tag itself survives for future spends.
        self.assertEqual([t["name"] for t in await db.list_tags(self.pool, user["id"])], ["курут"])

    async def test_export_carries_tags_and_note(self) -> None:
        user, _ = await self.make_user(1026)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        spend = await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("99.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=category["id"],
        )
        for name in ("вода", "кофе"):
            tag = await db.ensure_tag(self.pool, user["id"], name)
            await db.attach_tag(self.pool, user["id"], spend["id"], tag["id"])
        await db.set_note(self.pool, user["id"], spend["id"], "по дороге домой")

        row = (await db.export_rows(self.pool, user["id"]))[0]
        self.assertEqual(row["tags"], "вода, кофе")
        self.assertEqual(row["note"], "по дороге домой")

    async def test_category_suggested_from_merchant_history(self) -> None:
        user, _ = await self.make_user(1030)
        categories = await db.list_categories(self.pool, user["id"])
        cafe, shop = categories[0], categories[2]

        async def spend(amount: str, merchant: str | None, category) -> None:
            row = await db.create_spend(
                self.pool,
                user_id=user["id"],
                amount=Decimal(amount),
                currency="KGS",
                raw=None,
                source="shortcut",
                merchant=merchant,
            )
            await db.set_category(self.pool, user["id"], row["id"], category["id"])

        await spend("470.00", "Arabesk Bishkek", cafe)
        # The shop wins over the amount even when the amount points elsewhere.
        await spend("470.00", None, shop)
        await spend("470.00", None, shop)

        self.assertEqual(
            await db.suggest_category(
                self.pool, user["id"], merchant="arabesk bishkek", amount=Decimal("999.00")
            ),
            cafe["id"],
        )
        # Unknown shop falls back to the amount, which now has two matching records.
        self.assertEqual(
            await db.suggest_category(
                self.pool, user["id"], merchant="Совсем новое место", amount=Decimal("470.00")
            ),
            shop["id"],
        )

    async def test_single_past_match_is_not_enough_to_suggest(self) -> None:
        user, _ = await self.make_user(1031)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        row = await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("35.00"),
            currency="KGS",
            raw=None,
            source="manual",
        )
        await db.set_category(self.pool, user["id"], row["id"], category["id"])
        # One coincidence is not a habit — no suggestion yet.
        self.assertIsNone(
            await db.suggest_category(
                self.pool, user["id"], merchant=None, amount=Decimal("35.00")
            )
        )

    async def test_suggestions_do_not_cross_users(self) -> None:
        alice, _ = await self.make_user(1032)
        bob, _ = await self.make_user(1033)
        alice_cat = (await db.list_categories(self.pool, alice["id"]))[0]
        row = await db.create_spend(
            self.pool,
            user_id=alice["id"],
            amount=Decimal("470.00"),
            currency="KGS",
            raw=None,
            source="shortcut",
            merchant="Arabesk Bishkek",
        )
        await db.set_category(self.pool, alice["id"], row["id"], alice_cat["id"])

        self.assertIsNone(
            await db.suggest_category(
                self.pool, bob["id"], merchant="Arabesk Bishkek", amount=Decimal("470.00")
            )
        )

    async def test_entity_summary_works_the_same_for_categories_and_tags(self) -> None:
        user, _ = await self.make_user(1040)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        tag = await db.ensure_tag(self.pool, user["id"], "кофе")

        for amount in ("120.00", "80.00"):
            row = await db.create_spend(
                self.pool,
                user_id=user["id"],
                amount=Decimal(amount),
                currency="KGS",
                raw=None,
                source="manual",
                category_id=category["id"],
            )
            await db.attach_tag(self.pool, user["id"], row["id"], tag["id"])

        # One more spend in the category only, so the two slices must differ.
        await db.create_spend(
            self.pool,
            user_id=user["id"],
            amount=Decimal("50.00"),
            currency="KGS",
            raw=None,
            source="manual",
            category_id=category["id"],
        )

        by_category = await db.entity_totals(self.pool, user["id"], "cat", category["id"])
        self.assertEqual(by_category[0]["total"], Decimal("250.00"))
        self.assertEqual(by_category[0]["n"], 3)

        by_tag = await db.entity_totals(self.pool, user["id"], "tag", tag["id"])
        self.assertEqual(by_tag[0]["total"], Decimal("200.00"))
        self.assertEqual(by_tag[0]["n"], 2)

        self.assertEqual(len(await db.entity_recent(self.pool, user["id"], "tag", tag["id"])), 2)

        text = await build_entity_summary(self.pool, user, "cat", category["id"])
        self.assertIn("250", text)
        self.assertIn("За всё время", text)
        self.assertIn("Последние", text)

        tag_text = await build_entity_summary(self.pool, user, "tag", tag["id"])
        self.assertIn("кофе", tag_text)
        self.assertIn("200", tag_text)

    async def test_empty_entity_says_so_instead_of_printing_dashes(self) -> None:
        user, _ = await self.make_user(1043)
        category = (await db.list_categories(self.pool, user["id"]))[0]
        tag = await db.ensure_tag(self.pool, user["id"], "курут")

        text = await build_entity_summary(self.pool, user, "cat", category["id"])
        self.assertIn("пока нет", text)
        self.assertNotIn("Сегодня", text)
        self.assertNotIn("—", text)

        tag_text = await build_entity_summary(self.pool, user, "tag", tag["id"])
        self.assertIn("Трат с этим тегом пока нет", tag_text)

    async def test_entity_summary_refuses_someone_elses_entity(self) -> None:
        alice, _ = await self.make_user(1041)
        bob, _ = await self.make_user(1042)
        alice_cat = (await db.list_categories(self.pool, alice["id"]))[0]
        alice_tag = await db.ensure_tag(self.pool, alice["id"], "вода")

        self.assertIsNone(await build_entity_summary(self.pool, bob, "cat", alice_cat["id"]))
        self.assertIsNone(await build_entity_summary(self.pool, bob, "tag", alice_tag["id"]))
        self.assertEqual(
            await db.entity_totals(self.pool, bob["id"], "cat", alice_cat["id"]), []
        )

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

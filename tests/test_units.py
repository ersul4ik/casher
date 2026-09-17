"""Tests for the pure logic: push parsing, report periods, DSN normalisation.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from app import keyboards
from app.db import normalize_dsn
from app.reports import money, period_title, resolve_period
from app.webapi import amount_from_raw, currency_from_raw, is_not_a_spend, parse_amount

REAL_PUSH = "Успещная операция по QR. Сумма: 1470.00 KGS"
BISHKEK = 360


class TestAmountParsing(unittest.TestCase):
    def test_explicit_amount_formats(self) -> None:
        self.assertEqual(parse_amount("1470.00"), Decimal("1470.00"))
        self.assertEqual(parse_amount("1 470,50"), Decimal("1470.50"))
        self.assertEqual(parse_amount(1470), Decimal("1470.00"))

    def test_rejects_garbage_and_non_positive(self) -> None:
        for value in (None, "", "abc", "0", "-5"):
            self.assertIsNone(parse_amount(value), value)

    def test_amount_from_real_push(self) -> None:
        self.assertEqual(amount_from_raw(REAL_PUSH), Decimal("1470.00"))

    def test_amount_survives_typo_change(self) -> None:
        """The bank will fix its typo one day; the parser anchors on "Сумма:", not on it."""
        self.assertEqual(
            amount_from_raw("Успешная операция по QR. Сумма: 1 470.00 KGS"),
            Decimal("1470.00"),
        )

    def test_amount_without_label(self) -> None:
        self.assertEqual(amount_from_raw("Оплата 250.75 KGS"), Decimal("250.75"))

    def test_account_numbers_are_not_amounts(self) -> None:
        """A failed-payment push carries a phone number; it must not become a spend."""
        push = (
            "Платеж: Мобильная связь\nРеквизиты: 557277896\n"
            "Не исполнен.\nПроверьте корректность реквизитов."
        )
        self.assertIsNone(amount_from_raw(push))
        self.assertIsNone(amount_from_raw("Карта **** 1234 пополнена"))
        self.assertIsNone(amount_from_raw("Счёт 1234567890123"))

    def test_failed_operations_recognised(self) -> None:
        for text in (
            "Не исполнен. Проверьте корректность реквизитов.",
            "Операция отклонена банком",
            "Недостаточно средств на счёте",
            "Платёж отменён",
        ):
            self.assertTrue(is_not_a_spend(text), text)

    def test_successful_payment_is_not_filtered_out(self) -> None:
        self.assertFalse(is_not_a_spend(REAL_PUSH))
        self.assertFalse(is_not_a_spend("Оплата прошла. Сумма: 250.00 KGS"))

    def test_currency_from_push(self) -> None:
        self.assertEqual(currency_from_raw(REAL_PUSH), "KGS")
        self.assertEqual(currency_from_raw("charge 10.00 usd"), "USD")
        self.assertIsNone(currency_from_raw("нет валюты"))


class TestMenuButtons(unittest.TestCase):
    """A dialog waiting for text must recognise menu taps and step aside."""

    def test_every_menu_caption_is_recognised(self) -> None:
        for caption in keyboards.MENU_BUTTONS:
            self.assertTrue(keyboards.is_menu_button(caption), caption)

    def test_export_button_is_not_mistaken_for_a_note(self) -> None:
        # The bug this guards: tapping "⬇️ CSV" while entering a note stored the caption.
        self.assertTrue(keyboards.is_menu_button(keyboards.BTN_EXPORT))
        self.assertTrue(keyboards.is_menu_button(f"  {keyboards.BTN_EXPORT} "))

    def test_ordinary_text_passes_through(self) -> None:
        for text in ("Швепс, лёд", "две бутылки воды", "CSV", "", None):
            self.assertFalse(keyboards.is_menu_button(text), text)


class TestMoney(unittest.TestCase):
    def test_formatting(self) -> None:
        self.assertEqual(money(Decimal("1470.00")), "1 470")
        self.assertEqual(money(Decimal("1470.50")), "1 470.50")
        self.assertEqual(money(Decimal("999")), "999")


class TestPeriods(unittest.TestCase):
    def test_day_bounds(self) -> None:
        period = resolve_period("day", 0, BISHKEK)
        self.assertEqual(period.start_local.hour, 0)
        self.assertEqual(period.end - period.start, timedelta(days=1))
        self.assertEqual(period.days, 1)

    def test_yesterday_is_shifted_by_one_day(self) -> None:
        today = resolve_period("day", 0, BISHKEK)
        yesterday = resolve_period("day", 1, BISHKEK)
        self.assertEqual(today.start - yesterday.start, timedelta(days=1))
        self.assertEqual(yesterday.end, today.start)

    def test_week_starts_on_monday(self) -> None:
        period = resolve_period("week", 0, BISHKEK)
        self.assertEqual(period.start_local.weekday(), 0)
        self.assertEqual(period.end - period.start, timedelta(days=7))
        self.assertEqual(period.days, 7)

    def test_month_bounds_and_length(self) -> None:
        period = resolve_period("month", 0, BISHKEK)
        self.assertEqual(period.start_local.day, 1)
        self.assertEqual(period.end_local.day, 1)
        self.assertIn(period.days, (28, 29, 30, 31))

    def test_month_offset_crosses_year(self) -> None:
        current = resolve_period("month", 0, BISHKEK)
        deep = resolve_period("month", 14, BISHKEK)
        months_back = (current.start_local.year - deep.start_local.year) * 12 + (
            current.start_local.month - deep.start_local.month
        )
        self.assertEqual(months_back, 14)

    def test_timezone_offset_applied(self) -> None:
        """Midnight in Bishkek is 18:00 UTC on the previous day."""
        period = resolve_period("day", 0, BISHKEK)
        self.assertEqual(period.start.hour, 18)

    def test_negative_offset_is_clamped(self) -> None:
        self.assertEqual(resolve_period("day", -3, BISHKEK).offset, 0)

    def test_unknown_period_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_period("year", 0, BISHKEK)

    def test_titles(self) -> None:
        self.assertTrue(period_title(resolve_period("day", 0, BISHKEK)).startswith("Сегодня"))
        self.assertTrue(period_title(resolve_period("day", 1, BISHKEK)).startswith("Вчера"))
        self.assertTrue(period_title(resolve_period("week", 1, BISHKEK)).startswith("Прошлая"))
        self.assertRegex(period_title(resolve_period("month", 0, BISHKEK)), r"^[А-Я][а-я]+ \d{4}$")


class TestDsn(unittest.TestCase):
    def test_neon_style_dsn_cleaned(self) -> None:
        dsn, extra = normalize_dsn(
            "postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/db"
            "?sslmode=require&channel_binding=require"
        )
        self.assertNotIn("channel_binding", dsn)
        self.assertIn("sslmode=require", dsn)
        self.assertEqual(extra, {})

    def test_pgbouncer_disables_statement_cache(self) -> None:
        dsn, extra = normalize_dsn("postgresql://u:p@host/db?pgbouncer=true&sslmode=require")
        self.assertNotIn("pgbouncer", dsn)
        self.assertEqual(extra["statement_cache_size"], 0)

    def test_sqlalchemy_scheme_stripped(self) -> None:
        dsn, _ = normalize_dsn("postgresql+asyncpg://u:p@host/db")
        self.assertTrue(dsn.startswith("postgresql://"))


if __name__ == "__main__":
    unittest.main()

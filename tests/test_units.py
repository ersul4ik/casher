"""Tests for the pure logic: push parsing, report periods, DSN normalisation.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from app import keyboards
from app.config import _base_url
from app.db import normalize_dsn
from app.handlers import MANUAL_SPEND_RE, _shift_month, parse_tag_names
from app.reports import day_offset, money, month_offset, period_title, resolve_period, today_local
from app.webapi import (
    amount_from_raw,
    currency_from_raw,
    is_not_a_spend,
    merchant_from_raw,
    parse_amount,
)

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


class TestApplePayPush(unittest.TestCase):
    """Wallet lays an Apple Pay push out as bank / shop / amount."""

    PUSH = "OPTIMA BANK OJSC\nArabesk  Bishkek\n470,00 KGS"

    def test_amount_and_currency(self) -> None:
        self.assertEqual(amount_from_raw(self.PUSH), Decimal("470.00"))
        self.assertEqual(currency_from_raw(self.PUSH), "KGS")

    def test_merchant_extracted_and_whitespace_collapsed(self) -> None:
        self.assertEqual(merchant_from_raw(self.PUSH), "Arabesk Bishkek")

    def test_bank_name_is_not_a_merchant(self) -> None:
        for line in ("OPTIMA BANK OJSC", "ОАО Оптима Банк", "Optima24"):
            self.assertIsNone(merchant_from_raw(line), line)

    def test_qr_push_has_no_merchant(self) -> None:
        self.assertIsNone(merchant_from_raw(REAL_PUSH))

    def test_amount_only_line_is_not_a_merchant(self) -> None:
        self.assertIsNone(merchant_from_raw("470,00 KGS"))


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


class TestCalendarGrid(unittest.TestCase):
    """The grid has to line days up under their weekday and never offer the future."""

    def grid(self, year: int, month: int, **kwargs):
        options = dict(
            title=f"{year}-{month}",
            marked=set(),
            today=None,
            last_day=31,
            max_day=None,
            prev_month=(year, month - 1),
            next_month=(year, month + 1),
        )
        options.update(kwargs)
        return keyboards.calendar_grid(year, month, **options)

    def days(self, markup) -> list[str]:
        return [
            button.text
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("cd:")
        ]

    def test_first_day_sits_under_its_weekday(self) -> None:
        # 1 August 2026 is a Saturday, so five blanks come before it.
        rows = self.grid(2026, 8, last_day=31).inline_keyboard
        first_week = rows[2]
        self.assertEqual(
            [b.text for b in first_week[:5]], [keyboards.CALENDAR_BLANK] * 5
        )
        self.assertEqual(first_week[5].text, "1")

    def test_every_row_holds_a_full_week(self) -> None:
        for year, month, last in ((2026, 8, 31), (2024, 2, 29), (2026, 9, 30)):
            rows = self.grid(year, month, last_day=last).inline_keyboard[2:-1]
            self.assertTrue(all(len(row) == 7 for row in rows), (year, month))

    def test_marks_and_today(self) -> None:
        markup = self.grid(2026, 9, last_day=30, marked={3, 22}, today=22)
        days = self.days(markup)
        self.assertIn("3•", days)
        self.assertIn("[22]•", days)
        self.assertIn("4", days)

    def test_the_future_is_not_offered(self) -> None:
        markup = self.grid(2026, 9, last_day=30, today=22, max_day=22, next_month=None)
        numbers = [int(text.strip("[]•")) for text in self.days(markup)]
        self.assertEqual(numbers, list(range(1, 23)))
        # No next-month arrow either, and no trailing row of nothing.
        arrows = [b.text for b in markup.inline_keyboard[0]]
        self.assertEqual(arrows[2], keyboards.CALENDAR_BLANK)
        self.assertTrue(
            any(b.callback_data.startswith("cd:") for b in markup.inline_keyboard[-2])
        )

    def test_a_day_carries_its_date(self) -> None:
        markup = self.grid(2026, 9, last_day=30)
        first = next(
            b for row in markup.inline_keyboard for b in row if b.text == "1"
        )
        self.assertEqual(first.callback_data, "cd:2026-9-1")


class TestTagNames(unittest.TestCase):
    """Tags are typed as one line, so the splitting has to be forgiving."""

    def test_comma_separated_line(self) -> None:
        self.assertEqual(parse_tag_names("вода, кофе, курут"), ["вода", "кофе", "курут"])

    def test_blanks_and_repeats_dropped(self) -> None:
        self.assertEqual(parse_tag_names(" вода ,, кофе , Вода "), ["вода", "кофе"])
        self.assertEqual(parse_tag_names("   "), [])
        self.assertEqual(parse_tag_names(None), [])

    def test_a_single_word_is_one_tag(self) -> None:
        self.assertEqual(parse_tag_names("кофе"), ["кофе"])

    def test_limits_hold(self) -> None:
        self.assertEqual(len(parse_tag_names(",".join(f"тег{i}" for i in range(20)))), 5)
        self.assertEqual(len(parse_tag_names("я" * 100)[0]), 24)

    def test_a_typed_amount_is_not_tags(self) -> None:
        # The spend card listens for tags by itself, so "350 кофейня" must stay a spend.
        self.assertIsNotNone(MANUAL_SPEND_RE.match("350 кофейня"))
        self.assertIsNone(MANUAL_SPEND_RE.match("вода, кофе"))


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


class TestBaseUrl(unittest.TestCase):
    """Guessing the address wrong means the bot goes deaf, so the order is fixed."""

    def run_with(self, **env: str) -> str:
        with mock.patch.dict(os.environ, env, clear=True):
            return _base_url()

    def test_explicit_wins(self) -> None:
        self.assertEqual(
            self.run_with(
                BASE_URL="https://mine.example", RENDER_EXTERNAL_URL="https://render.example"
            ),
            "https://mine.example",
        )

    def test_render_address_is_used_when_nothing_is_set(self) -> None:
        # A service created in a new region knows its address before anyone types it in.
        self.assertEqual(
            self.run_with(RENDER_EXTERNAL_URL="https://casher-bot.onrender.com/"),
            "https://casher-bot.onrender.com",
        )

    def test_nothing_means_long_polling(self) -> None:
        self.assertEqual(self.run_with(), "")
        self.assertEqual(self.run_with(BASE_URL="  "), "")


class TestDateOffsets(unittest.TestCase):
    """A picked date becomes the offset the reports already understand."""

    def test_today_and_yesterday(self) -> None:
        today = today_local(BISHKEK)
        self.assertEqual(day_offset(today, BISHKEK), 0)
        self.assertEqual(day_offset(today - timedelta(days=1), BISHKEK), 1)
        self.assertEqual(day_offset(today - timedelta(days=40), BISHKEK), 40)

    def test_tomorrow_is_negative_so_it_can_be_refused(self) -> None:
        self.assertEqual(day_offset(today_local(BISHKEK) + timedelta(days=1), BISHKEK), -1)

    def test_months_count_back_across_the_year(self) -> None:
        today = today_local(BISHKEK)
        self.assertEqual(month_offset(today.year, today.month, BISHKEK), 0)
        self.assertEqual(month_offset(today.year - 1, today.month, BISHKEK), 12)
        self.assertEqual(month_offset(today.year - 1, 12, BISHKEK), today.month)

    def test_time_zone_decides_which_day_it_is(self) -> None:
        # Bishkek is a day ahead of UTC-11 for most of the day; the dates may differ by one.
        self.assertLessEqual((today_local(BISHKEK) - today_local(-660)).days, 1)

    def test_month_shift_wraps_the_year(self) -> None:
        self.assertEqual(_shift_month(2026, 1, -1), (2025, 12))
        self.assertEqual(_shift_month(2026, 12, 1), (2027, 1))
        self.assertEqual(_shift_month(2026, 5, -17), (2024, 12))


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

    def test_neon_pooled_host_disables_statement_cache(self) -> None:
        # The pooled string Neon offers by default never mentions pgbouncer.
        _, extra = normalize_dsn(
            "postgresql://u:p@ep-x-pooler.eu-central-1.aws.neon.tech/db?sslmode=require"
        )
        self.assertEqual(extra["statement_cache_size"], 0)

    def test_direct_host_keeps_the_cache(self) -> None:
        _, extra = normalize_dsn(
            "postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/db?sslmode=require"
        )
        self.assertEqual(extra, {})

    def test_sqlalchemy_scheme_stripped(self) -> None:
        dsn, _ = normalize_dsn("postgresql+asyncpg://u:p@host/db")
        self.assertTrue(dsn.startswith("postgresql://"))


if __name__ == "__main__":
    unittest.main()

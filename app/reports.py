"""Period boundaries and the report text built from them.

Everything returned here is shown to the user, so the wording stays in Russian.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg

from . import db

PERIODS = ("day", "week", "month")

PERIOD_TITLES = {"day": "День", "week": "Неделя", "month": "Месяц"}

_MONTHS_NOMINATIVE = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def user_tz(tz_minutes: int) -> timezone:
    return timezone(timedelta(minutes=tz_minutes))


def money(value: Decimal | float | int) -> str:
    """Format an amount: 1470.00 -> "1 470", 1470.50 -> "1 470.50"."""
    dec = Decimal(value).quantize(Decimal("0.01"))
    whole, _, frac = f"{dec:,.2f}".partition(".")
    whole = whole.replace(",", " ")
    return whole if frac == "00" else f"{whole}.{frac}"


def _add_months(moment: datetime, months: int) -> datetime:
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    return moment.replace(year=year, month=month, day=1)


@dataclass(frozen=True)
class Period:
    """Period bounds in UTC, plus the local dates used for the heading."""

    kind: str
    offset: int
    start: datetime
    end: datetime
    start_local: datetime
    end_local: datetime

    @property
    def days(self) -> int:
        return max(1, (self.end_local.date() - self.start_local.date()).days)


def resolve_period(kind: str, offset: int, tz_minutes: int) -> Period:
    """offset=0 is the current period, 1 the previous one, and so on."""
    if kind not in PERIODS:
        raise ValueError(f"Unknown period {kind!r}")
    offset = max(0, offset)
    tz = user_tz(tz_minutes)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if kind == "day":
        start_local = midnight - timedelta(days=offset)
        end_local = start_local + timedelta(days=1)
    elif kind == "week":
        monday = midnight - timedelta(days=midnight.weekday())
        start_local = monday - timedelta(weeks=offset)
        end_local = start_local + timedelta(weeks=1)
    else:
        start_local = _add_months(midnight.replace(day=1), -offset)
        end_local = _add_months(start_local, 1)

    return Period(
        kind=kind,
        offset=offset,
        start=start_local.astimezone(timezone.utc),
        end=end_local.astimezone(timezone.utc),
        start_local=start_local,
        end_local=end_local,
    )


def period_title(period: Period) -> str:
    start = period.start_local
    last_day = period.end_local - timedelta(days=1)

    if period.kind == "day":
        if period.offset == 0:
            prefix = "Сегодня, "
        elif period.offset == 1:
            prefix = "Вчера, "
        else:
            prefix = ""
        title = f"{prefix}{start.day} {_MONTHS_GENITIVE[start.month - 1]}"
        return title if period.offset < 2 else f"{title} {start.year}"

    if period.kind == "week":
        if start.month == last_day.month:
            span = f"{start.day}–{last_day.day} {_MONTHS_GENITIVE[start.month - 1]}"
        else:
            span = (
                f"{start.day} {_MONTHS_GENITIVE[start.month - 1]} — "
                f"{last_day.day} {_MONTHS_GENITIVE[last_day.month - 1]}"
            )
        if period.offset == 0:
            return f"Эта неделя ({span})"
        if period.offset == 1:
            return f"Прошлая неделя ({span})"
        return f"Неделя {span}"

    return f"{_MONTHS_NOMINATIVE[start.month - 1]} {start.year}"


def _delta_line(current: Decimal, previous: Decimal) -> str | None:
    if not previous:
        return None
    change = (current - previous) / previous * 100
    arrow = "🔺" if change > 0 else "🔻"
    if abs(change) < 1:
        return f"Прошлый период: {money(previous)} — примерно столько же"
    return f"Прошлый период: {money(previous)} {arrow} {abs(change):.0f}%"


async def build_report(
    pool: asyncpg.Pool, user: asyncpg.Record, kind: str, offset: int
) -> str:
    """Build the HTML report for a period, broken down by category."""
    period = resolve_period(kind, offset, user["tz_minutes"])
    rows = await db.report_by_category(pool, user["id"], period.start, period.end)
    lines = [f"📊 <b>{html.escape(period_title(period))}</b>"]

    if not rows:
        lines.append("")
        lines.append("Трат за этот период нет.")
    else:
        by_currency: dict[str, list[asyncpg.Record]] = {}
        for row in rows:
            by_currency.setdefault(row["currency"], []).append(row)

        prev_period = resolve_period(kind, offset + 1, user["tz_minutes"])
        prev_totals = await db.totals_by_currency(
            pool, user["id"], prev_period.start, prev_period.end
        )

        # Currencies ordered by turnover, so the main one comes first.
        order = sorted(
            by_currency,
            key=lambda cur: sum(r["total"] for r in by_currency[cur]),
            reverse=True,
        )
        for currency in order:
            group = by_currency[currency]
            total = sum(r["total"] for r in group)
            count = sum(r["n"] for r in group)
            lines.append("")
            if len(order) > 1:
                lines.append(f"<b>— {html.escape(currency)} —</b>")
            for row in group:
                share = row["total"] / total * 100
                lines.append(
                    f"{html.escape(row['category'])} — <b>{money(row['total'])}</b>"
                    f" ({row['n']} шт · {share:.0f}%)"
                )
            lines.append("")
            lines.append(f"Итого: <b>{money(total)} {html.escape(currency)}</b> · {count} шт")
            if period.days > 1:
                lines.append(f"В среднем в день: {money(total / period.days)}")
            delta = _delta_line(total, prev_totals.get(currency, Decimal(0)))
            if delta:
                lines.append(delta)

    pending = await db.count_pending(pool, user["id"])
    if pending:
        lines.append("")
        lines.append(f"⏳ Без категории: {pending} — разметить: /pending")
    return "\n".join(lines)

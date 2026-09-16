"""HTTP endpoint that receives spends from the iPhone shortcut."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiohttp import web

from . import db, notifier
from .config import Config

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 8 * 1024

# Render exports the deployed commit; without it we are running from a checkout.
APP_VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:7]

# A push reads "Успещная операция по QR. Сумма: 1470.00 KGS". The typo in the first word is the
# bank's own, so we anchor on "Сумма:" alone — the typo may be fixed in any app update.
AMOUNT_LABELLED_RE = re.compile(r"[Сс]умма[:\s]+([0-9][0-9\s ]*(?:[.,][0-9]{1,2})?)")
AMOUNT_ANY_RE = re.compile(r"([0-9][0-9\s ]*[.,][0-9]{2})")
AMOUNT_FALLBACK_RE = re.compile(r"([0-9]+)")
CURRENCY_RE = re.compile(r"\b(KGS|USD|EUR|RUB|KZT|UZS|GBP|TRY)\b", re.IGNORECASE)


def parse_amount(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        amount = Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def amount_from_raw(raw: str) -> Decimal | None:
    """Pull the amount out of the push text when the shortcut sends it whole."""
    for pattern in (AMOUNT_LABELLED_RE, AMOUNT_ANY_RE, AMOUNT_FALLBACK_RE):
        match = pattern.search(raw)
        if match:
            amount = parse_amount(match.group(1))
            if amount is not None:
                return amount
    return None


def currency_from_raw(raw: str) -> str | None:
    match = CURRENCY_RE.search(raw)
    return match.group(1).upper() if match else None


def _parse_occurred_at(value: object) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


async def handle_spend(request: web.Request) -> web.Response:
    pool = request.app["pool"]
    bot = request.app["bot"]
    config: Config = request.app["config"]

    token = request.headers.get("X-Token") or request.query.get("token") or ""
    if not token:
        return web.json_response({"error": "no token"}, status=401)

    user = await db.get_user_by_token(pool, token)
    if user is None:
        log.warning("Rejected a request carrying an unknown token")
        return web.json_response({"error": "bad token"}, status=403)

    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return web.json_response({"error": "body too large"}, status=413)
    body = await request.text()
    if len(body.encode()) > MAX_BODY_BYTES:
        return web.json_response({"error": "body too large"}, status=413)

    try:
        data = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "bad json"}, status=400)

    raw = str(data.get("raw") or "")[:1000]
    amount = parse_amount(data.get("amount")) or amount_from_raw(raw)
    if amount is None:
        return web.json_response({"error": "no amount"}, status=400)

    currency = (
        str(data.get("currency") or "").strip().upper()
        or currency_from_raw(raw)
        or user["currency"]
    )[:8]

    duplicate = await db.find_recent_duplicate(
        pool, user["id"], amount, currency, config.dedup_window_seconds
    )
    if duplicate is not None:
        return web.json_response({"status": "duplicate", "id": duplicate["id"]})

    spend = await db.create_spend(
        pool,
        user_id=user["id"],
        amount=amount,
        currency=currency,
        raw=raw or None,
        source=str(data.get("source") or "shortcut")[:32],
        occurred_at=_parse_occurred_at(data.get("occurred_at")),
    )
    try:
        await notifier.ask_category(bot, pool, user, spend)
    except Exception:  # noqa: BLE001 — the spend is stored; /pending will catch up later
        log.exception("Could not ask for a category, spend_id=%s", spend["id"])
        return web.json_response(
            {"status": "saved", "id": spend["id"], "note": "telegram unavailable"}
        )

    return web.json_response({"status": "ok", "id": spend["id"], "amount": str(amount)})


async def handle_health(request: web.Request) -> web.Response:
    """Also the target of the keep-alive ping that stops the free instance from sleeping."""
    try:
        await request.app["pool"].fetchval("SELECT 1")
    except Exception:  # noqa: BLE001
        log.exception("Healthcheck: database unreachable")
        return web.json_response(
            {"status": "degraded", "db": False, "version": APP_VERSION}, status=503
        )
    return web.json_response({"status": "ok", "db": True, "version": APP_VERSION})


def setup_routes(app: web.Application) -> None:
    app.router.add_post("/spend", handle_spend)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)

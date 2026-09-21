"""Configuration read from environment variables."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    bot_token: str
    database_url: str
    base_url: str
    webhook_secret: str
    port: int
    default_currency: str
    default_tz_minutes: int
    admin_ids: frozenset[int]
    dedup_window_seconds: int

    @property
    def use_webhook(self) -> bool:
        """Without BASE_URL the bot falls back to long polling, which suits local runs."""
        return bool(self.base_url)

    @property
    def webhook_path(self) -> str:
        return f"/tg/{self.webhook_secret}"

    @property
    def webhook_url(self) -> str:
        return f"{self.base_url}{self.webhook_path}"

    @property
    def spend_url(self) -> str:
        return f"{self.base_url}/spend" if self.base_url else "http://localhost/spend"


def _base_url() -> str:
    """Public address of the service, empty for a local run.

    Render hands the service its own address in RENDER_EXTERNAL_URL, so a freshly
    created service knows where to point the webhook before anyone types the address
    into the dashboard. That matters during a move between regions: started without an
    address the bot would fall back to long polling and quietly take the webhook away
    from the instance still serving users.
    """
    explicit = os.environ.get("BASE_URL", "").strip()
    return (explicit or os.environ.get("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")


def load_config() -> Config:
    bot_token = _require("BOT_TOKEN")
    admin_raw = os.environ.get("ADMIN_IDS", "").replace(";", ",")
    admin_ids = frozenset(
        int(chunk.strip()) for chunk in admin_raw.split(",") if chunk.strip().lstrip("-").isdigit()
    )
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not secret:
        # Webhook path stable across restarts, yet not derivable from anything public.
        secret = hashlib.sha256(bot_token.encode()).hexdigest()[:32]

    return Config(
        bot_token=bot_token,
        database_url=_require("DATABASE_URL"),
        base_url=_base_url(),
        webhook_secret=secret,
        port=_int("PORT", 8080),
        default_currency=(os.environ.get("DEFAULT_CURRENCY") or "KGS").strip().upper(),
        default_tz_minutes=_int("DEFAULT_TZ_MINUTES", 360),  # Bishkek, UTC+6
        admin_ids=admin_ids,
        dedup_window_seconds=_int("DEDUP_WINDOW_SECONDS", 90),
    )

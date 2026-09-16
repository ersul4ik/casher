"""Конфигурация из переменных окружения."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} должна быть целым числом, получено {raw!r}") from exc


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
        """Без BASE_URL бот работает на long polling — удобно для локального запуска."""
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


def load_config() -> Config:
    bot_token = _require("BOT_TOKEN")
    admin_raw = os.environ.get("ADMIN_IDS", "").replace(";", ",")
    admin_ids = frozenset(
        int(chunk.strip()) for chunk in admin_raw.split(",") if chunk.strip().lstrip("-").isdigit()
    )
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not secret:
        # Стабильный между рестартами путь вебхука, но не выводимый из публичных данных.
        secret = hashlib.sha256(bot_token.encode()).hexdigest()[:32]

    return Config(
        bot_token=bot_token,
        database_url=_require("DATABASE_URL"),
        base_url=os.environ.get("BASE_URL", "").strip().rstrip("/"),
        webhook_secret=secret,
        port=_int("PORT", 8080),
        default_currency=(os.environ.get("DEFAULT_CURRENCY") or "KGS").strip().upper(),
        default_tz_minutes=_int("DEFAULT_TZ_MINUTES", 360),  # Бишкек, UTC+6
        admin_ids=admin_ids,
        dedup_window_seconds=_int("DEDUP_WINDOW_SECONDS", 90),
    )

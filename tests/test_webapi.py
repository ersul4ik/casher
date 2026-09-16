"""End-to-end проверка приёма трат: HTTP-запрос от шортката → запись в базе → сообщение в чат.

Требует TEST_DATABASE_URL, см. test_integration.py. Телеграм подменён заглушкой.
"""

from __future__ import annotations

import os
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import db, webapi
from app.config import Config

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")

CONFIG = Config(
    bot_token="test:token",
    database_url=TEST_DSN,
    base_url="https://example.test",
    webhook_secret="secret",
    port=8080,
    default_currency="KGS",
    default_tz_minutes=360,
    admin_ids=frozenset(),
    dedup_window_seconds=90,
)

REAL_PUSH = "Успещная операция по QR. Сумма: 1470.00 KGS"


class FakeMessage:
    message_id = 777


class FakeBot:
    """Вместо Телеграма — список отправленных сообщений."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = False

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((chat_id, text))
        return FakeMessage()


@unittest.skipUnless(TEST_DSN, "TEST_DATABASE_URL не задан")
class SpendEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await db.create_pool(TEST_DSN)
        await self.pool.execute("DROP TABLE IF EXISTS spends, categories, users CASCADE")
        await db.apply_schema(self.pool)

        self.user, _ = await db.get_or_create_user(
            self.pool,
            tg_id=555,
            username="erik",
            first_name="Эрик",
            default_currency="KGS",
            default_tz_minutes=360,
        )
        self.bot = FakeBot()

        app = web.Application()
        app["pool"] = self.pool
        app["bot"] = self.bot
        app["config"] = CONFIG
        webapi.setup_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.pool.close()

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"X-Token": token or self.user["api_token"]}

    async def test_push_creates_spend_and_asks_category(self) -> None:
        response = await self.client.post("/spend", json={"raw": REAL_PUSH}, headers=self.auth())
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["status"], "ok")

        spend = await db.get_spend(self.pool, self.user["id"], payload["id"])
        self.assertEqual(str(spend["amount"]), "1470.00")
        self.assertEqual(spend["currency"], "KGS")
        self.assertEqual(spend["status"], "pending")
        self.assertEqual(spend["raw"], REAL_PUSH)
        self.assertEqual(spend["tg_message_id"], FakeMessage.message_id)

        self.assertEqual(len(self.bot.sent), 1)
        chat_id, text = self.bot.sent[0]
        self.assertEqual(chat_id, self.user["tg_id"])
        self.assertIn("1 470", text)

    async def test_explicit_amount_wins_over_raw(self) -> None:
        response = await self.client.post(
            "/spend",
            json={"amount": "250,50", "currency": "usd", "raw": REAL_PUSH},
            headers=self.auth(),
        )
        payload = await response.json()
        spend = await db.get_spend(self.pool, self.user["id"], payload["id"])
        self.assertEqual(str(spend["amount"]), "250.50")
        self.assertEqual(spend["currency"], "USD")

    async def test_duplicate_push_ignored(self) -> None:
        first = await self.client.post("/spend", json={"raw": REAL_PUSH}, headers=self.auth())
        second = await self.client.post("/spend", json={"raw": REAL_PUSH}, headers=self.auth())
        self.assertEqual((await second.json())["status"], "duplicate")
        self.assertEqual((await second.json())["id"], (await first.json())["id"])
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM spends"), 1)

    async def test_token_decides_the_owner(self) -> None:
        other, _ = await db.get_or_create_user(
            self.pool,
            tg_id=556,
            username="other",
            first_name="Другой",
            default_currency="USD",
            default_tz_minutes=180,
        )
        response = await self.client.post(
            "/spend", json={"raw": "Сумма: 10.00"}, headers=self.auth(other["api_token"])
        )
        payload = await response.json()

        self.assertIsNotNone(await db.get_spend(self.pool, other["id"], payload["id"]))
        self.assertIsNone(await db.get_spend(self.pool, self.user["id"], payload["id"]))
        # Валюта не указана и в тексте её нет — берётся валюта владельца токена.
        spend = await db.get_spend(self.pool, other["id"], payload["id"])
        self.assertEqual(spend["currency"], "USD")
        self.assertEqual(self.bot.sent[0][0], other["tg_id"])

    async def test_rejects_missing_and_wrong_token(self) -> None:
        self.assertEqual((await self.client.post("/spend", json={"raw": REAL_PUSH})).status, 401)
        response = await self.client.post(
            "/spend", json={"raw": REAL_PUSH}, headers=self.auth("чужой-токен")
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM spends"), 0)

    async def test_rejects_bad_payloads(self) -> None:
        bad_json = await self.client.post(
            "/spend", data="не json", headers=self.auth()
        )
        self.assertEqual(bad_json.status, 400)

        no_amount = await self.client.post(
            "/spend", json={"raw": "Пополнение без цифр"}, headers=self.auth()
        )
        self.assertEqual(no_amount.status, 400)

        negative = await self.client.post("/spend", json={"amount": -5}, headers=self.auth())
        self.assertEqual(negative.status, 400)

        oversized = await self.client.post(
            "/spend", json={"raw": "x" * 20000}, headers=self.auth()
        )
        self.assertEqual(oversized.status, 413)

    async def test_spend_survives_telegram_outage(self) -> None:
        self.bot.fail = True
        response = await self.client.post("/spend", json={"raw": REAL_PUSH}, headers=self.auth())
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["status"], "saved")
        # Трата записана и попадёт в /pending, когда Телеграм вернётся.
        self.assertEqual(await db.count_pending(self.pool, self.user["id"]), 1)

    async def test_health(self) -> None:
        for path in ("/", "/health"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["status"], "ok")


if __name__ == "__main__":
    unittest.main()

"""Entry point: a single process runs both the HTTP spend receiver and the Telegram bot.

Locally: python -m app.main  — long polling, BASE_URL not needed.
Hosted:  python -m app.main  — webhook mode, enabled by BASE_URL.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from . import db, webapi
from .config import Config, load_config
from .handlers import UserMiddleware, router

log = logging.getLogger("cacher")

# Descriptions show up in the Telegram command menu, so they stay in Russian.
BOT_COMMANDS = [
    BotCommand(command="report", description="Отчёт: день / неделя / месяц"),
    BotCommand(command="date", description="Календарь: траты за любое число"),
    BotCommand(command="pending", description="Разметить траты без категории"),
    BotCommand(command="last", description="Последние траты"),
    BotCommand(command="cats", description="Категории и траты по ним"),
    BotCommand(command="tags", description="Теги и траты по ним"),
    BotCommand(command="export", description="Выгрузка в CSV"),
    BotCommand(command="settings", description="Валюта и часовой пояс"),
    BotCommand(command="token", description="Токен для шортката"),
    BotCommand(command="help", description="Справка"),
]


def build_dispatcher(pool, config: Config) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["pool"] = pool
    dp["config"] = config
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    dp.include_router(router)
    return dp


async def _set_webhook_with_retries(bot: Bot, dp: Dispatcher, config: Config) -> None:
    """Without a webhook the bot is deaf, so give the network a few tries before giving up."""
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            await bot.set_webhook(
                config.webhook_url,
                secret_token=config.webhook_secret,
                # Keep queued updates: the free instance sleeps and redeploys, and the very
                # update that wakes it up would otherwise be dropped without a trace.
                drop_pending_updates=False,
                allowed_updates=dp.resolve_used_update_types(),
            )
            return
        except Exception:  # noqa: BLE001
            if attempt == attempts:
                raise
            log.warning("Webhook setup attempt %s/%s failed", attempt, attempts)
            await asyncio.sleep(3 * attempt)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()

    pool = await db.create_pool(config.database_url)
    await db.apply_schema(pool)
    log.info("Database ready")

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(pool, config)

    app = web.Application()
    app["pool"] = pool
    app["bot"] = bot
    app["config"] = config
    webapi.setup_routes(app)

    if config.use_webhook:
        SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=config.webhook_secret
        ).register(app, path=config.webhook_path)
        setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.port).start()
    log.info("HTTP listening on port %s, spends accepted at POST /spend", config.port)

    try:
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except Exception:  # noqa: BLE001 — the command menu is cosmetic, not worth crashing over
            log.warning("Could not update the command menu", exc_info=True)

        if config.use_webhook:
            await _set_webhook_with_retries(bot, dp, config)
            log.info("Telegram webhook: %s", config.webhook_url)
            await asyncio.Event().wait()  # aiohttp handlers do the work from here
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            log.info("Long polling mode")
            await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()
        await pool.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopped")


if __name__ == "__main__":
    main()

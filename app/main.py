"""Точка входа: один процесс держит и HTTP-приёмник трат, и Телеграм-бота.

Локально:   python -m app.main            (long polling, BASE_URL не нужен)
На хостинге: python -m app.main           (webhook, если задан BASE_URL)
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

BOT_COMMANDS = [
    BotCommand(command="report", description="Отчёт: день / неделя / месяц"),
    BotCommand(command="pending", description="Разметить траты без категории"),
    BotCommand(command="last", description="Последние траты"),
    BotCommand(command="cats", description="Категории"),
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
    """Без вебхука бот глухой, поэтому даём сети пару попыток и только потом падаем."""
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            await bot.set_webhook(
                config.webhook_url,
                secret_token=config.webhook_secret,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types(),
            )
            return
        except Exception:  # noqa: BLE001
            if attempt == attempts:
                raise
            log.warning("Попытка %s/%s установить вебхук не удалась", attempt, attempts)
            await asyncio.sleep(3 * attempt)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()

    pool = await db.create_pool(config.database_url)
    await db.apply_schema(pool)
    log.info("База готова")

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
    log.info("HTTP слушает порт %s, приём трат на POST /spend", config.port)

    try:
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except Exception:  # noqa: BLE001 — меню команд косметика, ради него падать не стоит
            log.warning("Не удалось обновить меню команд", exc_info=True)

        if config.use_webhook:
            await _set_webhook_with_retries(bot, dp, config)
            log.info("Вебхук Телеграма: %s", config.webhook_url)
            await asyncio.Event().wait()  # работу ведут обработчики aiohttp
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            log.info("Режим long polling")
            await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()
        await pool.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")


if __name__ == "__main__":
    main()

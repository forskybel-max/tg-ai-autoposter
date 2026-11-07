import asyncio
import logging
import os
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.ai_service import AIService
from app.bot_handlers import setup_router
from app.config import MEDIA_DIR, load_config
from app.database import Database
from app.publisher import Publisher
from app.userbot import UserbotParser


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def start_healthcheck_server(port: int) -> web.AppRunner:
    """
    Легковесный HTTP-сервер для облачных платформ (Render, Koyeb, HuggingFace Spaces, Railway),
    которые требуют открытый веб-порт для проверки работоспособности (Healthcheck).
    """
    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "tg_ai_autoposter"})

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logging.getLogger("main").info("Healthcheck HTTP-сервер запущен на порту %d", port)
    return runner


async def periodic_media_cleanup() -> None:
    """Каждые 6 часов очищает старые временные медиафайлы (>72 ч) и пингует внешний URL Render."""
    import httpx

    tick = 0
    while True:
        if tick % 36 == 0:
            try:
                removed = Publisher.cleanup_stale_media_dir(MEDIA_DIR, max_age_hours=72)
                if removed:
                    logging.getLogger("main").info(
                        "Очищено устаревших временных медиафайлов: %d", removed
                    )
            except Exception:
                pass

        external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        if external_url:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(f"{external_url}/health")
            except Exception:
                pass

        tick += 1
        await asyncio.sleep(600)  # каждые 10 минут


async def main() -> None:
    setup_logging()
    logger = logging.getLogger("main")

    try:
        config = load_config()
    except ValueError as err:
        logger.error("\n%s", err)
        return

    db = Database(config.db_path)
    await db.init()

    bot = Bot(token=config.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    ai_service = AIService(config=config, db=db)
    publisher = Publisher(bot=bot, config=config, db=db)
    userbot = UserbotParser(
        config=config,
        db=db,
        ai_service=ai_service,
        publisher=publisher,
    )

    router = setup_router(
        config=config,
        db=db,
        ai=ai_service,
        publisher=publisher,
        userbot=userbot,
    )
    dp.include_router(router)

    # Запуск HTTP Healthcheck, если задана переменная PORT (облачный деплой)
    health_runner: web.AppRunner | None = None
    port_env = os.getenv("PORT", "").strip()
    if port_env.isdigit():
        health_runner = await start_healthcheck_server(int(port_env))

    cleanup_task = asyncio.create_task(periodic_media_cleanup())

    logger.info("Подключение к Telegram...")
    ub_ready = await userbot.start()

    bot_info = await bot.get_me()
    logger.info("=========================================================")
    logger.info("БОТ ЗАПУЩЕН: @%s", bot_info.username)
    if not ub_ready:
        logger.info("Откройте @%s в Telegram, нажмите /start и кнопку 'Войти в Userbot'!", bot_info.username)
    else:
        logger.info("Userbot авторизован и слушает каналы-доноры!")
    logger.info("Не закрывайте это окно, пока бот должен работать.")
    logger.info("=========================================================")

    for admin_id in config.admin_ids:
        try:
            hint = (
                "Нажмите /start и кнопку <b>«🔑 Войти в Userbot»</b>, чтобы подключить чтение каналов."
                if not ub_ready
                else "Userbot активен и мониторит каналы. Нажмите /start для управления."
            )
            await bot.send_message(
                chat_id=admin_id,
                text=f"🟢 <b>AI-Автопостер запущен!</b>\n{hint}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def _startup_ai_probe() -> None:
        try:
            res = await ai_service.process_post("Сегодня вышла важная новость в мире технологий.")
            logger.info("STARTUP_AI_OK (model=%s): %s", config.ai_model, res.rewritten_text[:120])
        except Exception as exc:
            logger.error("STARTUP_AI_FAIL: %s", exc)

    asyncio.create_task(_startup_ai_probe())

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Остановка сервисов...")
        cleanup_task.cancel()
        if health_runner:
            await health_runner.cleanup()
        await userbot.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

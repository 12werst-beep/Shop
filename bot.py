import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiohttp import web

# ----------------- Логирование -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ----------------- Переменные окружения -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # токен бота
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # внешний URL Render
PORT = int(os.environ.get("PORT", 8000))  # порт для web-приложения

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

# ----------------- Инициализация бота и диспетчера -----------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ----------------- Хендлеры -----------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Бот запущен!")

@dp.message(F.text)
async def echo_message(message: Message):
    await message.answer(f"Вы написали: {message.text}")

# ----------------- Webhook handler -----------------
async def handle_webhook(request: web.Request):
    """Обработка POST-запросов от Telegram"""
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    
    await dp.feed_update(data)
    return web.Response(status=200)

# ----------------- Запуск веб-приложения -----------------
async def on_startup():
    logger.info("Устанавливаем webhook...")
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook установлен: {WEBHOOK_URL}")

async def on_shutdown():
    logger.info("Удаляем webhook...")
    await bot.delete_webhook()
    await bot.session.close()

def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    
    # Запуск webhook на Render
    loop = asyncio.get_event_loop()
    loop.create_task(on_startup())
    
    try:
        web.run_app(app, host="0.0.0.0", port=PORT)
    finally:
        loop.run_until_complete(on_shutdown())

if __name__ == "__main__":
    logger.info("Бот запущен на Render!")
    main()

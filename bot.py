import os
import asyncio
import logging
from datetime import datetime

import aiosqlite
import httpx
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters.command import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ========== Настройки логирования ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== Константы ==========
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN не установлен в переменных окружения!")

POLL_INTERVAL_SECONDS = 900   # Проверять каждые 15 минут
RATE_LIMIT_MS = 400           # Задержка между запросами к магазинам (0.4s)
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}{WEBHOOK_PATH}"  # ✅ КРИТИЧЕСКИ ВАЖНО!

# ========== Инициализация ==========
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

DB_FILE = "alerts.db"

# ========== FSM ==========
class SearchStates(StatesGroup):
    link = State()
    threshold = State()

# ========== База данных ==========
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                link TEXT,
                shop TEXT,
                product TEXT,
                price REAL,
                threshold REAL
            )
        """)
        await db.commit()
        logger.info("✅ База данных инициализирована")

# ========== Парсинг цены и названия товара ==========
async def get_price_and_product(link: str):
    """Парсит цену и название товара с сайта magnit.ru"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(link, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"HTTP {resp.status_code} при запросе {link}")
                return None, None, None

            html = resp.text

            if "magnit.ru" in link:
                import re
                # Название товара
                prod_match = re.search(r'data-test-id="v-product-details-offer-name".*?>(.*?)<', html)
                # Цена (формат: 123,45 → 123.45)
                price_match = re.search(r'<span data-v-67b88f3b="">([\d.,]+)', html)
                
                product = prod_match.group(1).strip() if prod_match else None
                price_str = price_match.group(1).replace(",", ".") if price_match else None
                price = float(price_str) if price_str else None
                shop = "Магнит"
                return product, price, shop

            # TODO: Добавьте другие магазины здесь
            return None, None, None

    except Exception as e:
        logger.error(f"❌ Ошибка парсинга {link}: {e}")
        return None, None, None

# ========== CRUD операции с базой ==========
async def add_alert(user_id, link, product, shop, price, threshold):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO alerts(user_id, link, product, shop, price, threshold)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, link, product, shop, price, threshold))
        await db.commit()

async def get_user_alerts(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT id, link, product, shop, price, threshold FROM alerts WHERE user_id=?", (user_id,))
        rows = await cursor.fetchall()
        return rows

async def delete_alert(user_id, alert_id):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM alerts WHERE id=? AND user_id=?", (alert_id, user_id))
        await db.commit()

# ========== Хэндлеры бота ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот мониторинга цен.\n\n"
        "Команды:\n"
        "/search - добавить правило\n"
        "/alerts - список активных правил"
    )

@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await message.answer("Введите ссылку на товар (например, с magnit.ru):")
    await state.set_state(SearchStates.link)

@dp.message(SearchStates.link)
async def process_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer("Введите минимальную цену для уведомления (например, 199.90):")
    await state.set_state(SearchStates.threshold)

@dp.message(SearchStates.threshold)
async def process_threshold(message: Message, state: FSMContext):
    data = await state.get_data()
    link = data.get("link")
    try:
        threshold = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число (например, 199.90)")
        return

    product, price, shop = await get_price_and_product(link)
    if product is None or price is None:
        await message.answer("❌ Не удалось получить информацию о товаре. Проверьте ссылку и попробуйте снова.")
        await state.clear()
        return

    await add_alert(message.from_user.id, link, product, shop, price, threshold)
    await message.answer(
        f"✅ Правило добавлено!\n\n"
        f"🛍️ Товар: {product}\n"
        f"💰 Текущая цена: {price} ₽\n"
        f"📉 Порог уведомления: {threshold} ₽\n"
        f"🔗 Ссылка: {link}"
    )
    await state.clear()

@dp.message(Command("alerts"))
async def cmd_alerts(message: Message):
    rows = await get_user_alerts(message.from_user.id)
    if not rows:
        await message.answer("У вас нет активных правил. Используйте /search, чтобы добавить.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {r[2]} ({r[4]} ₽)", callback_data=f"del_{r[0]}")] for r in rows
    ])
    await message.answer("Ваши активные правила:", reply_markup=keyboard)

@dp.callback_query()
async def process_delete(call):
    if call.data.startswith("del_"):
        alert_id = int(call.data.split("_")[1])
        await delete_alert(call.from_user.id, alert_id)
        await call.message.edit_text("🗑️ Правило удалено.")

# ========== Фоновый мониторинг ==========
async def check_alert(alert_id, user_id, link, product, shop, threshold):
    """Проверяет один алерт и отправляет уведомление, если цена упала"""
    try:
        product_new, price_new, shop_new = await get_price_and_product(link)
        if price_new is None:
            return  # Пропускаем, если не смогли спарсить

        if price_new <= threshold and abs(price_new - threshold) > 0.01:  # Избегаем дублей
            try:
                await bot.send_message(
                    user_id,
                    f"🔥 ЦЕНА УПАЛА! 🔥\n\n"
                    f"🛍️ {product_new}\n"
                    f"💰 Текущая цена: {price_new} ₽\n"
                    f"📉 Ваш порог: {threshold} ₽\n"
                    f"🔗 Перейти: {link}"
                )
                logger.info(f"✅ Уведомление отправлено пользователю {user_id} по {link}")
            except Exception as e:
                logger.error(f"❌ Не удалось отправить уведомление пользователю {user_id}: {e}")
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке алерта {alert_id}: {e}")

async def monitor_alerts():
    """Фоновая задача: регулярно проверяет все алерты с задержкой между запросами"""
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                cursor = await db.execute("""
                    SELECT id, user_id, link, product, shop, threshold 
                    FROM alerts
                """)
                rows = await cursor.fetchall()

            tasks = []
            for row in rows:
                task = asyncio.create_task(check_alert(*row))
                tasks.append(task)

            # Выполняем задачи с интервалом RATE_LIMIT_MS
            for i, task in enumerate(tasks):
                await task
                if i < len(tasks) - 1:  # Не ждём после последнего
                    await asyncio.sleep(RATE_LIMIT_MS / 1000)

        except Exception as e:
            logger.error(f"❌ Ошибка в фоновом мониторинге: {e}")

        # Ждём следующего цикла
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# ========== Запуск приложения ==========
async def on_startup():
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")
    asyncio.create_task(monitor_alerts())
    logger.info("✅ Фоновый мониторинг запущен")

async def on_shutdown():
    await bot.session.close()
    logger.info("🛑 Бот завершил работу")

if __name__ == "__main__":
    # Запускаем инициализацию (асинхронную)
    asyncio.run(on_startup())

    # Настраиваем веб-сервер
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # ДОБАВЛЕНО: health-check endpoint для Render
    app.router.add_get('/', lambda r: web.Response(text="OK"))

    # Запускаем сервер
    try:
        web.run_app(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 10000))
        )
    except KeyboardInterrupt:
        logger.info("🛑 Сервер остановлен вручную")
    finally:
        loop = asyncio.get_running_loop()
        loop.create_task(on_shutdown())

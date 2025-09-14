import os
import asyncio
import logging
from datetime import datetime
from dateutil.parser import isoparse

import aiosqlite
import httpx
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters.command import Command

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
POLL_INTERVAL_SECONDS = 900
RATE_LIMIT_MS = 400
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://{os.getenv('RENDER_SERVICE_URL')}{WEBHOOK_PATH}"  # Render URL

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ---------- FSM для пошагового ввода ----------
class SearchStates(StatesGroup):
    link = State()
    threshold = State()


# ---------- База данных ----------
DB_FILE = "alerts.db"


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


# ---------- Вспомогательные функции ----------
async def get_price_and_product(link: str):
    """Парсинг цены и названия товара по ссылке"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(link, timeout=15)
            if resp.status_code == 404:
                return None, None
            html = resp.text

            if "magnit.ru" in link:
                import re
                prod_match = re.search(r'data-test-id="v-product-details-offer-name".*?>(.*?)<', html)
                price_match = re.search(r'<span data-v-67b88f3b="">([\d.,]+)', html)
                product = prod_match.group(1).strip() if prod_match else None
                price = float(price_match.group(1).replace(",", ".")) if price_match else None
                shop = "Магнит"
                return product, price, shop
            # TODO: Добавить другие магазины аналогично
            return None, None, None
    except Exception as e:
        logger.error(f"Ошибка при парсинге {link}: {e}")
        return None, None, None


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


# ---------- Хэндлеры ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привет! Я бот мониторинга цен.\n\n"
                         "Команды:\n"
                         "/search - добавить правило\n"
                         "/alerts - список активных правил")


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await message.answer("Введите ссылку для отслеживания товара:")
    await state.set_state(SearchStates.link)


@dp.message(SearchStates.link)
async def process_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer("Введите минимальную цену для уведомления:")
    await state.set_state(SearchStates.threshold)


@dp.message(SearchStates.threshold)
async def process_threshold(message: Message, state: FSMContext):
    data = await state.get_data()
    link = data.get("link")
    try:
        threshold = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите корректное число!")
        return

    product, price, shop = await get_price_and_product(link)
    if product is None or price is None:
        await message.answer("Не удалось получить информацию о товаре.")
        await state.clear()
        return

    await add_alert(message.from_user.id, link, product, shop, price, threshold)
    await message.answer(f"Добавлено правило:\n{shop}\n{product}\nТекущая цена: {price} ₽\nПорог: {threshold} ₽")
    await state.clear()


@dp.message(Command("alerts"))
async def cmd_alerts(message: Message):
    rows = await get_user_alerts(message.from_user.id)
    if not rows:
        await message.answer("У вас нет активных правил.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {r[2]} ({r[4]} ₽)", callback_data=f"del_{r[0]}")] for r in rows
    ])
    await message.answer("Ваши правила:", reply_markup=keyboard)


@dp.callback_query()
async def process_delete(call):
    if call.data.startswith("del_"):
        alert_id = int(call.data.split("_")[1])
        await delete_alert(call.from_user.id, alert_id)
        await call.message.edit_text("Правило удалено")


# ---------- Фоновая задача ----------
async def monitor_alerts():
    while True:
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute("SELECT id, user_id, link, product, shop, threshold FROM alerts")
            rows = await cursor.fetchall()
            for row in rows:
                alert_id, user_id, link, product, shop, threshold = row
                prod, price, shop_name = await get_price_and_product(link)
                if price is not None and price <= threshold:
                    try:
                        await bot.send_message(user_id,
                            f"🔥 Цена упала до {price} ₽!\n🛍️ Название товара: {product}\n🔗 {link}")
                    except Exception as e:
                        logger.error(f"Ошибка отправки уведомления: {e}")
                await asyncio.sleep(RATE_LIMIT_MS / 1000)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ---------- Webhook ----------
async def handle_webhook(request: web.Request):
    data = await request.json()
    update = Update(**data)
    await dp.feed_update(update)
    return web.Response(text="OK")


app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_webhook)


async def on_startup():
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook установлен: {WEBHOOK_URL}")
    asyncio.create_task(monitor_alerts())
    logger.info("Фоновый мониторинг запущен")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(on_startup())
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

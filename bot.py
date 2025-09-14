import os
import re
import asyncio
import logging
from datetime import datetime
from dateutil.parser import isoparse

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

# ---------- Логирование ----------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_SERVICE_URL = os.getenv("RENDER_SERVICE_URL", "https://shop-rm9r.onrender.com")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", "400"))

DB_PATH = "alerts.db"

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_SERVICE_URL}{WEBHOOK_PATH}"

# ---------- Инициализация ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------- FSM ----------
class SearchStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_threshold = State()


# ---------- База данных ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                shop TEXT,
                product TEXT,
                price REAL,
                threshold REAL
            )
            """
        )
        await db.commit()


async def add_alert(user_id, link, shop, product, price, threshold):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts (user_id, link, shop, product, price, threshold) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, link, shop, product, price, threshold),
        )
        await db.commit()


async def get_alerts(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, link, shop, product, price, threshold FROM alerts WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchall()


async def delete_alert(user_id, alert_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alerts WHERE user_id = ? AND id = ?", (user_id, alert_id))
        await db.commit()


async def get_all_alerts():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, user_id, link, shop, product, price, threshold FROM alerts") as cur:
            return await cur.fetchall()


# ---------- Парсинг сайтов ----------
async def fetch_price_and_product(url: str):
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None, None, None
            html = resp.text

            if "magnit.ru" in url:
                shop = "Магнит"
                product_match = re.search(r'product-details-offer__title.*?>(.*?)</span>', html)
                price_match = re.search(r'(\d+[.,]?\d*)\s*₽', html)
            elif "lenta.com" in url:
                shop = "Лента"
                product_match = re.search(r'product.*?>(.*?)</span>', html)
                price_match = re.search(r'(\d+[.,]?\d*)\s*₽', html)
            elif "5ka.ru" in url:
                shop = "Пятерочка"
                product_match = re.search(r'<h1.*?>(.*?)</h1>', html)
                price_match = re.search(r'content="(\d+[.,]?\d*)"', html)
            elif "bristol.ru" in url:
                shop = "Бристоль"
                product_match = re.search(r'<h1.*?>(.*?)</h1>', html)
                price_match = re.search(r'(\d+[.,]?\d*)\s*₽', html)
            elif "myspar.ru" in url:
                shop = "Спар"
                product_match = re.search(r'<h1.*?>(.*?)</h1>', html)
                price_match = re.search(r'(\d+[.,]?\d*)', html)
            elif "wildberries.ru" in url:
                shop = "Wildberries"
                product_match = re.search(r'productTitle.*?>(.*?)</h1>', html)
                price_match = re.search(r'(\d[\d\s]+)\s*₽', html)
            else:
                return None, None, None

            product = product_match.group(1).strip() if product_match else None
            price_str = price_match.group(1).replace(" ", "").replace(",", ".") if price_match else None
            price = float(price_str) if price_str else None

            return shop, product, price
    except Exception as e:
        logger.error(f"Ошибка при парсинге {url}: {e}")
        return None, None, None


# ---------- Фоновый монитор ----------
async def monitor_alerts():
    while True:
        alerts = await get_all_alerts()
        for alert in alerts:
            alert_id, user_id, link, shop, product, old_price, threshold = alert
            shop, product, current_price = await fetch_price_and_product(link)
            if not current_price:
                continue
            if current_price <= threshold:
                try:
                    await bot.send_message(
                        user_id,
                        f"<b>{shop}</b>\n🔥 Цена упала до <b>{current_price} ₽</b>!\n"
                        f"🛍️ {product}\n🔗 <a href='{link}'>Ссылка</a>"
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление: {e}")
            await asyncio.sleep(RATE_LIMIT_MS / 1000.0)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ---------- Команды ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот для мониторинга цен.\n\n"
        "Доступные команды:\n"
        "/search — добавить правило мониторинга\n"
        "/alerts — показать активные правила\n"
    )


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_link)
    await message.answer("🔗 Введите ссылку на товар:")


@dp.message(SearchStates.waiting_for_link)
async def process_link(message: Message, state: FSMContext):
    link = message.text.strip()
    await state.update_data(link=link)
    await state.set_state(SearchStates.waiting_for_threshold)
    await message.answer("💰 Введите минимальную цену (₽):")


@dp.message(SearchStates.waiting_for_threshold)
async def process_threshold(message: Message, state: FSMContext):
    try:
        threshold = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Введите число, например 200")
        return

    data = await state.get_data()
    link = data["link"]

    shop, product, price = await fetch_price_and_product(link)
    if not price:
        await message.answer("❌ Не удалось получить цену. Проверь ссылку.")
        await state.clear()
        return

    await add_alert(message.from_user.id, link, shop, product, price, threshold)
    await message.answer(
        f"✅ Добавлено правило:\n"
        f"<b>{shop}</b> — {product}\n"
        f"Текущая цена: {price} ₽, уведомить при ≤ {threshold} ₽"
    )
    await state.clear()


@dp.message(Command("alerts"))
async def cmd_alerts(message: Message):
    alerts = await get_alerts(message.from_user.id)
    if not alerts:
        await message.answer("У вас нет активных правил.")
        return

    text = "📋 Ваши правила:\n\n"
    kb = []
    for alert in alerts:
        alert_id, link, shop, product, price, threshold = alert
        text += f"#{alert_id} <b>{shop}</b> — {product}\nТекущая: {price} ₽, порог: {threshold} ₽\n\n"
        kb.append([InlineKeyboardButton(text=f"❌ Удалить #{alert_id}", callback_data=f"del:{alert_id}")])

    markup = InlineKeyboardMarkup(inline_keyboard=kb)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_alert(callback: CallbackQuery):
    alert_id = int(callback.data.split(":")[1])
    await delete_alert(callback.from_user.id, alert_id)
    await callback.message.edit_text("✅ Правило удалено")
    await callback.answer("Удалено")


# ---------- Webhook ----------
async def on_startup(app):
    await init_db()
    asyncio.create_task(monitor_alerts())
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info(f"Webhook set to {WEBHOOK_URL}")


async def on_shutdown(app):
    await bot.delete_webhook()
    logger.info("Webhook удалён")


async def main():
    app = web.Application()
    dp.include_router(dp)  # регистрируем все хендлеры
    app.router.add_post(WEBHOOK_PATH, dp.webhook_handler())

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()

    logger.info("Bot is running via webhook")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())


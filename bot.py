import os
import asyncio
import logging
from aiohttp import web
import httpx
from bs4 import BeautifulSoup

# --- ИМПОРТЫ ДЛЯ AIOMGRAM 3.X ---
from aiogram.filters import Command
from aiogram import F
from aiogram.types import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.client.bot import Bot, DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Dispatcher
import aiosqlite

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Настройки
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_SERVICE_URL = os.getenv("RENDER_SERVICE_URL")  # Например: https://shop-rm9r.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_SERVICE_URL}{WEBHOOK_PATH}"

POLL_INTERVAL_SECONDS = 900  # 15 минут
RATE_LIMIT_MS = 400

# --- Инициализация бота ---
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=AiohttpSession(),
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- FSM ---
class SearchStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_threshold = State()

# --- База данных ---
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

# --- Хэндлеры ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "<b>Привет!</b>\nЯ могу следить за ценами на товары.\n\n"
        "Доступные команды:\n"
        "/search - добавить товар для отслеживания\n"
        "/alerts - показать активные правила\n"
        "/cancel - отменить правило"
    )

@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await message.answer("Введите ссылку для отслеживания товара:")
    await state.set_state(SearchStates.waiting_for_link)

@dp.message(SearchStates.waiting_for_link)
async def process_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer("Введите минимальную цену, при которой присылать уведомление:")
    await state.set_state(SearchStates.waiting_for_threshold)

@dp.message(SearchStates.waiting_for_threshold)
async def process_threshold(message: Message, state: FSMContext):
    data = await state.get_data()
    link = data["link"]
    try:
        threshold = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Некорректное значение цены, попробуйте снова:")
        return

    # Парсим товар
    product, price, shop = await parse_product(link)
    if product is None:
        await message.answer("Не удалось получить информацию о товаре.")
        await state.clear()
        return

    # Сохраняем в БД
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO alerts(user_id, link, shop, product, price, threshold) VALUES (?, ?, ?, ?, ?, ?)",
            (message.from_user.id, link, shop, product, price, threshold)
        )
        await db.commit()

    await message.answer(f"✅ Добавлено:\n<b>{product}</b>\nТекущая цена: {price} ₽\nПорог: {threshold} ₽")
    await state.clear()

# --- Парсинг сайтов ---
async def parse_product(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        }
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            r = await client.get(url)
            if r.status_code != 200:
                logger.error(f"HTTP {r.status_code} при запросе к {url}")
                return None, None, None

            html = r.text
            soup = BeautifulSoup(html, "lxml")

            # --- Магнит ---
            if "magnit.ru" in url:
                shop = "Магнит"
                prod_tag = soup.select_one("span[data-test-id='v-product-details-offer-name']")
                price_tag = soup.select_one("span[data-v-67b88f3b]")
                product = prod_tag.text.strip() if prod_tag else None
                price_text = price_tag.text.strip() if price_tag else ""
                price = float(price_text.replace(" ", "").replace("₽", "").replace(",", ".")) if price_text else None

            # --- Лента ---
            elif "lenta.com" in url:
                shop = "Лента"
                prod_tag = soup.select_one("span[_ngcontent-ng-c2436889447]")
                price_tag = soup.select_one("span.main-price.title-28-20.__accent")
                product = prod_tag.text.strip() if prod_tag else None
                price_text = price_tag.text.strip() if price_tag else ""
                # Оставляем только цифры, точку, запятую и минус
                cleaned = ''.join(c for c in price_text if c.isdigit() or c in '.-,')
                price = float(cleaned.replace(',', '.')) if cleaned else None

            # --- Пятерочка ---
            elif "5ka.ru" in url:
                shop = "Пятерочка"
                prod_tag = soup.select_one("h1.chakra-text.mainInformation_name__dpsck.css-0[itemprop='name']")
                price_tag = soup.select_one("p.chakra-text.priceContainer_price__AY8C_.priceContainer_productPrice__J6NsF[itemprop='price']")
                product = prod_tag.text.strip() if prod_tag else None
                price_text = price_tag.text.strip() if price_tag else ""
                price = float(price_text.replace(",", ".")) if price_text else None

            # --- Бристоль ---
            elif "bristol.ru" in url:
                shop = "Бристоль"
                prod_tag = soup.select_one("h1.product-page__title[itemprop='name']")
                price_tag = soup.select_one("span.product-card__price-tag__price")
                product = prod_tag.text.strip() if prod_tag else None
                price_text = price_tag.text.strip() if price_tag else ""
                price = float(price_text.replace("₽", "").replace(" ", "").replace(",", ".")) if price_text else None

            # --- Спар ---
            elif "myspar.ru" in url:
                shop = "Спар"
                prod_tag = soup.select_one("h1.catalog-element__title.js-cut-text")
                price_tag = soup.select_one("span.prices__cur.js-item-price")
                product = prod_tag.text.strip() if prod_tag else None
                price_text = ""
                # Извлекаем все текстовые узлы внутри span (включая <em>)
                if price_tag:
                    price_text = ''.join(price_tag.stripped_strings).replace(' ', '').replace('\u00A0', '').replace('₽', '')
                price = float(price_text.replace(',', '.')) if price_text else None

            # --- Wildberries ---
            elif "wildberries.ru" in url:
                shop = "Wildberries"
                prod_tag = soup.select_one("h1.productTitle--J2W7I")
                price_tag = soup.select_one("ins.priceBlockFinalPrice--iToZR")
                product = prod_tag.text.strip() if prod_tag else None
                price_text = price_tag.text.strip() if price_tag else ""
                price = float(price_text.replace("\u00A0", "").replace("₽", "").replace(",", ".")) if price_text else None

            else:
                logger.warning(f"Неизвестный домен: {url}")
                return None, None, None

            # Проверка результата
            if not product or price is None:
                logger.warning(f"Не удалось извлечь продукт или цену с {url}. Продукт: {product}, Цена: {price}")
                return None, None, None

            return product, price, shop

    except Exception as e:
        logger.error(f"Ошибка при парсинге {url}: {e}", exc_info=True)
        return None, None, None

# --- Inline меню для управления ---
def generate_alerts_keyboard(alerts):
    buttons = [
        [InlineKeyboardButton(f"{a[3]} ({a[5]} ₽)", callback_data=f"del_{a[0]}")]
        for a in alerts
    ]
    if buttons:
        buttons.append([InlineKeyboardButton("Удалить все", callback_data="del_all")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(Command("alerts"))
async def show_alerts(message: Message):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT * FROM alerts WHERE user_id=?", (message.from_user.id,))
        alerts = await cursor.fetchall()
    if not alerts:
        await message.answer("Нет активных правил")
        return
    kb = generate_alerts_keyboard(alerts)
    await message.answer("Активные правила:", reply_markup=kb)

@dp.callback_query(F.data.startswith("del_"))
async def delete_alert_callback(query: CallbackQuery):
    data = query.data
    async with aiosqlite.connect(DB_FILE) as db:
        if data == "del_all":
            await db.execute("DELETE FROM alerts WHERE user_id=?", (query.from_user.id,))
            await db.commit()
            await query.message.edit_text("Все правила удалены")
        else:
            alert_id = int(data.split("_")[1])
            await db.execute("DELETE FROM alerts WHERE user_id=? AND id=?", (query.from_user.id, alert_id))
            await db.commit()
            await query.message.edit_text("Правило удалено")
    await query.answer()

# --- Фоновая проверка ---
async def monitor_alerts():
    while True:
        async with aiosqlite.connect(DB_FILE) as db:
            cursor = await db.execute("SELECT * FROM alerts")
            all_alerts = await cursor.fetchall()
        for a in all_alerts:
            alert_id, user_id, link, shop, product, price, threshold = a
            new_product, new_price, _ = await parse_product(link)
            if new_price is not None and new_price <= threshold:
                try:
                    await bot.send_message(
                        user_id,
                        f"🔥 Цена упала до {new_price} ₽!\n"
                        f"🛍️ {product}\n"
                        f"🔗 {link}"
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# --- Webhook для Render (aiogram 3.x) ---
async def handle_webhook(request: web.Request):
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return web.Response()

async def main():
    await init_db()
    asyncio.create_task(monitor_alerts())

    # 🔴 УСТАНАВЛИВАЕМ ВЕБХУК!
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Вебхук установлен: {WEBHOOK_URL}")

    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()
    logger.info("Бот запущен на Render!")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())

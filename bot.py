import os
import asyncio
import logging
from aiohttp import web
import httpx
from bs4 import BeautifulSoup

from aiogram import F
from aiogram.filters import Command
from aiogram.client.bot import Bot, DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
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
        "/cancel - отменить текущее действие"
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
        await message.answer("Не удалось получить информацию о товаре. Проверьте ссылку.")
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
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None, None, None
            html = r.text
            soup = BeautifulSoup(html, "lxml")

            def clean_price(text):
                # Удаляем все лишние символы и нормализуем
                return text.strip().replace("₽", "").replace("\u00A0", "").replace("\u202F", "").replace(",", ".").replace(" ", "")

            if "magnit.ru" in url:
                shop = "Магнит"
                prod_tag = soup.select_one("span[data-test-id='v-product-details-offer-name']")
                price_tag = soup.select_one("span[data-v-67b88f3b]")
                product = prod_tag.text.strip() if prod_tag else None
                price_str = clean_price(price_tag.text) if price_tag else None
                price = float(price_str) if price_str else None

            elif "lenta.com" in url:
                shop = "Лента"
                prod_tag = soup.select_one("span[_ngcontent-ng-c2436889447]")
                price_tag = soup.select_one("span.main-price.__accent")
                product = prod_tag.text.strip() if prod_tag else None
                price_str = clean_price(price_tag.text.split()[0]) if price_tag else None
                price = float(price_str) if price_str else None

            elif "5ka.ru" in url:
                shop = "Пятерочка"
                prod_tag = soup.select_one("h1[itemprop='name']")
                price_tag = soup.select_one("p[itemprop='price']")
                product = prod_tag.text.strip() if prod_tag else None
                price_str = clean_price(price_tag.text) if price_tag else None
                price = float(price_str) if price_str else None

            elif "bristol.ru" in url:
                shop = "Бристоль"
                prod_tag = soup.select_one("h1.product-page__title")
                price_tag = soup.select_one("span.product-card__price-tag__price")
                product = prod_tag.text.strip() if prod_tag else None
                price_str = clean_price(price_tag.text) if price_tag else None
                price = float(price_str) if price_str else None

            elif "myspar.ru" in url:
                shop = "Спар"
                prod_tag = soup.select_one("h1.catalog-element__title")
                price_tag = soup.select_one("span.js-item-price")
                product = prod_tag.text.strip() if prod_tag else None
                price_str = clean_price(price_tag.text) if price_tag else None
                price = float(price_str) if price_str else None

            elif "wildberries.ru" in url:
                shop = "Wildberries"
                prod_tag = soup.select_one("h1.productTitle--J2W7I")
                price_tag = soup.select_one("ins.priceBlockFinalPrice--iToZR")
                product = prod_tag.text.strip() if prod_tag else None
                price_str = clean_price(price_tag.text) if price_tag else None
                price = float(price_str) if price_str else None

            else:
                return None, None, None

            return product, price, shop

    except Exception as e:
        logger.error(f"Ошибка при парсинге {url}: {e}")
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

# --- Фоновая проверка (параллельная, с ограничением) ---
async def monitor_alerts():
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                cursor = await db.execute("SELECT * FROM alerts")
                all_alerts = await cursor.fetchall()

            if not all_alerts:
                logger.info("Нет активных алертов для проверки.")
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Ограничиваем одновременные запросы до 5
            semaphore = asyncio.Semaphore(5)

            async def check_single_alert(alert):
                async with semaphore:
                    alert_id, user_id, link, shop, product, price, threshold = alert
                    try:
                        new_product, new_price, _ = await parse_product(link)
                        if new_price is not None and new_price <= threshold:
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"🔥 Цена упала до {new_price} ₽!\n"
                                    f"🛍️ {product}\n"
                                    f"🔗 {link}"
                                )
                                logger.info(f"Уведомление отправлено пользователю {user_id} по {link}")
                            except Exception as e:
                                logger.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
                    except Exception as e:
                        logger.error(f"Ошибка при проверке {link}: {e}")

            tasks = [check_single_alert(a) for a in all_alerts]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Логируем ошибки (если есть)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Ошибка в задаче {all_alerts[i][0]}: {result}")

        except Exception as e:
            logger.error(f"Критическая ошибка в мониторинге: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# --- Обработчик завершения ---
@dp.shutdown()
async def on_shutdown():
    await bot.session.close()
    logger.info("Бот завершил работу.")

# --- Главная функция ---
async def main():
    # Проверка обязательных переменных окружения
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN не задан! Установите его в настройках Render.com.")
        raise SystemExit(1)
    if not RENDER_SERVICE_URL:
        logger.critical("RENDER_SERVICE_URL не задан! Убедитесь, что он указан в формате https://your-app.onrender.com")
        raise SystemExit(1)

    # Инициализация БД
    await init_db()

    # Установка вебхука
    try:
        await bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types()
        )
        logger.info(f"Webhook установлен: {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Не удалось установить вебхук: {e}")
        raise SystemExit(1)

    # Запуск фонового мониторинга
    asyncio.create_task(monitor_alerts())

    # Создание веб-приложения
    app = web.Application()

    # Регистрация обработчика вебхука (aiogram v3)
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    handler.register(app, path=WEBHOOK_PATH)

    # Health-check для Render.com (обязательно!)
    @app.router.get("/")
    async def health_check(request):
        return web.Response(text="OK", content_type="text/plain")

    # Запуск сервера
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()

    logger.info("Бот запущен на Render.com!")

    # Поддерживаем процесс живым
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную.")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {e}")


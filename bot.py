import os
import re
import asyncio
import logging
from aiohttp import web
import httpx
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

# ----------------- Логирование -----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------- Настройки -----------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{os.getenv('RENDER_SERVICE_URL')}{WEBHOOK_PATH}"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", 900))
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", 400))

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())

# ----------------- FSM -----------------
class SearchStates(StatesGroup):
    waiting_link = State()
    waiting_price = State()

# ----------------- Вспомогательная функция парсинга -----------------
async def fetch_product(url: str):
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        except Exception as e:
            return {"error": f"Ошибка запроса: {e}"}

    soup = BeautifulSoup(r.text, "lxml")
    product, price, shop = None, None, None

    if "magnit.ru" in url:
        shop = "Магнит"
        name_tag = soup.select_one('[data-test-id="v-product-details-offer-name"]')
        price_tag = soup.select_one('span[data-v-67b88f3b]')
        if name_tag: product = name_tag.text.strip()
        if price_tag: price = float(re.sub(r"[^\d,]", "", price_tag.text).replace(",", "."))
    elif "lenta.com" in url:
        shop = "Лента"
        name_tag = soup.select_one('span[_ngcontent-ng-c2436889447]')
        price_tag = soup.select_one('span.main-price.__accent')
        if name_tag: product = name_tag.text.strip()
        if price_tag: price = float(re.sub(r"[^\d,]", "", price_tag.text).replace(",", "."))
    elif "5ka.ru" in url:
        shop = "Пятерочка"
        name_tag = soup.select_one('h1[itemprop="name"]')
        price_tag = soup.select_one('p[itemprop="price"]')
        if name_tag: product = name_tag.text.strip()
        if price_tag: price = float(price_tag.get("content", price_tag.text.strip()))
    elif "bristol.ru" in url:
        shop = "Бристоль"
        name_tag = soup.select_one('h1.product-page__title')
        price_tag = soup.select_one('span.product-card__price-tag__price')
        if name_tag: product = name_tag.text.strip()
        if price_tag: price = float(re.sub(r"[^\d,]", "", price_tag.text).replace(",", "."))
    elif "myspar.ru" in url:
        shop = "Спар"
        name_tag = soup.select_one('h1.catalog-element__title')
        price_tag = soup.select_one('span.js-item-price')
        if name_tag: product = name_tag.text.strip()
        if price_tag: 
            price_text = "".join(price_tag.stripped_strings)
            price = float(re.sub(r"[^\d,]", "", price_text).replace(",", "."))
    elif "wildberries.ru" in url:
        shop = "Wildberries"
        name_tag = soup.select_one('h1.productTitle--J2W7I')
        price_tag = soup.select_one('ins.priceBlockFinalPrice--iToZR')
        if name_tag: product = name_tag.text.strip()
        if price_tag:
            price = float(re.sub(r"[^\d,]", "", price_tag.text.replace("\u00A0", "")))

    if not product or not price:
        return {"error": "Не удалось получить данные о товаре"}

    return {"shop": shop, "product": product, "price": price, "url": url}

# ----------------- Хендлеры -----------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для мониторинга цен.\n\n"
        "Команды:\n"
        "/search - добавить товар для отслеживания\n"
        "/alerts - список ваших правил"
    )

@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await message.answer("Введите ссылку для отслеживания:")
    await state.set_state(SearchStates.waiting_link)

@dp.message(F(SearchStates.waiting_link))
async def state_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text.strip())
    await message.answer("Введите минимальную цену для уведомления:")
    await state.set_state(SearchStates.waiting_price)

@dp.message(F(SearchStates.waiting_price))
async def state_price(message: Message, state: FSMContext):
    data = await state.get_data()
    link = data.get("link")
    try:
        threshold = float(message.text.strip())
    except:
        await message.answer("Некорректная цена. Введите число:")
        return

    product_info = await fetch_product(link)
    if "error" in product_info:
        await message.answer(f"Ошибка при парсинге: {product_info['error']}")
        await state.clear()
        return

    # TODO: сохранить в БД
    await message.answer(
        f"Товар добавлен:\n"
        f"🛍 {product_info['product']}\n"
        f"💰 {product_info['price']} ₽\n"
        f"Порог для уведомления: {threshold} ₽"
    )
    await state.clear()

# ----------------- Вебхук -----------------
async def handle_webhook(request):
    try:
        update = await request.json()
        await dp.process_update(update)
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Ошибка при обработке вебхука: {e}")
        return web.Response(status=500)

async def main():
    # Настройка вебхука
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8000)))
    await site.start()
    logger.info(f"Webhook set to {WEBHOOK_URL}")

    # Бот в фоне
    logger.info("Bot is running via webhook")
    while True:
        await asyncio.sleep(3600)  # держим сервис живым

if __name__ == "__main__":
    asyncio.run(main())

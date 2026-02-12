"""
Telegram бот продаж VK Cloud аккаунтов.
aiogram 3 + aiohttp для запросов к панели.
"""
import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from aiogram.filters import CommandStart

from .config import BOT_TOKEN, PANEL_URL, BOT_API_KEY, SELLER_USERNAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

router = Router()


async def fetch_accounts() -> list[dict]:
    """Получить список аккаунтов на продажу из панели."""
    url = f"{PANEL_URL}/api/bot/accounts"
    headers = {"X-API-Key": BOT_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.error(f"Panel API error: {resp.status}")
                    return []
                data = await resp.json()
                return data.get("accounts", [])
    except Exception as e:
        logger.error(f"Failed to fetch accounts: {e}")
        return []


def build_catalog_message(accounts: list[dict]) -> tuple[str, InlineKeyboardMarkup | None]:
    """Построить сообщение каталога и клавиатуру."""
    if not accounts:
        return "😔 Сейчас нет аккаунтов в продаже.\n\nЗагляните позже!", None

    lines = ["🛒 <b>Каталог VK Cloud аккаунтов</b>\n"]

    buttons = []
    for i, acc in enumerate(accounts, 1):
        ip_list = ", ".join(acc["ips"][:5])
        if len(acc["ips"]) > 5:
            ip_list += f" (+{len(acc['ips']) - 5})"

        price_str = f"{acc['price']}₽" if acc["price"] else "договорная"

        lines.append(
            f"<b>{i}. {acc['masked_email']}</b>\n"
            f"   📦 Проектов: {acc['project_count']}\n"
            f"   🌐 IP ({acc['ip_count']}): <code>{ip_list}</code>\n"
            f"   💰 Цена: <b>{price_str}</b>\n"
        )

        # Кнопка "Купить" для каждого аккаунта
        buttons.append([
            InlineKeyboardButton(
                text=f"🛒 Купить #{i} — {acc['masked_email']} ({price_str})",
                url=f"https://t.me/{SELLER_USERNAME}?text=Хочу купить аккаунт {acc['masked_email']} ({acc['ip_count']} IP, {price_str})",
            )
        ])

    text = "\n".join(lines)
    text += "\n\n💬 Нажмите «Купить» для связи с продавцом."

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return text, keyboard


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Команда /start — приветствие."""
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Здесь можно купить VK Cloud аккаунты с Floating IP.\n\n"
        "📋 /catalog — посмотреть доступные аккаунты",
        parse_mode="HTML",
    )


@router.message(F.text.lower().in_(("/catalog", "/каталог", "каталог", "catalog")))
async def cmd_catalog(message: Message):
    """Показать каталог аккаунтов на продажу."""
    await message.answer("⏳ Загружаю каталог...")

    accounts = await fetch_accounts()
    text, keyboard = build_catalog_message(accounts)

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set! Export BOT_TOKEN env variable.")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

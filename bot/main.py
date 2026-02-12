"""
Telegram бот продаж и аренды VK Cloud.
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

SELLERS = "@xlmmama @haxonate"


def mask_ip(ip: str) -> str:
    """Маскировка последнего октета IP: 5.188.203.45 → 5.188.203.***"""
    parts = ip.rsplit(".", 1)
    if len(parts) == 2:
        return parts[0] + ".***"
    return ip


async def api_get(path: str) -> dict:
    """GET запрос к панели с API ключом."""
    url = f"{PANEL_URL}{path}"
    headers = {"X-API-Key": BOT_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.error(f"Panel API error {resp.status}: {path}")
                    return {}
                return await resp.json()
    except Exception as e:
        logger.error(f"API request failed: {e}")
        return {}


# ─── /start ───────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Приветствие с тарифами и кнопками."""
    text = (
        "👋 <b>Здравствуйте!</b>\n\n"
        "У нас вы можете полностью выкупить аккаунт VK Cloud "
        "или арендовать проект с Floating IP.\n\n"
        f"Для покупки/аренды писать:\n{SELLERS}\n\n"
        "📋 <b>Тарифы:</b>\n"
        "• Любой IP на покупку — <b>30 000₽</b>\n"
        "• Любой IP в аренду — <b>500₽/сутки</b>\n\n"
        "Выберите, что вас интересует 👇"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="menu:buy")],
        [InlineKeyboardButton(text="📦 Аренда проекта", callback_data="menu:rent")],
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


# ─── Каталог покупки ──────────────────────────────────────────

@router.callback_query(F.data == "menu:buy")
async def cb_buy(callback: CallbackQuery):
    """Каталог аккаунтов на продажу — простой список."""
    await callback.answer()

    data = await api_get("/api/bot/accounts")
    accounts = data.get("accounts", [])

    if not accounts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")]
        ])
        await callback.message.answer("😔 Сейчас нет аккаунтов в продаже.\n\nЗагляните позже!", reply_markup=keyboard)
        return

    lines = ["🛒 <b>Аккаунты на продажу</b>\n"]

    for i, acc in enumerate(accounts, 1):
        lines.append(f"<b>Аккаунт {i}</b>")

        # Группируем IP по проектам
        for j, proj in enumerate(acc.get("projects", []), 1):
            lines.append(f"  Проект {j}")
            for ip in proj.get("ips", []):
                lines.append(f"    <code>{mask_ip(ip)}</code>")

        lines.append("")

    lines.append(f"\nДля покупки: {SELLERS}")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")]
    ])

    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


# ─── Каталог аренды ───────────────────────────────────────────

@router.callback_query(F.data == "menu:rent")
async def cb_rent(callback: CallbackQuery):
    """Каталог проектов на аренду — простой список."""
    await callback.answer()

    data = await api_get("/api/bot/rentals")
    projects = data.get("projects", [])

    if not projects:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")]
        ])
        await callback.message.answer("😔 Сейчас нет проектов для аренды.\n\nЗагляните позже!", reply_markup=keyboard)
        return

    lines = ["📦 <b>Проекты на аренду</b>\n"]

    for i, proj in enumerate(projects, 1):
        lines.append(f"<b>Проект {i}</b>")
        for ip in proj.get("ips", []):
            lines.append(f"  <code>{mask_ip(ip)}</code>")
        lines.append("")

    lines.append(f"\nДля аренды: {SELLERS}")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")]
    ])

    await callback.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


# ─── Назад в меню ─────────────────────────────────────────────

@router.callback_query(F.data == "menu:back")
async def cb_back(callback: CallbackQuery):
    """Вернуться в главное меню."""
    await callback.answer()
    await cmd_start(callback.message)


# ─── Запуск ───────────────────────────────────────────────────

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

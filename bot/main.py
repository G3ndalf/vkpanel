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

# Контакты продавцов
SELLERS = "@xlmmama @haxonate"


# ─── API запросы к панели ─────────────────────────────────────

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
    """Каталог аккаунтов на продажу."""
    await callback.answer()

    data = await api_get("/api/bot/accounts")
    accounts = data.get("accounts", [])

    if not accounts:
        await callback.message.answer("😔 Сейчас нет аккаунтов в продаже.\n\nЗагляните позже!")
        return

    lines = ["🛒 <b>Аккаунты на продажу</b>\n"]
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

        buttons.append([
            InlineKeyboardButton(
                text=f"🛒 Купить #{i} — {acc['masked_email']}",
                url=f"https://t.me/{SELLER_USERNAME}?text=Хочу купить аккаунт {acc['masked_email']} ({acc['ip_count']} IP)",
            )
        ])

    # Кнопка "Назад"
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)


# ─── Каталог аренды ───────────────────────────────────────────

@router.callback_query(F.data == "menu:rent")
async def cb_rent(callback: CallbackQuery):
    """Каталог проектов на аренду."""
    await callback.answer()

    data = await api_get("/api/bot/rentals")
    projects = data.get("projects", [])

    if not projects:
        await callback.message.answer("😔 Сейчас нет проектов для аренды.\n\nЗагляните позже!")
        return

    lines = ["📦 <b>Проекты на аренду</b>\n"]
    buttons = []

    for i, proj in enumerate(projects, 1):
        ip_list = ", ".join(proj["ips"][:3])
        if len(proj["ips"]) > 3:
            ip_list += f" (+{len(proj['ips']) - 3})"

        price_str = f"{proj['price']}₽/сут" if proj["price"] else "500₽/сут"

        lines.append(
            f"<b>{i}. {proj['masked_project']}</b>\n"
            f"   🌐 IP ({proj['ip_count']}): <code>{ip_list}</code>\n"
            f"   💰 Цена: <b>{price_str}</b>\n"
        )

        buttons.append([
            InlineKeyboardButton(
                text=f"📦 Арендовать #{i} — {proj['masked_project']}",
                url=f"https://t.me/{SELLER_USERNAME}?text=Хочу арендовать проект {proj['masked_project']} ({proj['ip_count']} IP)",
            )
        ])

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)


# ─── Назад в меню ─────────────────────────────────────────────

@router.callback_query(F.data == "menu:back")
async def cb_back(callback: CallbackQuery):
    """Вернуться в главное меню."""
    await callback.answer()
    await cmd_start(callback.message)


# ─── Текстовые команды (фоллбэк) ─────────────────────────────

@router.message(F.text.lower().in_(("/catalog", "/каталог", "каталог", "купить")))
async def cmd_catalog(message: Message):
    """Текстовая команда каталога."""
    data = await api_get("/api/bot/accounts")
    accounts = data.get("accounts", [])

    if not accounts:
        await message.answer("😔 Сейчас нет аккаунтов в продаже.")
        return

    # Пересылаем на callback-логику через фейковое сообщение
    await cmd_start(message)


@router.message(F.text.lower().in_(("/rent", "/аренда", "аренда")))
async def cmd_rent(message: Message):
    """Текстовая команда аренды."""
    await cmd_start(message)


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

"""
Telegram бот для продажи VK Cloud аккаунтов.
Показывает каталог аккаунтов с IP, кнопка «Купить» ведёт к продавцу.
"""
import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode

from .config import BOT_TOKEN, PANEL_URL, PANEL_API_KEY, SELLER_USERNAME

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()


# ─── Запрос к панели ──────────────────────────────────────────

async def fetch_accounts() -> list[dict] | None:
    """Получить список аккаунтов на продаже из панели."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PANEL_URL}/api/bot/accounts",
                headers={"X-API-Key": PANEL_API_KEY},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("accounts", [])
                logger.error(f"Panel API error: {resp.status}")
                return None
    except Exception as e:
        logger.error(f"Failed to fetch accounts: {e}")
        return None


# ─── Клавиатуры ───────────────────────────────────────────────

def catalog_keyboard(accounts: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура каталога — список аккаунтов."""
    buttons = []
    for i, acc in enumerate(accounts):
        text = f"{acc['username_masked']} — {acc['total_ips']} IP — {acc['price']}₽"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"acc:{i}")])
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="catalog")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def account_keyboard(index: int) -> InlineKeyboardMarkup:
    """Клавиатура аккаунта — купить + назад."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Купить", url=f"https://t.me/{SELLER_USERNAME}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog")],
    ])


# ─── Хендлеры ─────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Команда /start — приветствие с кнопкой каталога."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Каталог аккаунтов", callback_data="catalog")]
    ])
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Здесь вы можете посмотреть доступные аккаунты VK Cloud с Floating IP.\n\n"
        "Нажмите кнопку ниже, чтобы посмотреть каталог.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "catalog")
async def show_catalog(callback: CallbackQuery):
    """Показать каталог аккаунтов."""
    await callback.answer()

    accounts = await fetch_accounts()

    if accounts is None:
        await callback.message.edit_text(
            "❌ Не удалось загрузить каталог. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="catalog")]
            ]),
        )
        return

    if not accounts:
        await callback.message.edit_text(
            "📭 Нет аккаунтов в наличии.\n\nЗагляните позже!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="catalog")]
            ]),
        )
        return

    # Кэшируем аккаунты в callback data (через индексы)
    # Сохраняем в атрибут бота для доступа из callback
    callback.bot._accounts_cache = accounts

    text = f"📦 **Аккаунты в наличии: {len(accounts)}**\n\nВыберите аккаунт для подробностей:"

    await callback.message.edit_text(
        text,
        reply_markup=catalog_keyboard(accounts),
        parse_mode=ParseMode.MARKDOWN,
    )


@router.callback_query(F.data.startswith("acc:"))
async def show_account(callback: CallbackQuery):
    """Показать детали аккаунта."""
    await callback.answer()

    index = int(callback.data.split(":")[1])
    accounts = getattr(callback.bot, "_accounts_cache", None)

    if not accounts or index >= len(accounts):
        await callback.message.edit_text(
            "⚠️ Данные устарели. Обновите каталог.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить каталог", callback_data="catalog")]
            ]),
        )
        return

    acc = accounts[index]

    # Формируем список IP
    ips_text = "\n".join(f"  `{ip}`" for ip in acc["ips"][:20])
    if len(acc["ips"]) > 20:
        ips_text += f"\n  ... и ещё {len(acc['ips']) - 20}"

    text = (
        f"👤 **{acc['username_masked']}**\n\n"
        f"📊 Проектов: {acc['projects_count']}\n"
        f"🌐 Floating IP: {acc['total_ips']}\n"
        f"💰 Цена: **{acc['price']}₽**\n\n"
        f"📋 Список IP:\n{ips_text}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=account_keyboard(index),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── Запуск ───────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

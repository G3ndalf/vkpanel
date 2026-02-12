"""
Конфигурация Telegram бота продаж.
Все значения из переменных окружения.
"""
import os

# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Панель VK IP Panel — откуда берём данные
PANEL_URL = os.getenv("PANEL_URL", "http://127.0.0.1:8080")
BOT_API_KEY = os.getenv("BOT_API_KEY", "vkpanel-bot-secret-2026")

# Продавец — кнопка "Купить" ведёт сюда
SELLER_USERNAME = os.getenv("SELLER_USERNAME", "xlmmama")

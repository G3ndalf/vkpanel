"""Конфигурация Telegram бота."""
import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PANEL_URL = os.getenv("PANEL_URL", "http://localhost:8080")
PANEL_API_KEY = os.getenv("PANEL_API_KEY", "vkpanel-bot-key-2026")
SELLER_USERNAME = os.getenv("SELLER_USERNAME", "xlmmama")

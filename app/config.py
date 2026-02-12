"""
Конфигурация приложения из переменных окружения.
"""
import os
from pathlib import Path

# ─── Пути ─────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
APP_DIR = Path(__file__).parent

# ─── Авторизация ──────────────────────────────────────────────

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "Haxoastemir29")
SECRET_KEY = os.getenv("SECRET_KEY", "vkpanel-secret-key-change-me-2026")

# ─── Данные ───────────────────────────────────────────────────

DATA_FILE = os.getenv("DATA_FILE", "/opt/vkpanel/data.json")

# ─── SSH ──────────────────────────────────────────────────────

SSH_TIMEOUT = int(os.getenv("SSH_TIMEOUT", "10"))
SSH_COMMAND_TIMEOUT = int(os.getenv("SSH_COMMAND_TIMEOUT", "30"))

# ─── Параллельность ───────────────────────────────────────────

MAX_SSH_WORKERS = int(os.getenv("MAX_SSH_WORKERS", "10"))
MAX_CLOUD_WORKERS = int(os.getenv("MAX_CLOUD_WORKERS", "5"))

# ─── Бот продаж ───────────────────────────────────────────────

BOT_API_KEY = os.getenv("BOT_API_KEY", "vkpanel-bot-secret-2026")

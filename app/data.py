"""
Работа с данными (JSON-файл).
"""
import json
import logging
import os
from datetime import datetime
from typing import Any

from .config import DATA_FILE

logger = logging.getLogger(__name__)


def load_data() -> dict:
    """Загрузить данные из JSON файла."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                # Ensure required keys exist
                data.setdefault("servers", [])
                data.setdefault("accounts", [])
                data.setdefault("projects", [])
                data.setdefault("status_cache", {})
                data.setdefault("cloud_cache", {})
                data.setdefault("projects_cache", {})
                data.setdefault("last_update", None)
                data.setdefault("cloud_last_update", None)
                data.setdefault("projects_last_update", None)
                data.setdefault("sales", {})
                return data
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse {DATA_FILE}: {e}")
        except Exception as e:
            logger.error(f"Failed to load {DATA_FILE}: {e}")

    return {
        "servers": [],
        "accounts": [],
        "projects": [],
        "status_cache": {},
        "cloud_cache": {},
        "projects_cache": {},
        "last_update": None,
        "cloud_last_update": None,
        "projects_last_update": None,
        "sales": {},
    }


def save_data(data: dict) -> bool:
    """Сохранить данные в JSON файл."""
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save {DATA_FILE}: {e}")
        return False


def get_server_by_id(data: dict, server_id: int) -> dict | None:
    """Найти сервер по ID."""
    return next((s for s in data.get("servers", []) if s.get("id") == server_id), None)


def get_script_by_id(server: dict, script_id: int) -> dict | None:
    """Найти скрипт по ID."""
    return next((s for s in server.get("scripts", []) if s.get("id") == script_id), None)


def update_status_cache(data: dict, server_id: int, script_id: int, status: dict) -> None:
    """Обновить кэш статуса для одного скрипта."""
    cache_key = f"{server_id}-{script_id}"
    if "status_cache" not in data:
        data["status_cache"] = {}
    data["status_cache"][cache_key] = status
    data["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_cached_status(data: dict, server_id: int, script_id: int) -> dict:
    """Получить кэшированный статус скрипта."""
    cache_key = f"{server_id}-{script_id}"
    return data.get("status_cache", {}).get(cache_key, {})


def get_cached_cloud(data: dict, server_id: int, script_id: int) -> dict:
    """Получить кэшированные cloud данные."""
    cache_key = f"{server_id}-{script_id}"
    return data.get("cloud_cache", {}).get(cache_key, {})

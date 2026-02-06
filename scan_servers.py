#!/usr/bin/env python3
"""
Скрипт для автоматического сканирования серверов и добавления в панель.

Использование:
    # Через переменные окружения
    export SSH_USER=root
    export SSH_PASS=your_password
    python scan_servers.py

    # Или через .env файл
    python scan_servers.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Загружаем .env если есть
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

# Конфигурация
SERVERS = [
    "94.232.41.116",
    "109.205.56.114",
    "94.232.40.187",
    "45.156.26.17",
    "94.232.43.193",
    "94.232.43.146",
    "93.183.71.175",
    "94.232.44.188",
    "178.236.254.35",
    "94.232.44.53",
    "94.232.43.63",
    "94.232.40.170",
]

SSH_USER = os.getenv("SSH_USER", "root")
SSH_PASS = os.getenv("SSH_PASS")

if not SSH_PASS:
    print("Ошибка: SSH_PASS не задан!")
    print("Установите переменную окружения SSH_PASS или создайте .env файл")
    sys.exit(1)


def ssh_exec(host: str, cmd: str) -> str:
    """Выполнить команду по SSH."""
    full_cmd = (
        f"sshpass -p '{SSH_PASS}' ssh "
        f"-o StrictHostKeyChecking=no "
        f"-o ConnectTimeout=10 "
        f"{SSH_USER}@{host} \"{cmd}\""
    )
    try:
        result = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print(f"  Таймаут подключения к {host}")
        return ""
    except Exception as e:
        print(f"  Ошибка: {e}")
        return ""


def scan_server(host: str, server_id: int) -> dict | None:
    """Сканировать сервер и вернуть структуру данных."""
    print(f"Сканирую {host}...")

    # Получить список сервисов
    output = ssh_exec(
        host,
        "systemctl list-units 'vk-fip@*' --all --no-pager 2>/dev/null | "
        "grep -oE 'vk-fip@[a-zA-Z0-9]+\\.service'"
    )

    services = []
    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("vk-fip@") and line.endswith(".service"):
            # vk-fip@vk1.service -> vk1
            name = line.replace("vk-fip@", "").replace(".service", "")
            services.append(name)

    if not services:
        print(f"  Не найдено сервисов на {host}")
        return None

    # Получаем инфо об аккаунте из .env файлов
    scripts = []
    for i, name in enumerate(sorted(services), 1):
        env_output = ssh_exec(
            host,
            f"grep -E '^OS_USERNAME=|^OS_PROJECT_NAME=' /root/{name}/.env 2>/dev/null"
        )

        account_name = ""
        project_name = ""
        for line in env_output.split("\n"):
            if line.startswith("OS_USERNAME="):
                account_name = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("OS_PROJECT_NAME="):
                project_name = line.split("=", 1)[1].strip().strip('"').strip("'")

        scripts.append({
            "id": i,
            "name": name,
            "path": f"/root/{name}",
            "service_name": f"vk-fip@{name}",
            "state_file": f"/root/{name}/vk_fip_state.json",
            "account_name": account_name,
            "project_name": project_name,
        })
        print(f"  Найден: {name} ({account_name or '?'} / {project_name or '?'})")

    return {
        "id": server_id,
        "name": f"vps-{server_id}",
        "host": host,
        "port": 22,
        "user": SSH_USER,
        "password": SSH_PASS,
        "scripts": scripts,
    }


def main():
    """Основная функция."""
    data = {"servers": [], "accounts": []}

    for i, host in enumerate(SERVERS, 1):
        server = scan_server(host, i)
        if server:
            data["servers"].append(server)

    print(f"\nНайдено {len(data['servers'])} серверов")

    # Сохраняем
    output_file = Path(__file__).parent / "data.json"
    with open(output_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Сохранено в {output_file}")


if __name__ == "__main__":
    main()

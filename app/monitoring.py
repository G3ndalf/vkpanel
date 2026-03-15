"""
Мониторинг трафика — бизнес-логика.
Деплой агента, проверка доступности, сбор трафика через SSH.
"""
import base64
import json
import logging
import os
import secrets
import io
from datetime import datetime, timedelta
from typing import Optional

import paramiko

from .config import SSH_TIMEOUT, SSH_COMMAND_TIMEOUT

logger = logging.getLogger(__name__)

SSH_KEYS_DIR = os.getenv("SSH_KEYS_DIR", "/opt/vkpanel/ssh_keys")

# Актуальная версия агента — должна совпадать с AGENT_VERSION в agent_cron.py
CURRENT_AGENT_VERSION = "2.0.0"

# Содержимое агента — читается один раз при старте
_AGENT_SCRIPT: Optional[str] = None


def _get_agent_script() -> str:
    """Загружает скрипт агента из файла или возвращает встроенный."""
    global _AGENT_SCRIPT
    if _AGENT_SCRIPT is not None:
        return _AGENT_SCRIPT

    # Пробуем прочитать из файла рядом с проектом
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "agent_cron.py"),
        "/opt/vkpanel/agent_cron.py",
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path) as f:
                _AGENT_SCRIPT = f.read()
            return _AGENT_SCRIPT

    # Встроенная минимальная версия
    _AGENT_SCRIPT = _EMBEDDED_AGENT
    return _AGENT_SCRIPT


# Минимальный встроенный агент v2 (на случай если файл не найден)
_EMBEDDED_AGENT = '''#!/usr/bin/env python3
"""Traffic Agent v2 — минимальный сборщик исходящего трафика."""
AGENT_VERSION = "2.0.0"
import os, sys, json, urllib.request, logging

CONFIG = {"SERVER_URL": "", "API_KEY": "", "LOG_FILE": "/var/log/traffic_agent.log"}
for path in ["/etc/traffic_agent.conf", os.path.expanduser("~/.traffic_agent.conf")]:
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip(\\'"\\').strip("\\'" )
                    if k in CONFIG: CONFIG[k] = v
        break
for key in CONFIG:
    env_val = os.getenv(key)
    if env_val: CONFIG[key] = env_val

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(CONFIG["LOG_FILE"]), logging.StreamHandler()])
logger = logging.getLogger("traffic_agent")

def read_tx_bytes():
    total_tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.split(":")
                if len(parts) != 2: continue
                iface = parts[0].strip()
                if iface in ("lo",) or iface.startswith(("veth", "br-", "docker")): continue
                vals = parts[1].split()
                if len(vals) >= 10: total_tx += int(vals[8])
    except Exception as e: logger.error(f"Read error: {e}")
    return total_tx

def send_report(tx_bytes):
    url = CONFIG["SERVER_URL"].rstrip("/") + "/api/v1/report"
    data = json.dumps({"tx_bytes": tx_bytes, "version": AGENT_VERSION}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/json", "X-API-Key": CONFIG["API_KEY"]}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200: return True
            logger.error(f"Server returned {resp.status}"); return False
    except Exception as e: logger.error(f"Send error: {e}"); return False

if __name__ == "__main__":
    if not CONFIG["SERVER_URL"] or not CONFIG["API_KEY"]:
        logger.error("Configure SERVER_URL and API_KEY"); sys.exit(1)
    tx = read_tx_bytes()
    logger.info(f"TX bytes: {tx} ({tx / (1000**3):.2f} GB)")
    if send_report(tx): logger.info("Report sent OK"); sys.exit(0)
    else: logger.error("Failed"); sys.exit(1)
'''


def _ssh_connect_by_key(ip: str, key_path: str, ssh_user: str = "ubuntu") -> paramiko.SSHClient:
    """SSH подключение по ключу."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, port=22, username=ssh_user, key_filename=key_path, timeout=SSH_TIMEOUT)
    return client


def _ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = SSH_COMMAND_TIMEOUT) -> tuple[int, str, str]:
    """Выполнить команду."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    return code, stdout.read().decode(), stderr.read().decode()


def save_ssh_key(name: str, key_content: bytes) -> str:
    """Сохраняет SSH ключ на диск. Возвращает путь."""
    os.makedirs(SSH_KEYS_DIR, exist_ok=True)
    # Очищаем имя от опасных символов
    safe_name = "".join(c for c in name if c.isalnum() or c in ".-_")
    path = os.path.join(SSH_KEYS_DIR, f"{safe_name}.pem")
    with open(path, "wb") as f:
        f.write(key_content)
    os.chmod(path, 0o600)
    logger.info(f"SSH key saved: {path}")
    return path


def get_ssh_key_path(data: dict, ip: str, tenant_name: Optional[str] = None) -> Optional[str]:
    """Получить путь к SSH ключу: сначала для IP, потом для арендатора."""
    monitoring = data.get("monitoring", {})

    # Проверяем ключ для конкретного IP
    ip_key = monitoring.get("ip_ssh_keys", {}).get(ip, {})
    if ip_key and ip_key.get("key_path") and os.path.exists(ip_key["key_path"]):
        return ip_key["key_path"]

    # Проверяем ключ арендатора
    if tenant_name:
        tenant_key = monitoring.get("ssh_keys", {}).get(tenant_name, {})
        if tenant_key and tenant_key.get("key_path") and os.path.exists(tenant_key["key_path"]):
            return tenant_key["key_path"]

    return None


def get_ssh_user(data: dict, ip: str) -> str:
    """Получить SSH пользователя для IP (по умолчанию root)."""
    return data.get("monitoring", {}).get("ip_ssh_users", {}).get(ip, "ubuntu")


def check_agent_version(ip: str, key_path: str, ssh_user: str = "ubuntu") -> dict:
    """Проверяет версию агента на сервере. Возвращает {"version": str|None, "up_to_date": bool}."""
    try:
        client = _ssh_connect_by_key(ip, key_path, ssh_user)
        code, out, _ = _ssh_exec(client, "python3 -c \"exec(open('/opt/traffic_agent/agent.py').read().split('CONFIG')[0]); print(AGENT_VERSION)\" 2>/dev/null || echo NONE", timeout=10)
        client.close()
        version = out.strip() if code == 0 and out.strip() != "NONE" else None
        return {
            "version": version,
            "up_to_date": version == CURRENT_AGENT_VERSION if version else False,
            "current_version": CURRENT_AGENT_VERSION,
        }
    except Exception as e:
        return {"version": None, "up_to_date": False, "current_version": CURRENT_AGENT_VERSION, "error": str(e)[:200]}


def check_ssh_reachable(ip: str, key_path: str, ssh_user: str = "ubuntu") -> dict:
    """Проверить доступность сервера по SSH."""
    try:
        client = _ssh_connect_by_key(ip, key_path, ssh_user)
        code, out, _ = _ssh_exec(client, "echo OK", timeout=10)
        client.close()
        return {"reachable": code == 0, "error": None}
    except Exception as e:
        return {"reachable": False, "error": str(e)[:200]}


def deploy_agent(ip: str, key_path: str, ssh_user: str, panel_url: str) -> dict:
    """
    Разворачивает агент мониторинга трафика на сервере.
    Возвращает {"ok": bool, "message": str, "api_key": str}.
    """
    api_key = secrets.token_urlsafe(32)
    agent_script = _get_agent_script()
    agent_b64 = base64.b64encode(agent_script.encode()).decode()

    try:
        client = _ssh_connect_by_key(ip, key_path, ssh_user)

        # Устанавливаем python3 если нет
        _ssh_exec(client, "which python3 || (apt-get update -qq && apt-get install -y -qq python3)", timeout=120)

        # Создаём директории
        _ssh_exec(client, "mkdir -p /opt/traffic_agent /var/lib/traffic_agent")

        # Загружаем скрипт через base64
        _ssh_exec(client, f"echo '{agent_b64}' | base64 -d > /opt/traffic_agent/agent.py && chmod +x /opt/traffic_agent/agent.py")

        # Создаём конфиг
        conf = f"SERVER_URL={panel_url}\nAPI_KEY={api_key}\nLOG_FILE=/var/log/traffic_agent.log"
        conf_b64 = base64.b64encode(conf.encode()).decode()
        _ssh_exec(client, f"echo '{conf_b64}' | base64 -d > /etc/traffic_agent.conf && chmod 600 /etc/traffic_agent.conf")

        # Cron: 02:00 и 14:00 МСК (= 23:00 и 11:00 UTC)
        cron_job = "0 23,11 * * * /usr/bin/python3 /opt/traffic_agent/agent.py >> /var/log/traffic_agent.log 2>&1"
        _ssh_exec(client, f'(crontab -l 2>/dev/null | grep -v traffic_agent; echo "{cron_job}") | crontab -')

        # Удаляем старый systemd сервис если есть
        _ssh_exec(client, "systemctl stop traffic-agent 2>/dev/null; systemctl disable traffic-agent 2>/dev/null; rm -f /etc/systemd/system/traffic-agent.service; systemctl daemon-reload 2>/dev/null")

        # Первый запуск
        code, out, err = _ssh_exec(client, "/usr/bin/python3 /opt/traffic_agent/agent.py", timeout=60)
        client.close()

        if code == 0:
            return {"ok": True, "message": "Агент развёрнут и отправил первый отчёт", "api_key": api_key}
        else:
            # Агент развёрнут, но первый отчёт не прошёл (панель может быть недоступна извне)
            return {"ok": True, "message": f"Агент развёрнут, первый отчёт: {(err or out)[:150]}", "api_key": api_key}

    except Exception as e:
        logger.error(f"Deploy agent to {ip} failed: {e}")
        return {"ok": False, "message": str(e)[:300], "api_key": ""}


def trigger_agent(ip: str, key_path: str, ssh_user: str = "ubuntu") -> dict:
    """Принудительно запускает агент на сервере (он сам отправит отчёт на панель)."""
    try:
        client = _ssh_connect_by_key(ip, key_path, ssh_user)
        code, out, err = _ssh_exec(client, "/usr/bin/python3 /opt/traffic_agent/agent.py", timeout=60)
        client.close()
        if code == 0:
            return {"ok": True, "message": f"Агент на {ip} отправил отчёт"}
        else:
            return {"ok": False, "message": f"{(err or out)[:200]}"}
    except Exception as e:
        logger.error(f"Trigger agent on {ip} failed: {e}")
        return {"ok": False, "message": str(e)[:300]}


def remove_agent(ip: str, key_path: str, ssh_user: str = "ubuntu") -> dict:
    """
    Удаляет агент мониторинга с сервера.
    Убирает: скрипт, конфиг, cron, state, логи.
    """
    try:
        client = _ssh_connect_by_key(ip, key_path, ssh_user)

        # Удаляем cron задачу
        _ssh_exec(client, "(crontab -l 2>/dev/null | grep -v traffic_agent) | crontab -")

        # Удаляем файлы агента
        _ssh_exec(client, "rm -rf /opt/traffic_agent /var/lib/traffic_agent /etc/traffic_agent.conf /var/log/traffic_agent.log")

        client.close()
        return {"ok": True, "message": f"Агент удалён с {ip}"}
    except Exception as e:
        logger.error(f"Remove agent from {ip} failed: {e}")
        return {"ok": False, "message": str(e)[:300]}


def get_tenant_ips(data: dict, tenant_name: str) -> list[str]:
    """Получить список IP арендатора."""
    tenants = data.get("tenants", [])
    tenant = next((t for t in tenants if t["name"] == tenant_name), None)
    if not tenant:
        return []
    return tenant.get("ips", [])

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


# Минимальный встроенный агент (на случай если файл не найден)
_EMBEDDED_AGENT = '''#!/usr/bin/env python3
"""Traffic Agent — cron version with reboot persistence."""
import os, sys, json, urllib.request, logging

CONFIG = {"SERVER_URL": "", "API_KEY": "", "LOG_FILE": "/var/log/traffic_agent.log"}
STATE_DIR = "/var/lib/traffic_agent"
STATE_FILE = os.path.join(STATE_DIR, "state.json")

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

def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {}

def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

def read_raw_traffic():
    stats = {}
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.split(":")
                if len(parts) != 2: continue
                iface = parts[0].strip()
                if iface in ("lo",) or iface.startswith(("veth", "br-", "docker")): continue
                vals = parts[1].split()
                if len(vals) >= 10:
                    stats[iface] = {"rx_bytes": int(vals[0]), "tx_bytes": int(vals[8])}
    except Exception as e: logger.error(f"Read error: {e}")
    return stats

def compute_traffic(raw_stats):
    state = load_state()
    result = {}
    for iface, raw in raw_stats.items():
        prev = state.get(iface, {})
        prev_rx, prev_tx = prev.get("prev_rx", 0), prev.get("prev_tx", 0)
        base_rx, base_tx = prev.get("base_rx", 0), prev.get("base_tx", 0)
        if raw["rx_bytes"] < prev_rx: base_rx += prev_rx
        if raw["tx_bytes"] < prev_tx: base_tx += prev_tx
        state[iface] = {"prev_rx": raw["rx_bytes"], "prev_tx": raw["tx_bytes"], "base_rx": base_rx, "base_tx": base_tx}
        result[iface] = {"rx_bytes": base_rx + raw["rx_bytes"], "tx_bytes": base_tx + raw["tx_bytes"]}
    save_state(state)
    return result

def send_report(stats):
    url = CONFIG["SERVER_URL"].rstrip("/") + "/api/v1/report"
    data = json.dumps({"interfaces": stats}).encode("utf-8")
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
    raw = read_raw_traffic()
    if not raw: logger.error("No interfaces"); sys.exit(1)
    stats = compute_traffic(raw)
    if send_report(stats): logger.info(f"OK: {len(stats)} ifaces"); sys.exit(0)
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

        # Настраиваем cron (каждую ночь в 03:00)
        cron_job = "0 3 * * * /usr/bin/python3 /opt/traffic_agent/agent.py >> /var/log/traffic_agent.log 2>&1"
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


def collect_traffic(ip: str, key_path: str, ssh_user: str, days: int = 1,
                    date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    """
    Собирает данные о трафике с сервера.
    Возвращает {"ok": bool, "traffic": dict, "error": str|None}.
    date_from/date_to — строки YYYY-MM-DD для фильтрации логов по периоду.
    """
    try:
        client = _ssh_connect_by_key(ip, key_path, ssh_user)

        # Читаем текущий трафик из /proc/net/dev
        code, out, _ = _ssh_exec(client, "cat /proc/net/dev")
        raw_traffic = {}
        if code == 0:
            for line in out.strip().split("\n")[2:]:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                iface = parts[0].strip()
                if iface in ("lo",) or iface.startswith(("veth", "br-", "docker")):
                    continue
                vals = parts[1].split()
                if len(vals) >= 10:
                    raw_traffic[iface] = {
                        "rx_bytes": int(vals[0]),
                        "tx_bytes": int(vals[8]),
                    }

        # Читаем state файл (персистентные данные)
        code, out, _ = _ssh_exec(client, "cat /var/lib/traffic_agent/state.json 2>/dev/null")
        state = {}
        if code == 0 and out.strip():
            try:
                state = json.loads(out)
            except json.JSONDecodeError:
                pass

        # Вычисляем полный трафик с учётом базы (ребутов)
        traffic = {}
        for iface, raw in raw_traffic.items():
            st = state.get(iface, {})
            base_rx = st.get("base_rx", 0)
            base_tx = st.get("base_tx", 0)
            traffic[iface] = {
                "rx_bytes": base_rx + raw["rx_bytes"],
                "tx_bytes": base_tx + raw["tx_bytes"],
            }

        # Парсим лог за период
        log_entries = []
        if date_from and date_to:
            cutoff_start = date_from
            cutoff_end = date_to
        elif days > 0:
            cutoff_start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
            cutoff_end = datetime.utcnow().strftime("%Y-%m-%d")
        else:
            cutoff_start = cutoff_end = None

        if cutoff_start and cutoff_end:
            code, out, _ = _ssh_exec(client, f"cat /var/log/traffic_agent.log 2>/dev/null | grep '^{cutoff_start[:4]}' || true", timeout=30)
            if code == 0 and out.strip():
                for line in out.strip().split("\n"):
                    # Формат: "2026-03-07 03:00:00 | OK: 2 interfaces sent"
                    if cutoff_start <= line[:10] <= cutoff_end:
                        log_entries.append(line.strip())

        # Считаем суммарный трафик
        total_rx = sum(v["rx_bytes"] for v in traffic.values())
        total_tx = sum(v["tx_bytes"] for v in traffic.values())

        client.close()

        return {
            "ok": True,
            "traffic": traffic,
            "total_rx_bytes": total_rx,
            "total_tx_bytes": total_tx,
            "total_rx_gb": round(total_rx / (1000**3), 2),
            "total_tx_gb": round(total_tx / (1000**3), 2),
            "total_gb": round((total_rx + total_tx) / (1000**3), 2),
            "log_entries": log_entries[-50:],  # Последние 50 записей
            "state": state,
            "error": None,
        }

    except Exception as e:
        logger.error(f"Collect traffic from {ip} failed: {e}")
        return {"ok": False, "traffic": {}, "total_rx_gb": 0, "total_tx_gb": 0, "total_gb": 0, "error": str(e)[:300]}


def get_tenant_ips(data: dict, tenant_name: str) -> list[str]:
    """Получить список IP арендатора."""
    tenants = data.get("tenants", [])
    tenant = next((t for t in tenants if t["name"] == tenant_name), None)
    if not tenant:
        return []
    return tenant.get("ips", [])

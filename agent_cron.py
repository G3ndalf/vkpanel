#!/usr/bin/env python3
"""
Traffic Agent — однократный запуск для cron.
Читает трафик и отправляет на сервер биллинга.

Хранит состояние в /var/lib/traffic_agent/state.json чтобы
при ребуте сервера (когда /proc/net/dev обнуляется) трафик
не терялся — к текущим значениям прибавляется накопленная база.
"""

AGENT_VERSION = "1.1.0"  # Версия агента — бампить при изменениях

import os
import sys
import json
import urllib.request
import logging

CONFIG = {
    "SERVER_URL": "",
    "API_KEY": "",
    "LOG_FILE": "/var/log/traffic_agent.log"
}

STATE_DIR = "/var/lib/traffic_agent"
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Загрузка конфига
for path in ["/etc/traffic_agent.conf", os.path.expanduser("~/.traffic_agent.conf")]:
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k in CONFIG:
                        CONFIG[k] = v
        break

# Переменные окружения имеют приоритет
for key in CONFIG:
    env_val = os.getenv(key)
    if env_val:
        CONFIG[key] = env_val

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("traffic_agent")


def load_state():
    """Загружает сохранённое состояние (предыдущие значения + накопленная база)."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    """Сохраняет состояние на диск."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def read_raw_traffic():
    """Читает текущие значения из /proc/net/dev (сбрасываются при ребуте)."""
    stats = {}
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                iface = parts[0].strip()
                # Пропускаем loopback и виртуальные интерфейсы
                if iface in ("lo",) or iface.startswith(("veth", "br-", "docker")):
                    continue
                vals = parts[1].split()
                if len(vals) >= 10:
                    stats[iface] = {
                        "rx_bytes": int(vals[0]),
                        "tx_bytes": int(vals[8])
                    }
    except Exception as e:
        logger.error(f"Read error: {e}")
    return stats


def compute_traffic(raw_stats):
    """
    Вычисляет реальный трафик с учётом ребутов.

    Состояние хранит для каждого интерфейса:
      - prev_rx / prev_tx: предыдущие raw-значения из /proc/net/dev
      - base_rx / base_tx: накопленная база (суммируется при обнаружении ребута)

    Если текущее raw-значение < предыдущего — значит был ребут,
    и мы добавляем предыдущее значение к базе.

    Итоговый трафик = base + current_raw
    """
    state = load_state()
    result = {}

    for iface, raw in raw_stats.items():
        prev = state.get(iface, {})
        prev_rx = prev.get("prev_rx", 0)
        prev_tx = prev.get("prev_tx", 0)
        base_rx = prev.get("base_rx", 0)
        base_tx = prev.get("base_tx", 0)

        # Детект ребута: текущее значение меньше предыдущего
        if raw["rx_bytes"] < prev_rx:
            base_rx += prev_rx
            logger.info(f"Reboot detected on {iface} (RX reset), adding {prev_rx} to base")
        if raw["tx_bytes"] < prev_tx:
            base_tx += prev_tx
            logger.info(f"Reboot detected on {iface} (TX reset), adding {prev_tx} to base")

        # Сохраняем текущие raw-значения и обновлённую базу
        state[iface] = {
            "prev_rx": raw["rx_bytes"],
            "prev_tx": raw["tx_bytes"],
            "base_rx": base_rx,
            "base_tx": base_tx
        }

        # Итоговый трафик = база + текущее raw
        result[iface] = {
            "rx_bytes": base_rx + raw["rx_bytes"],
            "tx_bytes": base_tx + raw["tx_bytes"]
        }

    save_state(state)
    return result


def save_daily_snapshot(stats):
    """
    Сохраняет ежедневный снапшот кумулятивного трафика.
    Файл: /var/lib/traffic_agent/history/YYYY-MM-DD.json
    Перезаписывается при повторном запуске в тот же день.
    """
    from datetime import datetime
    history_dir = os.path.join(STATE_DIR, "history")
    os.makedirs(history_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    snapshot_path = os.path.join(history_dir, f"{today}.json")
    snapshot = {
        "date": today,
        "interfaces": stats,
    }
    try:
        with open(snapshot_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        logger.info(f"Snapshot saved: {snapshot_path}")
    except Exception as e:
        logger.error(f"Failed to save snapshot: {e}")


def send_report(stats):
    """Отправляет данные на сервер биллинга."""
    url = CONFIG["SERVER_URL"].rstrip("/") + "/api/v1/report"
    data = json.dumps({"interfaces": stats, "version": AGENT_VERSION}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": CONFIG["API_KEY"]
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                return True
            logger.error(f"Server returned {resp.status}")
            return False
    except Exception as e:
        logger.error(f"Send error: {e}")
        return False


if __name__ == "__main__":
    if not CONFIG["SERVER_URL"] or not CONFIG["API_KEY"]:
        logger.error("Configure SERVER_URL and API_KEY in /etc/traffic_agent.conf")
        sys.exit(1)

    raw_stats = read_raw_traffic()

    if not raw_stats:
        logger.error("No interfaces found")
        sys.exit(1)

    # Вычисляем трафик с учётом ребутов
    stats = compute_traffic(raw_stats)

    # Сохраняем ежедневный снапшот для расчёта трафика за период
    save_daily_snapshot(stats)

    if send_report(stats):
        logger.info(f"OK: {len(stats)} interfaces sent")
        sys.exit(0)
    else:
        logger.error("Failed to send report")
        sys.exit(1)

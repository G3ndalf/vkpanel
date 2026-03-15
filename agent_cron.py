#!/usr/bin/env python3
"""
Traffic Agent v2 — минимальный сборщик исходящего трафика.
Читает TX из /proc/net/dev и отправляет на панель.
Вся логика подсчёта на стороне панели.

Cron: 0 23,11 * * * (= 02:00, 14:00 МСК)
"""

AGENT_VERSION = "2.0.0"

import os
import sys
import json
import urllib.request
import logging

CONFIG = {
    "SERVER_URL": "",
    "API_KEY": "",
    "LOG_FILE": "/var/log/traffic_agent.log",
}

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
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("traffic_agent")


def read_tx_bytes() -> int:
    """Читает суммарный исходящий трафик из /proc/net/dev (все интерфейсы кроме lo/виртуальных)."""
    total_tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                iface = parts[0].strip()
                # Пропускаем loopback и виртуальные
                if iface in ("lo",) or iface.startswith(("veth", "br-", "docker")):
                    continue
                vals = parts[1].split()
                if len(vals) >= 10:
                    total_tx += int(vals[8])  # TX bytes — 9-е поле
    except Exception as e:
        logger.error(f"Read /proc/net/dev error: {e}")
    return total_tx


def send_report(tx_bytes: int) -> bool:
    """Отправляет TX на панель."""
    url = CONFIG["SERVER_URL"].rstrip("/") + "/api/v1/report"
    data = json.dumps({"tx_bytes": tx_bytes, "version": AGENT_VERSION}).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": CONFIG["API_KEY"],
        },
        method="POST",
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

    tx = read_tx_bytes()
    if tx == 0:
        logger.warning("TX = 0, sending anyway")

    logger.info(f"TX bytes: {tx} ({tx / (1000**3):.2f} GB)")

    if send_report(tx):
        logger.info("Report sent OK")
        sys.exit(0)
    else:
        logger.error("Failed to send report")
        sys.exit(1)

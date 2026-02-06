#!/usr/bin/env python3
"""
Скрипт для автоматического сканирования серверов и добавления в панель.
"""
import json
import subprocess

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

SSH_USER = "root"
SSH_PASS = "Xlmmama_609)"

def ssh_exec(host, cmd):
    """Выполнить команду по SSH."""
    full_cmd = f"sshpass -p '{SSH_PASS}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {SSH_USER}@{host} \"{cmd}\""
    try:
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.stdout.strip()
    except:
        return ""

def scan_server(host, server_id):
    """Сканировать сервер и вернуть структуру данных."""
    print(f"Сканирую {host}...")
    
    # Получить список сервисов
    output = ssh_exec(host, "systemctl list-units 'vk-fip@*' --all --no-pager 2>/dev/null | grep -oE 'vk-fip@[a-zA-Z0-9]+\\.service'")
    
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
    
    # Также получим инфу об аккаунте из .env файлов
    scripts = []
    for i, name in enumerate(sorted(services), 1):
        # Попробуем прочитать OS_USERNAME и OS_PROJECT_NAME из .env
        env_output = ssh_exec(host, f"grep -E '^OS_USERNAME=|^OS_PROJECT_NAME=' /root/{name}/.env 2>/dev/null")
        
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
    data = {"servers": [], "accounts": []}
    
    for i, host in enumerate(SERVERS, 1):
        server = scan_server(host, i)
        if server:
            data["servers"].append(server)
    
    # Сохранить
    print(f"\nНайдено {len(data['servers'])} серверов")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    
    with open("data.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print("\nСохранено в data.json")


if __name__ == "__main__":
    main()

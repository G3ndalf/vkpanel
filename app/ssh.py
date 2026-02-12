"""
SSH функции для работы с серверами.
"""
import json
import logging
from typing import Optional

import paramiko

from .config import SSH_TIMEOUT, SSH_COMMAND_TIMEOUT

logger = logging.getLogger(__name__)


def ssh_connect(
    host: str,
    port: int,
    user: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
) -> paramiko.SSHClient:
    """Подключиться к серверу по SSH."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        if key_path:
            client.connect(
                host, port=port, username=user,
                key_filename=key_path, timeout=SSH_TIMEOUT
            )
        else:
            client.connect(
                host, port=port, username=user,
                password=password, timeout=SSH_TIMEOUT
            )
        return client
    except Exception as e:
        logger.error(f"SSH connect failed to {host}: {e}")
        raise


def ssh_exec(
    client: paramiko.SSHClient,
    cmd: str,
    timeout: int = SSH_COMMAND_TIMEOUT,
) -> tuple[int, str, str]:
    """Выполнить команду по SSH."""
    try:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()
    except Exception as e:
        logger.error(f"SSH exec failed: {cmd[:50]}... - {e}")
        raise


def get_script_status(server: dict, script: dict) -> dict:
    """
    Получить статус скрипта через SSH.
    
    Возвращает dict с полями:
    - running: bool
    - cycles: int
    - success: int
    - last_ip: str | None
    - account: str | None
    - project: str | None
    - error: str | None
    """
    result = {
        "running": False,
        "error": None,
        "state": None,
        "cycles": 0,
        "success": 0,
        "last_ip": None,
        "account": None,
        "project": None,
    }

    client = None
    try:
        client = ssh_connect(
            server["host"],
            server.get("port", 22),
            server["user"],
            server.get("password"),
            server.get("key_path"),
        )

        # Проверяем статус systemd сервиса
        service_name = script.get("service_name", f"vkip-{script['name']}")
        code, out, err = ssh_exec(client, f"systemctl is-active {service_name}")
        result["running"] = out.strip() == "active"

        # Читаем OS_USERNAME и OS_PROJECT_NAME из .env
        env_file = f"{script['path']}/.env"
        code, out, err = ssh_exec(
            client,
            f"grep -E '^OS_USERNAME=|^OS_PROJECT_NAME=' {env_file} 2>/dev/null"
        )
        if code == 0 and out.strip():
            for line in out.strip().split("\n"):
                if line.startswith("OS_USERNAME="):
                    result["account"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("OS_PROJECT_NAME="):
                    result["project"] = line.split("=", 1)[1].strip().strip('"').strip("'")

        # Читаем state файл
        state_file = script.get("state_file", f"{script['path']}/vk_fip_state.json")
        code, out, err = ssh_exec(client, f"cat {state_file} 2>/dev/null")

        if code == 0 and out.strip():
            try:
                state = json.loads(out)
                result["state"] = state

                meta = state.get("meta", {})
                result["cycles"] = meta.get("cycle_no", 0)

                stats = meta.get("stats", {})
                total_success = sum(s.get("success", 0) for s in stats.values())
                result["success"] = total_success

                # Последний пойманный IP
                allocated = state.get("allocated", {})
                for subnet_ips in allocated.values():
                    if subnet_ips:
                        last = subnet_ips[-1]
                        result["last_ip"] = last.get("floating_ip")
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse state file: {e}")

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"get_script_status failed for {server.get('name')}/{script.get('name')}: {e}")
    finally:
        if client:
            client.close()

    return result


def control_script(server: dict, script: dict, action: str) -> tuple[bool, str]:
    """
    Управление скриптом: start/stop/restart.
    
    Возвращает (success, message).
    """
    if action not in ("start", "stop", "restart"):
        return False, f"Invalid action: {action}"

    client = None
    try:
        client = ssh_connect(
            server["host"],
            server.get("port", 22),
            server["user"],
            server.get("password"),
            server.get("key_path"),
        )

        service_name = script.get("service_name", f"vkip-{script['name']}")
        code, out, err = ssh_exec(client, f"systemctl {action} {service_name}")

        if code == 0:
            logger.info(f"Script {action}: {server['name']}/{script['name']} - OK")
            return True, f"Service {action} OK"
        else:
            error_msg = err.strip() or out.strip() or f"Exit code {code}"
            logger.error(f"Script {action} failed: {server['name']}/{script['name']} - {error_msg}")
            return False, error_msg

    except Exception as e:
        logger.error(f"control_script failed: {server.get('name')}/{script.get('name')} - {e}")
        return False, str(e)
    finally:
        if client:
            client.close()


def get_floating_ips_via_cli(server: dict, script: dict) -> dict:
    """
    Получить список floating IP из проекта через openstack CLI.
    
    Возвращает dict с полями:
    - ips: list[dict]
    - account: str | None
    - project: str | None
    - error: str | None
    """
    result = {
        "ips": [],
        "error": None,
        "account": None,
        "project": None,
    }

    client = None
    try:
        client = ssh_connect(
            server["host"],
            server.get("port", 22),
            server["user"],
            server.get("password"),
            server.get("key_path"),
        )

        env_file = f"{script['path']}/.env"

        # Получаем floating ip list через openstack CLI
        cmd = (
            f"cd {script['path']} && "
            f"export $(grep -E '^OS_' .env | grep -v '#' | xargs) && "
            f"openstack floating ip list -f json 2>&1"
        )
        code, out, err = ssh_exec(client, cmd, timeout=60)

        # Получаем account/project
        code2, env_out, _ = ssh_exec(
            client,
            f"grep -E '^OS_USERNAME=|^OS_PROJECT_NAME=' {env_file} 2>/dev/null"
        )
        if code2 == 0 and env_out.strip():
            for line in env_out.strip().split("\n"):
                if line.startswith("OS_USERNAME="):
                    result["account"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("OS_PROJECT_NAME="):
                    result["project"] = line.split("=", 1)[1].strip().strip('"').strip("'")

        if code == 0 and out.strip():
            try:
                ips_data = json.loads(out)
                for ip in ips_data:
                    result["ips"].append({
                        "ip": ip.get("Floating IP Address"),
                        "id": ip.get("ID"),
                        "status": ip.get("Status"),
                        "fixed_ip": ip.get("Fixed IP Address"),
                        "port_id": ip.get("Port"),
                        "attached": bool(ip.get("Port") or ip.get("Fixed IP Address")),
                    })
            except json.JSONDecodeError as e:
                result["error"] = f"JSON parse error: {e}"
                logger.error(f"Failed to parse openstack output: {e}")
        elif err:
            result["error"] = err[:200]

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"get_floating_ips_via_cli failed: {e}")
    finally:
        if client:
            client.close()

    return result


def change_script_project(server: dict, script: dict, project: dict) -> tuple[bool, str]:
    """
    Сменить проект (аккаунт) для скрипта.
    
    Обновляет .env файл на сервере и перезапускает скрипт.
    
    Args:
        server: данные сервера
        script: данные скрипта
        project: данные проекта (username, password, project_id, name, auth_url)
    
    Returns:
        (success, message)
    """
    client = None
    try:
        client = ssh_connect(
            server["host"],
            server.get("port", 22),
            server["user"],
            server.get("password"),
            server.get("key_path"),
        )

        env_file = f"{script['path']}/.env"
        service_name = script.get("service_name", f"vkip-{script['name']}")

        # Читаем текущий .env
        code, current_env, err = ssh_exec(client, f"cat {env_file}")
        if code != 0:
            return False, f"Failed to read .env: {err}"

        # Обновляем переменные
        new_lines = []
        updated_keys = set()
        
        updates = {
            "OS_USERNAME": project["username"],
            "OS_PASSWORD": project["password"],
            "OS_PROJECT_ID": project["project_id"],
            "OS_PROJECT_NAME": project.get("os_project_name") or project["name"],
        }
        
        # Если есть auth_url, обновляем и его
        if project.get("auth_url"):
            updates["OS_AUTH_URL"] = project["auth_url"]

        for line in current_env.split("\n"):
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("#"):
                new_lines.append(line)
                continue
            
            # Проверяем, нужно ли обновить эту строку
            key = line_stripped.split("=")[0] if "=" in line_stripped else None
            if key in updates:
                new_lines.append(f'{key}="{updates[key]}"')
                updated_keys.add(key)
            else:
                new_lines.append(line)
        
        # Добавляем отсутствующие ключи
        for key, value in updates.items():
            if key not in updated_keys:
                new_lines.append(f'{key}="{value}"')

        new_env = "\n".join(new_lines)

        # Записываем новый .env
        # Экранируем для bash
        escaped_env = new_env.replace("'", "'\\''")
        code, out, err = ssh_exec(client, f"echo '{escaped_env}' > {env_file}")
        if code != 0:
            return False, f"Failed to write .env: {err}"

        # Перезапускаем скрипт
        code, out, err = ssh_exec(client, f"systemctl restart {service_name}")
        if code != 0:
            return False, f"Failed to restart service: {err}"

        logger.info(f"Changed project for {server['name']}/{script['name']} to {project['name']}")
        return True, f"Проект изменён на {project['name']}, скрипт перезапущен"

    except Exception as e:
        logger.error(f"change_script_project failed: {e}")
        return False, str(e)
    finally:
        if client:
            client.close()

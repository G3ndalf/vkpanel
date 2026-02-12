"""
OpenStack API функции для работы с VK Cloud.
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def openstack_auth(
    auth_url: str,
    username: str,
    password: str,
    project_id: str,
) -> tuple[str, dict, str]:
    """
    Получить токен OpenStack через Keystone.
    
    Возвращает (token, endpoints_dict, project_name).
    """
    auth_payload = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": username,
                        "domain": {"name": "users"},
                        "password": password,
                    }
                },
            },
            "scope": {
                "project": {"id": project_id}
            },
        }
    }

    token_url = auth_url.rstrip("/") + "/auth/tokens"

    with httpx.Client(timeout=30) as client:
        resp = client.post(token_url, json=auth_payload)
        resp.raise_for_status()

        token = resp.headers.get("X-Subject-Token")
        catalog = resp.json().get("token", {}).get("catalog", [])

        # Собираем endpoints (только RegionOne)
        endpoints = {}
        for service in catalog:
            stype = service.get("type")
            for ep in service.get("endpoints", []):
                if ep.get("interface") == "public" and ep.get("region") == "RegionOne":
                    endpoints[stype] = ep.get("url")

        # Имя проекта из ответа Keystone
        project_name = resp.json().get("token", {}).get("project", {}).get("name", "")

        return token, endpoints, project_name


def openstack_get_floating_ips(token: str, network_endpoint: str) -> list[dict]:
    """Получить список floating IPs через Neutron API."""
    url = network_endpoint.rstrip("/") + "/v2.0/floatingips"

    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers={"X-Auth-Token": token})
        resp.raise_for_status()
        return resp.json().get("floatingips", [])


def openstack_get_servers(token: str, compute_endpoint: str) -> list[dict]:
    """Получить список серверов через Nova API."""
    url = compute_endpoint.rstrip("/") + "/servers/detail"

    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers={"X-Auth-Token": token})
        resp.raise_for_status()
        return resp.json().get("servers", [])


def get_project_floating_ips(project: dict) -> dict:
    """
    Получить все floating IPs для проекта через OpenStack API.
    
    Возвращает dict с полями:
    - name: str
    - username: str
    - ips: list[dict]
    - error: str | None
    """
    result = {
        "name": project["name"],
        "username": project["username"],
        "os_project_name": None,
        "ips": [],
        "error": None,
    }

    try:
        token, endpoints, os_project_name = openstack_auth(
            project["auth_url"],
            project["username"],
            project["password"],
            project["project_id"],
        )

        result["os_project_name"] = os_project_name

        network_ep = endpoints.get("network")
        compute_ep = endpoints.get("compute")

        if not network_ep:
            result["error"] = "No network endpoint"
            return result

        # Получаем floating IPs
        fips = openstack_get_floating_ips(token, network_ep)

        # Получаем серверы для маппинга fixed_ip -> server_name
        servers = []
        if compute_ep:
            try:
                servers = openstack_get_servers(token, compute_ep)
            except Exception as e:
                logger.warning(f"Failed to get servers: {e}")

        # Собираем информацию по IP
        for fip in fips:
            ip_info = {
                "ip": fip.get("floating_ip_address"),
                "id": fip.get("id"),
                "status": fip.get("status"),
                "fixed_ip": fip.get("fixed_ip_address"),
                "port_id": fip.get("port_id"),
                "attached": bool(fip.get("port_id")),
                "server_name": None,
            }

            # Ищем сервер по fixed_ip
            fixed_ip = fip.get("fixed_ip_address")
            if fixed_ip and servers:
                for srv in servers:
                    for network_addrs in srv.get("addresses", {}).values():
                        for addr in network_addrs:
                            if addr.get("addr") == fixed_ip:
                                ip_info["server_name"] = srv.get("name")
                                break

            result["ips"].append(ip_info)

    except httpx.HTTPStatusError as e:
        result["error"] = f"HTTP {e.response.status_code}"
        logger.error(f"OpenStack API error for {project['name']}: {e}")
    except Exception as e:
        result["error"] = str(e)[:100]
        logger.error(f"get_project_floating_ips failed for {project['name']}: {e}")

    return result

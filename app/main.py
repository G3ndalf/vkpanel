"""
VK IP Panel — главный модуль приложения.
FastAPI + Jinja2 + Paramiko для SSH.
"""
import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends, HTTPException, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import paramiko
import httpx

# ─── Конфиг ───────────────────────────────────────────────────

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "Haxoastemir29")
SECRET_KEY = os.getenv("SECRET_KEY", "vkpanel-secret-key-change-me-2026")
DATA_FILE = os.getenv("DATA_FILE", "/opt/vkpanel/data.json")

# ─── Данные (JSON файл вместо БД для простоты) ────────────────

def load_data() -> dict:
    """Загрузить данные из JSON файла."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                data.setdefault("servers", [])
                data.setdefault("accounts", [])
                data.setdefault("status_cache", {})
                data.setdefault("last_update", None)
                return data
        except:
            pass
    return {"servers": [], "accounts": [], "status_cache": {}, "last_update": None}


def save_data(data: dict):
    """Сохранить данные в JSON файл."""
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── SSH функции ──────────────────────────────────────────────

def ssh_connect(host: str, port: int, user: str, password: str = None, key_path: str = None) -> paramiko.SSHClient:
    """Подключиться к серверу по SSH."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    if key_path and os.path.exists(key_path):
        client.connect(host, port=port, username=user, key_filename=key_path, timeout=10)
    else:
        client.connect(host, port=port, username=user, password=password, timeout=10)
    
    return client


def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Выполнить команду по SSH."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, stdout.read().decode(), stderr.read().decode()


def get_script_status(server: dict, script: dict) -> dict:
    """Получить статус скрипта через SSH."""
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
    
    try:
        client = ssh_connect(
            server["host"], 
            server.get("port", 22),
            server["user"],
            server.get("password"),
            server.get("key_path")
        )
        
        # Проверяем статус systemd сервиса
        service_name = script.get("service_name", f"vkip-{script['name']}")
        code, out, err = ssh_exec(client, f"systemctl is-active {service_name}")
        result["running"] = out.strip() == "active"
        
        # Читаем OS_USERNAME и OS_PROJECT_NAME из .env
        env_file = f"{script['path']}/.env"
        code, out, err = ssh_exec(client, f"grep -E '^OS_USERNAME=|^OS_PROJECT_NAME=' {env_file} 2>/dev/null")
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
            except:
                pass
        
        client.close()
        
    except Exception as e:
        result["error"] = str(e)
    
    return result


def control_script(server: dict, script: dict, action: str) -> tuple[bool, str]:
    """Управление скриптом: start/stop/restart."""
    try:
        client = ssh_connect(
            server["host"],
            server.get("port", 22),
            server["user"],
            server.get("password"),
            server.get("key_path")
        )
        
        service_name = script.get("service_name", f"vkip-{script['name']}")
        code, out, err = ssh_exec(client, f"systemctl {action} {service_name}")
        client.close()
        
        if code == 0:
            return True, f"Service {action} OK"
        else:
            return False, err or out
            
    except Exception as e:
        return False, str(e)


# ─── FastAPI приложение ───────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    if not os.path.exists(DATA_FILE):
        save_data({"servers": [], "accounts": []})
    yield
    # Shutdown


app = FastAPI(title="VK IP Panel", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Статика и шаблоны
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ─── Авторизация ──────────────────────────────────────────────

def get_current_user(request: Request) -> Optional[str]:
    """Получить текущего пользователя из сессии."""
    return request.session.get("user")


def require_auth(request: Request):
    """Проверить авторизацию."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ─── Страницы ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    servers = data.get("servers", [])
    status_cache = data.get("status_cache", {})
    cloud_cache = data.get("cloud_cache", {})
    last_update = data.get("last_update")
    cloud_last_update = data.get("cloud_last_update")
    
    # Собираем скрипты с кэшированными статусами
    total_scripts = 0
    running_scripts = 0
    scripts_info = []
    
    for server in servers:
        for script in server.get("scripts", []):
            total_scripts += 1
            cache_key = f"{server['id']}-{script['id']}"
            cached = status_cache.get(cache_key, {})
            cloud = cloud_cache.get(cache_key, {})
            
            # Объединяем данные из status_cache и cloud_cache
            combined = {**cached}
            combined["floating_ips"] = cloud.get("ips", [])
            combined["account"] = cached.get("account") or cloud.get("account")
            combined["project"] = cached.get("project") or cloud.get("project")
            combined["cloud_error"] = cloud.get("error")
            
            if cached.get("running"):
                running_scripts += 1
            
            scripts_info.append({
                "server": server["name"],
                "server_id": server["id"],
                "script": script,
                "status": combined,
            })
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "servers": servers,
        "scripts_info": scripts_info,
        "total_scripts": total_scripts,
        "running_scripts": running_scripts,
        "last_update": last_update or cloud_last_update,
    })


@app.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    return templates.TemplateResponse("servers.html", {
        "request": request,
        "user": user,
        "servers": data.get("servers", []),
    })


@app.post("/servers/add")
async def add_server(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    user: str = Form(...),
    password: str = Form(None),
):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    
    # Генерируем ID
    server_id = max([s.get("id", 0) for s in data["servers"]] + [0]) + 1
    
    server = {
        "id": server_id,
        "name": name,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "scripts": [],
    }
    
    data["servers"].append(server)
    save_data(data)
    
    return RedirectResponse("/servers", status_code=303)


@app.post("/servers/{server_id}/delete")
async def delete_server(request: Request, server_id: int):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    data["servers"] = [s for s in data["servers"] if s.get("id") != server_id]
    save_data(data)
    
    return RedirectResponse("/servers", status_code=303)


@app.get("/servers/{server_id}/scripts", response_class=HTMLResponse)
async def server_scripts(request: Request, server_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    server = next((s for s in data["servers"] if s.get("id") == server_id), None)
    
    if not server:
        return RedirectResponse("/servers", status_code=303)
    
    status_cache = data.get("status_cache", {})
    cloud_cache = data.get("cloud_cache", {})
    
    # Берём статусы из кэша
    scripts_with_status = []
    for script in server.get("scripts", []):
        cache_key = f"{server['id']}-{script['id']}"
        cached = status_cache.get(cache_key, {})
        cloud = cloud_cache.get(cache_key, {})
        
        combined = {**cached}
        combined["floating_ips"] = cloud.get("ips", [])
        combined["account"] = cached.get("account") or cloud.get("account")
        combined["project"] = cached.get("project") or cloud.get("project")
        
        scripts_with_status.append({
            "script": script,
            "status": combined,
        })
    
    return templates.TemplateResponse("scripts.html", {
        "request": request,
        "user": user,
        "server": server,
        "scripts": scripts_with_status,
    })


@app.post("/servers/{server_id}/scripts/add")
async def add_script(
    request: Request,
    server_id: int,
    name: str = Form(...),
    path: str = Form(...),
    service_name: str = Form(...),
    account_name: str = Form(None),
    project_name: str = Form(None),
):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    server = next((s for s in data["servers"] if s.get("id") == server_id), None)
    
    if not server:
        return RedirectResponse("/servers", status_code=303)
    
    script_id = max([sc.get("id", 0) for sc in server.get("scripts", [])] + [0]) + 1
    
    script = {
        "id": script_id,
        "name": name,
        "path": path,
        "service_name": service_name,
        "state_file": f"{path}/vk_fip_state.json",
        "account_name": account_name,
        "project_name": project_name,
    }
    
    if "scripts" not in server:
        server["scripts"] = []
    server["scripts"].append(script)
    save_data(data)
    
    return RedirectResponse(f"/servers/{server_id}/scripts", status_code=303)


@app.post("/servers/{server_id}/scripts/{script_id}/delete")
async def delete_script(request: Request, server_id: int, script_id: int):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    server = next((s for s in data["servers"] if s.get("id") == server_id), None)
    
    if server:
        server["scripts"] = [s for s in server.get("scripts", []) if s.get("id") != script_id]
        save_data(data)
    
    return RedirectResponse(f"/servers/{server_id}/scripts", status_code=303)


@app.post("/servers/{server_id}/scripts/{script_id}/{action}")
async def script_action(request: Request, server_id: int, script_id: int, action: str):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)
    
    if action not in ("start", "stop", "restart"):
        return RedirectResponse("/", status_code=303)
    
    data = load_data()
    server = next((s for s in data["servers"] if s.get("id") == server_id), None)
    
    if server:
        script = next((s for s in server.get("scripts", []) if s.get("id") == script_id), None)
        if script:
            success, msg = control_script(server, script, action)
    
    # Возвращаемся туда откуда пришли
    referer = request.headers.get("referer", "/")
    return RedirectResponse(referer, status_code=303)


@app.get("/ips", response_class=HTMLResponse)
async def all_ips(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    all_ips_list = []
    
    for server in data.get("servers", []):
        for script in server.get("scripts", []):
            status = get_script_status(server, script)
            if status["state"]:
                allocated = status["state"].get("allocated", {})
                for subnet, ips in allocated.items():
                    for ip_info in ips:
                        all_ips_list.append({
                            "ip": ip_info.get("floating_ip"),
                            "fip_id": ip_info.get("fip_id"),
                            "created_at": ip_info.get("created_at"),
                            "subnet": subnet,
                            "server": server["name"],
                            "script": script["name"],
                            "account": script.get("account_name", "-"),
                            "project": script.get("project_name", "-"),
                        })
    
    # Сортируем по дате (новые первые)
    all_ips_list.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    
    return templates.TemplateResponse("ips.html", {
        "request": request,
        "user": user,
        "ips": all_ips_list,
    })


# ─── OpenStack функции ────────────────────────────────────────

def get_floating_ips_from_project(server: dict, script: dict) -> dict:
    """Получить список floating IP из проекта через openstack CLI."""
    result = {
        "ips": [],
        "error": None,
        "account": None,
        "project": None,
    }
    
    try:
        client = ssh_connect(
            server["host"],
            server.get("port", 22),
            server["user"],
            server.get("password"),
            server.get("key_path")
        )
        
        # Читаем креды из .env
        env_file = f"{script['path']}/.env"
        
        # Получаем floating ip list через openstack CLI
        # export $(grep...) работает лучше чем source для .env с спецсимволами
        cmd = f"cd {script['path']} && export $(grep -E '^OS_' .env | grep -v '#' | xargs) && openstack floating ip list -f json 2>&1"
        code, out, err = ssh_exec(client, cmd, timeout=60)
        
        # Получаем account/project
        code2, env_out, _ = ssh_exec(client, f"grep -E '^OS_USERNAME=|^OS_PROJECT_NAME=' {env_file} 2>/dev/null")
        if code2 == 0 and env_out.strip():
            for line in env_out.strip().split("\n"):
                if line.startswith("OS_USERNAME="):
                    result["account"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("OS_PROJECT_NAME="):
                    result["project"] = line.split("=", 1)[1].strip().strip('"').strip("'")
        
        client.close()
        
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
            except Exception as e:
                result["error"] = f"JSON parse error: {e}"
        elif err:
            result["error"] = err[:200]
            
    except Exception as e:
        result["error"] = str(e)
    
    return result


# ─── API для AJAX ─────────────────────────────────────────────

@app.post("/api/refresh")
async def api_refresh_status(request: Request):
    """Обновить статусы всех скриптов и сохранить в кэш."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)
    
    data = load_data()
    
    # Собираем задачи для параллельного выполнения
    import concurrent.futures
    from datetime import datetime
    
    tasks = []
    for server in data.get("servers", []):
        for script in server.get("scripts", []):
            tasks.append((server, script))
    
    # Выполняем параллельно с ограничением потоков
    def fetch_status(args):
        server, script = args
        status = get_script_status(server, script)
        return {
            "server_id": server["id"],
            "script_id": script["id"],
            "server_name": server["name"],
            "script_name": script["name"],
            "running": status["running"],
            "cycles": status["cycles"],
            "success": status["success"],
            "last_ip": status["last_ip"],
            "error": status["error"],
            "account": status["account"],
            "project": status["project"],
        }
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_status, tasks))
    
    # Сохраняем в кэш
    status_cache = {}
    for r in results:
        cache_key = f"{r['server_id']}-{r['script_id']}"
        status_cache[cache_key] = r
    
    data["status_cache"] = status_cache
    data["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_data(data)
    
    return {"ok": True, "updated": len(results), "last_update": data["last_update"]}


@app.get("/api/status")
async def api_status(request: Request):
    """Получить кэшированные статусы."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)
    
    data = load_data()
    return {
        "status_cache": data.get("status_cache", {}),
        "last_update": data.get("last_update"),
    }


@app.get("/api/status/{server_id}/{script_id}")
async def api_script_status(request: Request, server_id: int, script_id: int):
    """Получить статус одного скрипта."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)
    
    data = load_data()
    server = next((s for s in data["servers"] if s["id"] == server_id), None)
    if not server:
        raise HTTPException(status_code=404)
    
    script = next((s for s in server.get("scripts", []) if s["id"] == script_id), None)
    if not script:
        raise HTTPException(status_code=404)
    
    status = get_script_status(server, script)
    return {
        "server_id": server_id,
        "script_id": script_id,
        "running": status["running"],
        "cycles": status["cycles"],
        "success": status["success"],
        "last_ip": status["last_ip"],
        "error": status["error"],
    }


@app.get("/cloud", response_class=HTMLResponse)
async def cloud_ips_page(request: Request):
    """Страница с floating IP из VK Cloud."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    cloud_cache = data.get("cloud_cache", {})
    cloud_last_update = data.get("cloud_last_update")
    
    # Собираем все IP из кэша
    all_ips = []
    stats = {"total": 0, "attached": 0, "free": 0}
    
    for key, project_data in cloud_cache.items():
        account = project_data.get("account", "-")
        project = project_data.get("project", "-")
        
        for ip in project_data.get("ips", []):
            stats["total"] += 1
            if ip.get("attached"):
                stats["attached"] += 1
            else:
                stats["free"] += 1
            
            all_ips.append({
                "ip": ip.get("ip"),
                "account": account,
                "project": project,
                "attached": ip.get("attached"),
                "fixed_ip": ip.get("fixed_ip"),
                "status": ip.get("status"),
                "id": ip.get("id"),
            })
    
    # Сортируем по аккаунту, потом по IP
    all_ips.sort(key=lambda x: (x["account"] or "", x["ip"] or ""))
    
    return templates.TemplateResponse("cloud.html", {
        "request": request,
        "user": user,
        "all_ips": all_ips,
        "stats": stats,
        "last_update": cloud_last_update,
    })


@app.post("/api/cloud/refresh")
async def api_cloud_refresh(request: Request):
    """Обновить данные по всем проектам VK Cloud."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)
    
    data = load_data()
    
    import concurrent.futures
    from datetime import datetime
    
    # Собираем все проекты
    tasks = []
    for server in data.get("servers", []):
        for script in server.get("scripts", []):
            tasks.append((server, script))
    
    def fetch_cloud(args):
        server, script = args
        result = get_floating_ips_from_project(server, script)
        return {
            "key": f"{server['id']}-{script['id']}",
            "data": result,
        }
    
    # Параллельно получаем данные
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch_cloud, tasks))
    
    # Сохраняем в кэш
    cloud_cache = {}
    for r in results:
        cloud_cache[r["key"]] = r["data"]
    
    data["cloud_cache"] = cloud_cache
    data["cloud_last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_data(data)
    
    # Считаем статистику
    total = 0
    attached = 0
    for project_data in cloud_cache.values():
        for ip in project_data.get("ips", []):
            total += 1
            if ip.get("attached"):
                attached += 1
    
    return {
        "ok": True, 
        "total_ips": total,
        "attached": attached,
        "free": total - attached,
        "last_update": data["cloud_last_update"],
    }


@app.get("/api/cloud/{server_id}/{script_id}")
async def api_cloud_ips(request: Request, server_id: int, script_id: int):
    """API для получения floating IP из конкретного проекта."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)
    
    data = load_data()
    server = next((s for s in data["servers"] if s["id"] == server_id), None)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    script = next((s for s in server.get("scripts", []) if s["id"] == script_id), None)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    
    result = get_floating_ips_from_project(server, script)
    return result


# ─── OpenStack API (прямое подключение) ──────────────────────

def openstack_auth(auth_url: str, username: str, password: str, project_id: str) -> tuple[str, dict]:
    """Получить токен OpenStack через Keystone."""
    auth_payload = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": username,
                        "domain": {"name": "users"},
                        "password": password
                    }
                }
            },
            "scope": {
                "project": {
                    "id": project_id
                }
            }
        }
    }
    
    # Keystone v3 auth
    token_url = auth_url.rstrip('/') + '/auth/tokens'
    
    with httpx.Client(timeout=30) as client:
        resp = client.post(token_url, json=auth_payload)
        resp.raise_for_status()
        
        token = resp.headers.get('X-Subject-Token')
        catalog = resp.json().get('token', {}).get('catalog', [])
        
        endpoints = {}
        for service in catalog:
            stype = service.get('type')
            for ep in service.get('endpoints', []):
                # Берём только RegionOne, игнорируем kz и другие регионы
                if ep.get('interface') == 'public' and ep.get('region') == 'RegionOne':
                    endpoints[stype] = ep.get('url')
        
        return token, endpoints


def openstack_get_floating_ips(token: str, network_endpoint: str) -> list:
    """Получить список floating IPs через Neutron API."""
    url = network_endpoint.rstrip('/') + '/v2.0/floatingips'
    
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers={'X-Auth-Token': token})
        resp.raise_for_status()
        
        return resp.json().get('floatingips', [])


def openstack_get_servers(token: str, compute_endpoint: str) -> dict:
    """Получить словарь серверов {port_id: server_name}."""
    # Получаем список серверов
    url = compute_endpoint.rstrip('/') + '/servers/detail'
    
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers={'X-Auth-Token': token})
        resp.raise_for_status()
        
        servers = resp.json().get('servers', [])
        
        # Создаём маппинг port_id -> server_name
        port_to_server = {}
        for srv in servers:
            name = srv.get('name')
            addresses = srv.get('addresses', {})
            for network_name, addrs in addresses.items():
                for addr in addrs:
                    # У floating IP есть "OS-EXT-IPS:type": "floating"
                    if addr.get('OS-EXT-IPS:type') == 'fixed':
                        # Для fixed IP берём порт
                        # Но нам нужен port_id, которого тут нет напрямую
                        pass
            # Сохраняем по ID сервера
            port_to_server[srv.get('id')] = name
        
        return servers


def get_project_floating_ips(project: dict) -> dict:
    """Получить все floating IPs для проекта через API."""
    result = {
        "name": project["name"],
        "username": project["username"],
        "ips": [],
        "error": None,
    }
    
    try:
        token, endpoints = openstack_auth(
            project["auth_url"],
            project["username"],
            project["password"],
            project["project_id"]
        )
        
        network_ep = endpoints.get('network')
        compute_ep = endpoints.get('compute')
        
        if not network_ep:
            result["error"] = "No network endpoint"
            return result
        
        # Получаем floating IPs
        fips = openstack_get_floating_ips(token, network_ep)
        
        # Получаем серверы для маппинга
        servers = []
        server_by_id = {}
        if compute_ep:
            try:
                url = compute_ep.rstrip('/') + '/servers/detail'
                with httpx.Client(timeout=30) as client:
                    resp = client.get(url, headers={'X-Auth-Token': token})
                    if resp.status_code == 200:
                        servers = resp.json().get('servers', [])
                        server_by_id = {s['id']: s['name'] for s in servers}
            except:
                pass
        
        # Собираем информацию по IP
        for fip in fips:
            ip_info = {
                "ip": fip.get('floating_ip_address'),
                "id": fip.get('id'),
                "status": fip.get('status'),
                "fixed_ip": fip.get('fixed_ip_address'),
                "port_id": fip.get('port_id'),
                "attached": bool(fip.get('port_id')),
                "server_name": None,
            }
            
            # Ищем сервер по fixed_ip
            fixed_ip = fip.get('fixed_ip_address')
            if fixed_ip:
                for srv in servers:
                    for network_addrs in srv.get('addresses', {}).values():
                        for addr in network_addrs:
                            if addr.get('addr') == fixed_ip:
                                ip_info['server_name'] = srv.get('name')
                                break
            
            result["ips"].append(ip_info)
        
    except httpx.HTTPStatusError as e:
        result["error"] = f"HTTP {e.response.status_code}"
    except Exception as e:
        result["error"] = str(e)[:100]
    
    return result


# ─── Страница проектов ────────────────────────────────────────

@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    """Страница со всеми проектами VK Cloud."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    data = load_data()
    projects = data.get("projects", [])
    projects_cache = data.get("projects_cache", {})
    projects_last_update = data.get("projects_last_update")
    
    # Группируем проекты по аккаунту
    accounts_dict = {}
    stats = {"total": 0, "attached": 0, "free": 0}
    
    for proj in projects:
        cached = projects_cache.get(proj["name"], {})
        proj_data = {
            "name": proj["name"],
            "ips": cached.get("ips", []),
            "error": cached.get("error"),
        }
        
        username = proj["username"]
        if username not in accounts_dict:
            accounts_dict[username] = {
                "username": username,
                "projects": [],
                "total_ips": 0,
            }
        
        accounts_dict[username]["projects"].append(proj_data)
        accounts_dict[username]["total_ips"] += len(proj_data["ips"])
        
        for ip in proj_data["ips"]:
            stats["total"] += 1
            if ip.get("attached"):
                stats["attached"] += 1
            else:
                stats["free"] += 1
    
    # Сортируем аккаунты по количеству IP (больше — выше)
    accounts = sorted(accounts_dict.values(), key=lambda x: -x["total_ips"])
    
    return templates.TemplateResponse("projects.html", {
        "request": request,
        "user": user,
        "accounts": accounts,
        "total_projects": len(projects),
        "stats": stats,
        "last_update": projects_last_update,
    })


@app.post("/api/projects/refresh")
async def api_projects_refresh(request: Request):
    """Обновить данные по всем проектам."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)
    
    data = load_data()
    projects = data.get("projects", [])
    
    if not projects:
        return {"ok": False, "error": "No projects configured"}
    
    import concurrent.futures
    from datetime import datetime
    
    def fetch_project(proj):
        return get_project_floating_ips(proj)
    
    # Параллельно получаем данные
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(fetch_project, projects))
    
    # Сохраняем в кэш
    projects_cache = {}
    stats = {"total": 0, "attached": 0, "free": 0}
    
    for r in results:
        projects_cache[r["name"]] = {
            "ips": r["ips"],
            "error": r["error"],
        }
        for ip in r["ips"]:
            stats["total"] += 1
            if ip.get("attached"):
                stats["attached"] += 1
            else:
                stats["free"] += 1
    
    data["projects_cache"] = projects_cache
    data["projects_last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_data(data)
    
    return {
        "ok": True,
        "total_ips": stats["total"],
        "attached": stats["attached"],
        "free": stats["free"],
        "last_update": data["projects_last_update"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

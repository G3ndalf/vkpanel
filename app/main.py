"""
VK IP Panel — главный модуль приложения.
FastAPI + Jinja2 + Paramiko для SSH.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

# Московское время (UTC+3)
MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    """Текущее время по Москве."""
    return datetime.now(MSK)
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import (
    ADMIN_USER, ADMIN_PASS, SECRET_KEY,
    BASE_DIR, MAX_SSH_WORKERS, MAX_CLOUD_WORKERS,
    BOT_API_KEY,
)
from .data import (
    load_data, save_data,
    get_server_by_id, get_script_by_id,
    update_status_cache, get_cached_status, get_cached_cloud,
)
from .ssh import get_script_status, control_script, get_floating_ips_via_cli, change_script_project, ssh_connect, ssh_exec
from .openstack import get_project_floating_ips
from .monitoring import (
    save_ssh_key, get_ssh_key_path, get_ssh_user,
    check_ssh_reachable, deploy_agent, collect_traffic, get_tenant_ips,
)

# ─── Логирование ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── FastAPI приложение ───────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown events."""
    logger.info("VK IP Panel starting...")
    yield
    logger.info("VK IP Panel stopping...")


app = FastAPI(title="VK IP Panel", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Статика и шаблоны
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ─── Авторизация ──────────────────────────────────────────────

def get_current_user(request: Request) -> Optional[str]:
    """Получить текущего пользователя из сессии."""
    return request.session.get("user")


def require_auth(request: Request) -> str:
    """Проверить авторизацию, вернуть user или raise."""
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
        logger.info(f"User logged in: {username}")
        return RedirectResponse("/", status_code=303)
    logger.warning(f"Failed login attempt: {username}")
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    user = request.session.get("user")
    request.session.clear()
    logger.info(f"User logged out: {user}")
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
    last_update = data.get("last_update") or data.get("cloud_last_update")

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

    # Список серверов для JS-фильтрации
    server_names = sorted(set(s["name"] for s in servers))

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "servers": servers,
        "server_names": server_names,
        "scripts_info": scripts_info,
        "total_scripts": total_scripts,
        "running_scripts": running_scripts,
        "last_update": last_update,
    })


@app.get("/servers", response_class=HTMLResponse)
async def servers_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    
    # Добавляем статистику IP к проектам
    projects = data.get("projects", [])
    projects_cache = data.get("projects_cache", {})
    
    projects_with_stats = []
    for proj in projects:
        cached = projects_cache.get(proj["name"], {})
        ips = cached.get("ips", [])
        total = len(ips)
        attached = sum(1 for ip in ips if ip.get("attached"))
        
        projects_with_stats.append({
            **proj,
            "total_ips": total,
            "attached_ips": attached,
            "free_ips": total - attached,
        })
    
    return templates.TemplateResponse("servers.html", {
        "request": request,
        "user": user,
        "servers": data.get("servers", []),
        "projects": projects_with_stats,
        "projects_cache": projects_cache,
        "status_cache": data.get("status_cache", {}),
        "last_update": data.get("last_update"),
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
    logger.info(f"Server added: {name} ({host})")

    return RedirectResponse("/servers", status_code=303)


@app.post("/servers/{server_id}/delete")
async def delete_server(request: Request, server_id: int):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    server = get_server_by_id(data, server_id)
    if server:
        data["servers"] = [s for s in data["servers"] if s.get("id") != server_id]
        save_data(data)
        logger.info(f"Server deleted: {server['name']}")

    return RedirectResponse("/servers", status_code=303)


@app.get("/servers/{server_id}/scripts", response_class=HTMLResponse)
async def server_scripts(request: Request, server_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    server = get_server_by_id(data, server_id)

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
    server = get_server_by_id(data, server_id)

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
    logger.info(f"Script added: {server['name']}/{name}")

    return RedirectResponse(f"/servers/{server_id}/scripts", status_code=303)


@app.post("/servers/{server_id}/scripts/{script_id}/delete")
async def delete_script(request: Request, server_id: int, script_id: int):
    if not get_current_user(request):
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    server = get_server_by_id(data, server_id)

    if server:
        script = get_script_by_id(server, script_id)
        if script:
            server["scripts"] = [s for s in server.get("scripts", []) if s.get("id") != script_id]
            save_data(data)
            logger.info(f"Script deleted: {server['name']}/{script['name']}")

    return RedirectResponse(f"/servers/{server_id}/scripts", status_code=303)


@app.post("/servers/{server_id}/scripts/{script_id}/{action}")
async def script_action(request: Request, server_id: int, script_id: int, action: str):
    """Управление скриптом: start/stop/restart. Возвращает JSON для AJAX или редирект."""
    if not get_current_user(request):
        # Проверяем, это AJAX запрос или обычный
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            raise HTTPException(status_code=401, detail="Unauthorized")
        return RedirectResponse("/login", status_code=303)

    if action not in ("start", "stop", "restart"):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JSONResponse({"ok": False, "error": "Invalid action"}, status_code=400)
        return RedirectResponse("/", status_code=303)

    data = load_data()
    server = get_server_by_id(data, server_id)

    if not server:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JSONResponse({"ok": False, "error": "Server not found"}, status_code=404)
        return RedirectResponse("/", status_code=303)

    script = get_script_by_id(server, script_id)
    if not script:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JSONResponse({"ok": False, "error": "Script not found"}, status_code=404)
        return RedirectResponse("/", status_code=303)

    # Выполняем действие
    success, msg = control_script(server, script, action)

    # Обновляем кэш статуса после действия
    if success:
        new_status = get_script_status(server, script)
        update_status_cache(data, server_id, script_id, {
            "server_id": server_id,
            "script_id": script_id,
            "server_name": server["name"],
            "script_name": script["name"],
            "running": new_status["running"],
            "cycles": new_status["cycles"],
            "success": new_status["success"],
            "last_ip": new_status["last_ip"],
            "error": new_status["error"],
            "account": new_status["account"],
            "project": new_status["project"],
        })
        save_data(data)

    # AJAX ответ
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({
            "ok": success,
            "message": msg,
            "action": action,
            "running": success and action in ("start", "restart"),
        })

    # Обычный редирект
    referer = request.headers.get("referer", "/")
    return RedirectResponse(referer, status_code=303)


@app.post("/api/scripts/{server_id}/{script_id}/change-project")
async def api_change_project(request: Request, server_id: int, script_id: int, project_name: str = Form(...)):
    """Сменить проект для скрипта."""
    if not get_current_user(request):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            raise HTTPException(status_code=401)
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    server = get_server_by_id(data, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Server not found"}, status_code=404)

    script = get_script_by_id(server, script_id)
    if not script:
        return JSONResponse({"ok": False, "error": "Script not found"}, status_code=404)

    # Ищем проект по имени
    projects = data.get("projects", [])
    project = next((p for p in projects if p["name"] == project_name), None)
    if not project:
        return JSONResponse({"ok": False, "error": f"Project '{project_name}' not found"}, status_code=404)

    # Подставляем os_project_name из кэша (реальное имя в VK Cloud)
    projects_cache = data.get("projects_cache", {})
    os_pname = projects_cache.get(project["name"], {}).get("os_project_name")
    if os_pname:
        project = {**project, "os_project_name": os_pname}

    # Меняем проект
    success, msg = change_script_project(server, script, project)

    if success:
        # Обновляем кэш
        new_status = get_script_status(server, script)
        update_status_cache(data, server_id, script_id, {
            "server_id": server_id,
            "script_id": script_id,
            "server_name": server["name"],
            "script_name": script["name"],
            "running": new_status["running"],
            "cycles": new_status["cycles"],
            "success": new_status["success"],
            "last_ip": new_status["last_ip"],
            "error": new_status["error"],
            "account": new_status["account"],
            "project": new_status["project"],
        })
        save_data(data)

    return JSONResponse({
        "ok": success,
        "message": msg,
        "project": project_name if success else None,
    })


# ─── API ──────────────────────────────────────────────────────

@app.post("/api/refresh")
async def api_refresh_status(request: Request):
    """Обновить статусы всех скриптов и сохранить в кэш."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()

    # Собираем задачи для параллельного выполнения
    tasks = []
    for server in data.get("servers", []):
        for script in server.get("scripts", []):
            tasks.append((server, script))

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

    # Выполняем параллельно
    with ThreadPoolExecutor(max_workers=MAX_SSH_WORKERS) as executor:
        results = list(executor.map(fetch_status, tasks))

    # Сохраняем в кэш
    status_cache = {}
    for r in results:
        cache_key = f"{r['server_id']}-{r['script_id']}"
        status_cache[cache_key] = r

    data["status_cache"] = status_cache
    data["last_update"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    save_data(data)

    running = sum(1 for r in results if r.get("running"))
    logger.info(f"Status refresh: {len(results)} scripts, {running} running")

    return {"ok": True, "updated": len(results), "running": running, "last_update": data["last_update"]}


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
    """Получить статус одного скрипта (live, не из кэша)."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    server = get_server_by_id(data, server_id)
    if not server:
        raise HTTPException(status_code=404)

    script = get_script_by_id(server, script_id)
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


# ─── Cloud страницы ───────────────────────────────────────────

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

    # Собираем все проекты
    tasks = []
    for server in data.get("servers", []):
        for script in server.get("scripts", []):
            tasks.append((server, script))

    def fetch_cloud(args):
        server, script = args
        result = get_floating_ips_via_cli(server, script)
        return {
            "key": f"{server['id']}-{script['id']}",
            "data": result,
        }

    # Параллельно получаем данные
    with ThreadPoolExecutor(max_workers=MAX_CLOUD_WORKERS) as executor:
        results = list(executor.map(fetch_cloud, tasks))

    # Сохраняем в кэш
    cloud_cache = {}
    for r in results:
        cloud_cache[r["key"]] = r["data"]

    data["cloud_cache"] = cloud_cache
    data["cloud_last_update"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    save_data(data)

    # Считаем статистику
    total = 0
    attached = 0
    for project_data in cloud_cache.values():
        for ip in project_data.get("ips", []):
            total += 1
            if ip.get("attached"):
                attached += 1

    logger.info(f"Cloud refresh: {total} IPs, {attached} attached")

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
    server = get_server_by_id(data, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    script = get_script_by_id(server, script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    result = get_floating_ips_via_cli(server, script)
    return result


# ─── Projects страница ────────────────────────────────────────

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

    # Строим маппинг: project_name -> список скриптов из status_cache
    status_cache = data.get("status_cache", {})
    servers = data.get("servers", [])
    project_scripts_map: dict[str, list[dict]] = {}
    for cache_key, cached_status in status_cache.items():
        proj_name = cached_status.get("project")
        if proj_name:
            if proj_name not in project_scripts_map:
                project_scripts_map[proj_name] = []
            # Парсим server_id и script_id из cache_key "1-2"
            parts = cache_key.split("-")
            server_id = int(parts[0]) if len(parts) == 2 else 0
            script_id = int(parts[1]) if len(parts) == 2 else 0
            project_scripts_map[proj_name].append({
                "server_id": server_id,
                "script_id": script_id,
                "server_name": cached_status.get("server_name", "?"),
                "script_name": cached_status.get("script_name", "?"),
                "running": cached_status.get("running", False),
                "cycles": cached_status.get("cycles", 0),
                "success": cached_status.get("success", 0),
                "last_ip": cached_status.get("last_ip"),
                "error": cached_status.get("error"),
            })

    # Собираем полную карту серверов → скриптов с текущими статусами (для модалки выбора)
    servers_scripts_info = []
    for server in servers:
        srv_info = {
            "id": server["id"],
            "name": server["name"],
            "scripts": [],
        }
        for script in server.get("scripts", []):
            ck = f"{server['id']}-{script['id']}"
            cached = status_cache.get(ck, {})
            srv_info["scripts"].append({
                "id": script["id"],
                "name": script["name"],
                "running": cached.get("running", False),
                "project": cached.get("project", "—"),
                "account": cached.get("account", "—"),
                "cycles": cached.get("cycles", 0),
                "success": cached.get("success", 0),
            })
        servers_scripts_info.append(srv_info)

    # Группируем проекты по аккаунту
    accounts_dict = {}
    stats = {"total": 0, "attached": 0, "free": 0}
    scripts_active = 0
    scripts_total = 0

    for proj in projects:
        cached = projects_cache.get(proj["name"], {})
        # Маппинг через os_project_name (реальное имя из VK Cloud)
        os_pname = cached.get("os_project_name") or ""
        scripts_for_proj = project_scripts_map.get(os_pname, [])
        # Фоллбэк: если os_project_name ещё не загружен, пробуем по name
        if not scripts_for_proj:
            scripts_for_proj = project_scripts_map.get(proj["name"], [])
        scripts_total += len(scripts_for_proj)
        scripts_active += sum(1 for s in scripts_for_proj if s["running"])

        proj_data = {
            "name": proj["name"],
            "os_project_name": cached.get("os_project_name"),
            "ips": cached.get("ips", []),
            "error": cached.get("error"),
            "scripts": scripts_for_proj,
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

    # Маппинг IP -> арендатор
    ip_tenant_map = {}
    for t in data.get("tenants", []):
        for ip_addr in t.get("ips", []):
            ip_tenant_map[ip_addr] = t["name"]

    sales = data.get("sales", {})
    rentals = data.get("rentals", {})
    pricing = data.get("pricing", {"sale_per_ip": 30000, "rent_per_ip": 500})

    return templates.TemplateResponse("projects.html", {
        "request": request,
        "user": user,
        "accounts": accounts,
        "total_projects": len(projects),
        "stats": stats,
        "scripts_active": scripts_active,
        "scripts_total": scripts_total,
        "servers_scripts": servers_scripts_info,
        "ip_tenant_map": ip_tenant_map,
        "tenants": [t["name"] for t in data.get("tenants", [])],
        "sales": sales,
        "rentals": rentals,
        "pricing": pricing,
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

    def fetch_project(proj):
        return get_project_floating_ips(proj)

    # Параллельно получаем данные
    with ThreadPoolExecutor(max_workers=MAX_CLOUD_WORKERS) as executor:
        results = list(executor.map(fetch_project, projects))

    # Сохраняем в кэш
    projects_cache = {}
    stats = {"total": 0, "attached": 0, "free": 0}

    for r in results:
        projects_cache[r["name"]] = {
            "ips": r["ips"],
            "error": r["error"],
            "os_project_name": r.get("os_project_name"),
        }
        for ip in r["ips"]:
            stats["total"] += 1
            if ip.get("attached"):
                stats["attached"] += 1
            else:
                stats["free"] += 1

    data["projects_cache"] = projects_cache
    data["projects_last_update"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    save_data(data)

    logger.info(f"Projects refresh: {stats['total']} IPs")

    return {
        "ok": True,
        "total_ips": stats["total"],
        "attached": stats["attached"],
        "free": stats["free"],
        "last_update": data["projects_last_update"],
    }


@app.post("/api/projects/add")
async def api_add_project(
    request: Request,
    script: str = Form(...),
    password: str = Form(""),
):
    """Добавить новый проект из openrc скрипта."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    # Парсим скрипт
    import re
    
    auth_url_match = re.search(r'OS_AUTH_URL[="]+(https?://[^"\']+)', script)
    project_id_match = re.search(r'OS_PROJECT_ID[="]+([\w-]+)', script)
    username_match = re.search(r'OS_USERNAME[="]+([\w@._-]+)', script)
    project_name_match = re.search(r'OS_PROJECT_NAME[="]+([\w-]+)', script)
    
    if not auth_url_match:
        return JSONResponse({"ok": False, "error": "OS_AUTH_URL не найден"}, status_code=400)
    if not project_id_match:
        return JSONResponse({"ok": False, "error": "OS_PROJECT_ID не найден"}, status_code=400)
    if not username_match:
        return JSONResponse({"ok": False, "error": "OS_USERNAME не найден"}, status_code=400)
    
    auth_url = auth_url_match.group(1).rstrip('"').rstrip("'")
    project_id = project_id_match.group(1)
    username = username_match.group(1)
    
    # Имя проекта: из OS_PROJECT_NAME или генерируем из project_id
    if project_name_match:
        name = project_name_match.group(1)
    else:
        # Берём первые 8 символов project_id как имя
        name = f"mcs{project_id[:8]}"
    
    # Пароль по умолчанию
    if not password.strip():
        password = "Haxoastemir29"
    
    # Создаём проект
    new_project = {
        "name": name,
        "username": username,
        "password": password,
        "auth_url": auth_url,
        "project_id": project_id,
    }
    
    data = load_data()
    
    # Проверяем дубликат по project_id
    existing = next((p for p in data.get("projects", []) if p["project_id"] == project_id), None)
    if existing:
        return JSONResponse({"ok": False, "error": f"Проект с таким ID уже существует ({existing['name']})"}, status_code=400)
    
    if "projects" not in data:
        data["projects"] = []
    
    data["projects"].append(new_project)
    save_data(data)
    
    logger.info(f"Project added: {name} ({username})")
    
    return JSONResponse({
        "ok": True,
        "message": f"Проект '{name}' добавлен",
        "project": {
            "name": name,
            "username": username,
        }
    })


@app.post("/api/accounts/{username}/delete")
async def api_delete_account(request: Request, username: str):
    """Удалить все проекты аккаунта."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    projects = data.get("projects", [])

    # Находим проекты этого аккаунта
    account_projects = [p for p in projects if p["username"] == username]
    if not account_projects:
        return JSONResponse({"ok": False, "error": "Аккаунт не найден"}, status_code=404)

    # Удаляем проекты и их кэш
    project_names = [p["name"] for p in account_projects]
    data["projects"] = [p for p in projects if p["username"] != username]
    for name in project_names:
        data.get("projects_cache", {}).pop(name, None)

    save_data(data)
    logger.info(f"Account deleted: {username} ({len(account_projects)} projects)")

    return JSONResponse({"ok": True, "message": f"Удалено {len(account_projects)} проектов аккаунта '{username}'"})


@app.post("/api/projects/{project_name}/delete")
async def api_delete_project(request: Request, project_name: str):
    """Удалить проект."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    projects = data.get("projects", [])
    
    project = next((p for p in projects if p["name"] == project_name), None)
    if not project:
        return JSONResponse({"ok": False, "error": "Проект не найден"}, status_code=404)
    
    data["projects"] = [p for p in projects if p["name"] != project_name]
    
    # Удаляем из кэша
    if project_name in data.get("projects_cache", {}):
        del data["projects_cache"][project_name]
    
    save_data(data)
    logger.info(f"Project deleted: {project_name}")
    
    return JSONResponse({"ok": True, "message": f"Проект '{project_name}' удалён"})


# ─── IPs страница ─────────────────────────────────────────────

@app.get("/ips", response_class=HTMLResponse)
async def all_ips(request: Request):
    """Страница всех пойманных IP — данные из кэша, без live SSH."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    all_ips_list = []
    status_cache = data.get("status_cache", {})

    # Берём allocated IP из кэша статусов (state файлы)
    for server in data.get("servers", []):
        for script in server.get("scripts", []):
            cache_key = f"{server['id']}-{script['id']}"
            cached = status_cache.get(cache_key, {})
            state = cached.get("state", {})
            if state:
                allocated = state.get("allocated", {})
                for subnet, ips in allocated.items():
                    for ip_info in ips:
                        all_ips_list.append({
                            "ip": ip_info.get("floating_ip"),
                            "fip_id": ip_info.get("fip_id"),
                            "created_at": ip_info.get("created_at"),
                            "subnet": subnet,
                            "server": server["name"],
                            "script": script["name"],
                            "account": cached.get("account") or script.get("account_name", "-"),
                            "project": cached.get("project") or script.get("project_name", "-"),
                        })

    # Сортируем по дате (новые первые)
    all_ips_list.sort(key=lambda x: x.get("created_at") or "", reverse=True)

    return templates.TemplateResponse("ips.html", {
        "request": request,
        "user": user,
        "ips": all_ips_list,
        "last_update": data.get("last_update"),
    })


# ─── Арендаторы ───────────────────────────────────────────────

@app.get("/tenants", response_class=HTMLResponse)
async def tenants_page(request: Request):
    """Страница арендаторов."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    tenants = data.get("tenants", [])
    projects_cache = data.get("projects_cache", {})
    projects = {p["name"]: p for p in data.get("projects", [])}

    # Строим маппинг ip -> {project, account, attached, server_name}
    ip_info_map = {}
    for proj_name, cached in projects_cache.items():
        proj = projects.get(proj_name, {})
        for ip in cached.get("ips", []):
            ip_addr = ip.get("ip")
            if ip_addr:
                ip_info_map[ip_addr] = {
                    "project": cached.get("os_project_name") or proj_name,
                    "project_key": proj_name,
                    "account": proj.get("username", "—"),
                    "attached": ip.get("attached", False),
                    "server_name": ip.get("server_name"),
                }

    # Обогащаем данные арендаторов — группировка по аккаунтам
    tenants_enriched = []
    for t in tenants:
        ips_enriched = []
        for ip_addr in t.get("ips", []):
            info = ip_info_map.get(ip_addr, {})
            ips_enriched.append({
                "ip": ip_addr,
                "project": info.get("project", "—"),
                "account": info.get("account", "—"),
                "attached": info.get("attached", False),
                "server_name": info.get("server_name"),
            })

        # Группируем IP по аккаунтам, сортируем аккаунты по алфавиту
        accounts = {}
        for ip_data in ips_enriched:
            acc = ip_data["account"]
            accounts.setdefault(acc, []).append(ip_data)
        accounts_sorted = [
            {"account": acc, "ips": ips}
            for acc, ips in sorted(accounts.items(), key=lambda x: x[0].lower())
        ]

        tenants_enriched.append({
            "name": t["name"],
            "ips": ips_enriched,
            "accounts": accounts_sorted,
        })

    # Собираем все IP для выбора (свободные от арендаторов)
    rented_ips = set()
    for t in tenants:
        rented_ips.update(t.get("ips", []))

    all_available_ips = []
    for ip_addr, info in sorted(ip_info_map.items()):
        all_available_ips.append({
            "ip": ip_addr,
            "project": info.get("project", "—"),
            "account": info.get("account", "—"),
            "rented_by": None,
        })
    # Отмечаем кто арендует
    for t in tenants:
        for ip_addr in t.get("ips", []):
            for aip in all_available_ips:
                if aip["ip"] == ip_addr:
                    aip["rented_by"] = t["name"]

    return templates.TemplateResponse("tenants.html", {
        "request": request,
        "user": user,
        "tenants": tenants_enriched,
        "all_ips": all_available_ips,
        "total_ips": len(ip_info_map),
        "rented_count": len(rented_ips),
    })


@app.post("/api/tenants/add")
async def api_add_tenant(request: Request, name: str = Form(...)):
    """Создать арендатора."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    tenants = data.setdefault("tenants", [])

    if any(t["name"] == name for t in tenants):
        return JSONResponse({"ok": False, "error": f"Арендатор '{name}' уже существует"}, status_code=400)

    tenants.append({"name": name, "ips": []})
    save_data(data)
    logger.info(f"Tenant added: {name}")
    return JSONResponse({"ok": True, "message": f"Арендатор '{name}' создан"})


@app.post("/api/tenants/{tenant_name}/delete")
async def api_delete_tenant(request: Request, tenant_name: str):
    """Удалить арендатора."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    tenants = data.get("tenants", [])
    if not any(t["name"] == tenant_name for t in tenants):
        return JSONResponse({"ok": False, "error": "Арендатор не найден"}, status_code=404)

    data["tenants"] = [t for t in tenants if t["name"] != tenant_name]
    save_data(data)
    logger.info(f"Tenant deleted: {tenant_name}")
    return JSONResponse({"ok": True, "message": f"Арендатор '{tenant_name}' удалён"})


@app.post("/api/tenants/{tenant_name}/assign-ip")
async def api_assign_ip(request: Request, tenant_name: str, ip: str = Form(...)):
    """Привязать IP к арендатору."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    tenants = data.get("tenants", [])
    tenant = next((t for t in tenants if t["name"] == tenant_name), None)
    if not tenant:
        return JSONResponse({"ok": False, "error": "Арендатор не найден"}, status_code=404)

    # Проверяем что IP не занят другим арендатором
    for t in tenants:
        if ip in t.get("ips", []) and t["name"] != tenant_name:
            return JSONResponse({"ok": False, "error": f"IP {ip} уже у арендатора '{t['name']}'"}, status_code=400)

    if ip not in tenant.get("ips", []):
        tenant.setdefault("ips", []).append(ip)
        save_data(data)

    logger.info(f"IP {ip} assigned to tenant {tenant_name}")
    return JSONResponse({"ok": True, "message": f"IP {ip} привязан к '{tenant_name}'"})


@app.post("/api/tenants/{tenant_name}/unassign-ip")
async def api_unassign_ip(request: Request, tenant_name: str, ip: str = Form(...)):
    """Отвязать IP от арендатора."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    tenant = next((t for t in data.get("tenants", []) if t["name"] == tenant_name), None)
    if not tenant:
        return JSONResponse({"ok": False, "error": "Арендатор не найден"}, status_code=404)

    if ip in tenant.get("ips", []):
        tenant["ips"].remove(ip)
        save_data(data)

    logger.info(f"IP {ip} unassigned from tenant {tenant_name}")
    return JSONResponse({"ok": True, "message": f"IP {ip} отвязан от '{tenant_name}'"})


# ─── Логи ─────────────────────────────────────────────────────

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Страница логов скриптов."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    servers = data.get("servers", [])
    status_cache = data.get("status_cache", {})

    servers_info = []
    for server in servers:
        srv = {"id": server["id"], "name": server["name"], "scripts": []}
        for script in server.get("scripts", []):
            ck = f"{server['id']}-{script['id']}"
            cached = status_cache.get(ck, {})
            srv["scripts"].append({
                "id": script["id"],
                "name": script["name"],
                "service_name": script.get("service_name", ""),
                "running": cached.get("running", False),
                "project": cached.get("project", "—"),
            })
        servers_info.append(srv)

    logs_cache = data.get("logs_cache", {})

    return templates.TemplateResponse("logs.html", {
        "request": request,
        "user": user,
        "servers": servers_info,
        "logs_cache": logs_cache,
    })


@app.get("/api/logs/{server_id}/{script_id}")
async def api_get_logs(request: Request, server_id: int, script_id: int, lines: int = 10):
    """Получить логи скрипта через SSH + journalctl."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    if lines > 200:
        lines = 200

    data = load_data()
    server = get_server_by_id(data, server_id)
    if not server:
        return JSONResponse({"ok": False, "error": "Сервер не найден"}, status_code=404)

    script = get_script_by_id(server, script_id)
    if not script:
        return JSONResponse({"ok": False, "error": "Скрипт не найден"}, status_code=404)

    service_name = script.get("service_name", f"vk-fip@{script['name']}")

    try:
        client = ssh_connect(
            server["host"], server.get("port", 22),
            server["user"], server.get("password"), server.get("key_path"),
        )
        code, out, err = ssh_exec(client, f"journalctl -u {service_name} -n {lines} --no-pager 2>&1")
        client.close()

        log_text = out.strip() if code == 0 else (err.strip() or out.strip())

        # Сохраняем в кэш
        data.setdefault("logs_cache", {})[f"{server_id}-{script_id}"] = {
            "log": log_text,
            "time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        save_data(data)

        return JSONResponse({
            "ok": True,
            "server": server["name"],
            "script": script["name"],
            "lines": lines,
            "log": log_text,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@app.post("/api/logs/all")
async def api_get_all_logs(request: Request, lines: int = Form(10)):
    """Получить логи со всех скриптов параллельно."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    if lines > 200:
        lines = 200

    data = load_data()
    tasks = []
    for server in data.get("servers", []):
        for script in server.get("scripts", []):
            tasks.append((server, script))

    def fetch_log(args):
        server, script = args
        service_name = script.get("service_name", f"vk-fip@{script['name']}")
        try:
            client = ssh_connect(
                server["host"], server.get("port", 22),
                server["user"], server.get("password"), server.get("key_path"),
            )
            code, out, err = ssh_exec(client, f"journalctl -u {service_name} -n {lines} --no-pager 2>&1")
            client.close()
            return {
                "server_id": server["id"],
                "script_id": script["id"],
                "server": server["name"],
                "script": script["name"],
                "log": out.strip() if code == 0 else (err.strip() or out.strip()),
                "error": None,
            }
        except Exception as e:
            return {
                "server_id": server["id"],
                "script_id": script["id"],
                "server": server["name"],
                "script": script["name"],
                "log": "",
                "error": str(e)[:200],
            }

    with ThreadPoolExecutor(max_workers=MAX_SSH_WORKERS) as executor:
        results = list(executor.map(fetch_log, tasks))

    # Сохраняем в кэш
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    logs_cache = data.setdefault("logs_cache", {})
    for r in results:
        if r["log"] or not r.get("error"):
            logs_cache[f"{r['server_id']}-{r['script_id']}"] = {
                "log": r["log"],
                "time": now,
            }
    save_data(data)

    return JSONResponse({"ok": True, "results": results, "lines": lines})


# ─── Продажи (Sales) ──────────────────────────────────────────

@app.post("/api/pricing")
async def api_pricing(request: Request, sale_per_ip: int = Form(30000), rent_per_ip: int = Form(500)):
    """Обновить глобальные цены за IP."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    data["pricing"] = {"sale_per_ip": max(0, sale_per_ip), "rent_per_ip": max(0, rent_per_ip)}
    save_data(data)
    logger.info(f"Pricing updated: sale={sale_per_ip}, rent={rent_per_ip}")
    return JSONResponse({"ok": True, "sale_per_ip": sale_per_ip, "rent_per_ip": rent_per_ip})


@app.get("/api/pricing")
async def api_get_pricing(request: Request):
    """Получить текущие цены."""
    data = load_data()
    pricing = data.get("pricing", {"sale_per_ip": 30000, "rent_per_ip": 500})
    return pricing


def mask_email(email: str) -> str:
    """Маскировка email: первые 3 символа + ***@domain."""
    if "@" not in email:
        return email[:3] + "***"
    local, domain = email.rsplit("@", 1)
    return local[:3] + "***@" + domain


def require_bot_api_key(request: Request):
    """Проверить X-API-Key для бот-эндпоинтов."""
    key = request.headers.get("X-API-Key", "")
    if key != BOT_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


@app.post("/api/sales/{username}/toggle")
async def api_sales_toggle(
    request: Request,
    username: str,
    for_sale: bool = Form(False),
):
    """Переключить статус продажи аккаунта (админ). Цена считается из pricing."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()

    account_exists = any(p["username"] == username for p in data.get("projects", []))
    if not account_exists:
        return JSONResponse({"ok": False, "error": "Аккаунт не найден"}, status_code=404)

    sales = data.setdefault("sales", {})

    if for_sale:
        sales[username] = {"updated": datetime.utcnow().isoformat()}
        logger.info(f"Account {username} marked for sale")
    else:
        sales.pop(username, None)
        logger.info(f"Account {username} removed from sale")

    save_data(data)
    return JSONResponse({"ok": True, "for_sale": for_sale})


@app.get("/api/bot/accounts")
async def api_bot_accounts(request: Request):
    """API для бота: список аккаунтов на продажу с незанятыми IP."""
    require_bot_api_key(request)

    data = load_data()
    sales = data.get("sales", {})
    projects = data.get("projects", [])
    projects_cache = data.get("projects_cache", {})
    pricing = data.get("pricing", {"sale_per_ip": 30000, "rent_per_ip": 500})
    pricing_sale = pricing.get("sale_per_ip", 30000)

    if not sales:
        return {"accounts": [], "price_per_ip": pricing_sale}

    # Группируем проекты по username
    accounts_projects: dict[str, list] = {}
    for proj in projects:
        accounts_projects.setdefault(proj["username"], []).append(proj)

    result = []
    for username, sale_info in sales.items():
        user_projects = accounts_projects.get(username, [])
        if not user_projects:
            continue

        # Собираем IP по проектам, проверяем что ВСЕ свободны
        all_ips = []
        all_free = True
        projects_list = []

        for proj in user_projects:
            cached = projects_cache.get(proj["name"], {})
            ips = cached.get("ips", [])
            proj_ips = []
            for ip in ips:
                proj_ips.append(ip["ip"])
                all_ips.append(ip["ip"])
                if ip.get("attached"):
                    all_free = False
            if proj_ips:
                projects_list.append({"ips": proj_ips})

        # Только аккаунты где ВСЕ IP свободны (не привязаны к ВМ)
        if not all_free or not all_ips:
            continue

        result.append({
            "username": username,
            "masked_email": mask_email(username),
            "ips": all_ips,
            "ip_count": len(all_ips),
            "project_count": len(user_projects),
            "projects": projects_list,
            "price": pricing_sale * len(all_ips),
        })

    result.sort(key=lambda x: -x["ip_count"])

    return {"accounts": result, "price_per_ip": pricing_sale}


def mask_project_name(name: str) -> str:
    """Маскировка имени проекта: первые 4 символа + ***."""
    if len(name) <= 4:
        return name + "***"
    return name[:4] + "***"


@app.post("/api/rentals/{project_name}/toggle")
async def api_rentals_toggle(
    request: Request,
    project_name: str,
    for_rent: bool = Form(False),
):
    """Переключить статус аренды проекта (админ). Цена из pricing."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()

    project_exists = any(p["name"] == project_name for p in data.get("projects", []))
    if not project_exists:
        return JSONResponse({"ok": False, "error": "Проект не найден"}, status_code=404)

    rentals = data.setdefault("rentals", {})

    if for_rent:
        rentals[project_name] = {"updated": datetime.utcnow().isoformat()}
        logger.info(f"Project {project_name} marked for rent")
    else:
        rentals.pop(project_name, None)
        logger.info(f"Project {project_name} removed from rent")

    save_data(data)
    return JSONResponse({"ok": True, "for_rent": for_rent})


@app.get("/api/bot/rentals")
async def api_bot_rentals(request: Request):
    """API для бота: список проектов на аренду с незанятыми IP."""
    require_bot_api_key(request)

    data = load_data()
    rentals = data.get("rentals", {})
    projects = data.get("projects", [])
    projects_cache = data.get("projects_cache", {})
    pricing = data.get("pricing", {"sale_per_ip": 30000, "rent_per_ip": 500})
    pricing_rent = pricing.get("rent_per_ip", 500)

    if not rentals:
        return {"projects": [], "price_per_ip": pricing_rent}

    result = []
    for proj in projects:
        if proj["name"] not in rentals:
            continue

        cached = projects_cache.get(proj["name"], {})
        ips = cached.get("ips", [])

        # Только свободные IP (не привязаны к ВМ)
        free_ips = [ip["ip"] for ip in ips if not ip.get("attached")]
        if not free_ips:
            continue

        result.append({
            "project_name": proj["name"],
            "masked_project": mask_project_name(proj["name"]),
            "username": proj["username"],
            "ips": free_ips,
            "ip_count": len(free_ips),
            "price": pricing_rent * len(free_ips),
        })

    result.sort(key=lambda x: -x["ip_count"])
    return {"projects": result, "price_per_ip": pricing_rent}


# ─── Мониторинг трафика ────────────────────────────────────────

@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    """Страница мониторинга трафика арендаторов."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    data = load_data()
    tenants = data.get("tenants", [])
    monitoring = data.get("monitoring", {})
    ip_status = monitoring.get("ip_status", {})
    ssh_keys = monitoring.get("ssh_keys", {})
    ip_ssh_keys = monitoring.get("ip_ssh_keys", {})
    ip_ssh_users = monitoring.get("ip_ssh_users", {})
    traffic_data = monitoring.get("traffic_data", {})

    # Обогащаем данные арендаторов информацией мониторинга
    tenants_enriched = []
    total_ips = 0
    deployed_count = 0
    reachable_count = 0

    for t in tenants:
        ips_info = []
        has_tenant_key = t["name"] in ssh_keys
        for ip_addr in t.get("ips", []):
            total_ips += 1
            status = ip_status.get(ip_addr, {})
            has_ip_key = ip_addr in ip_ssh_keys
            ssh_user = ip_ssh_users.get(ip_addr, "root")
            last_traffic = traffic_data.get(ip_addr)

            if status.get("agent_deployed"):
                deployed_count += 1
            if status.get("is_reachable"):
                reachable_count += 1

            ips_info.append({
                "ip": ip_addr,
                "ssh_user": ssh_user,
                "has_key": has_ip_key or has_tenant_key,
                "has_own_key": has_ip_key,
                "agent_deployed": status.get("agent_deployed", False),
                "is_reachable": status.get("is_reachable"),
                "last_check": status.get("last_check"),
                "last_traffic": last_traffic,
            })

        # Суммарный трафик арендатора (исходящий для расчёта оплаты)
        tenant_tx_gb = sum(
            (traffic_data.get(ip_addr, {}) or {}).get("total_tx_gb", 0)
            for ip_addr in t.get("ips", [])
        )
        tenant_rx_gb = sum(
            (traffic_data.get(ip_addr, {}) or {}).get("total_rx_gb", 0)
            for ip_addr in t.get("ips", [])
        )
        tenant_total_gb = tenant_tx_gb + tenant_rx_gb
        # Оплата: 0.5 руб за 1 ГБ исходящего трафика
        tenant_cost = round(tenant_tx_gb * 0.5, 2)

        # Последнее время проверки доступности и сбора трафика (среди IP арендатора)
        check_times = [
            ip_status.get(ip_addr, {}).get("last_check")
            for ip_addr in t.get("ips", [])
            if ip_status.get(ip_addr, {}).get("last_check")
        ]
        traffic_times = [
            traffic_data.get(ip_addr, {}).get("collected_at") if traffic_data.get(ip_addr) else None
            for ip_addr in t.get("ips", [])
        ]
        traffic_times = [x for x in traffic_times if x]
        traffic_days_list = [
            traffic_data.get(ip_addr, {}).get("days") if traffic_data.get(ip_addr) else None
            for ip_addr in t.get("ips", [])
        ]
        traffic_days_list = [x for x in traffic_days_list if x]

        tenants_enriched.append({
            "name": t["name"],
            "ips": ips_info,
            "has_key": has_tenant_key,
            "total_tx_tb": round(tenant_tx_gb / 1000, 3),
            "total_rx_tb": round(tenant_rx_gb / 1000, 3),
            "total_tb": round(tenant_total_gb / 1000, 3),
            "cost": tenant_cost,
            "last_check": max(check_times) if check_times else None,
            "last_traffic_at": max(traffic_times) if traffic_times else None,
            "last_traffic_period": next(
                (traffic_data.get(ip_addr, {}).get("period") for ip_addr in t.get("ips", [])
                 if traffic_data.get(ip_addr, {}).get("period")),
                None
            ),
        })

    return templates.TemplateResponse("monitoring.html", {
        "request": request,
        "user": user,
        "tenants": tenants_enriched,
        "total_ips": total_ips,
        "deployed_count": deployed_count,
        "reachable_count": reachable_count,
    })


@app.post("/api/monitoring/ssh-key/tenant/{tenant_name}")
async def api_monitoring_ssh_key_tenant(
    request: Request, tenant_name: str, file: UploadFile = File(...)
):
    """Загрузить SSH ключ для арендатора."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    content = await file.read()
    if len(content) > 50_000:
        return JSONResponse({"ok": False, "error": "Файл слишком большой"}, status_code=400)

    path = save_ssh_key(f"tenant_{tenant_name}", content)

    data = load_data()
    monitoring = data.setdefault("monitoring", {})
    ssh_keys = monitoring.setdefault("ssh_keys", {})
    ssh_keys[tenant_name] = {
        "key_path": path,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    save_data(data)

    return JSONResponse({"ok": True, "message": f"SSH ключ загружен для {tenant_name}"})


@app.post("/api/monitoring/ssh-key/ip/{ip:path}")
async def api_monitoring_ssh_key_ip(
    request: Request, ip: str, file: UploadFile = File(...)
):
    """Загрузить SSH ключ для конкретного IP."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    content = await file.read()
    if len(content) > 50_000:
        return JSONResponse({"ok": False, "error": "Файл слишком большой"}, status_code=400)

    path = save_ssh_key(ip.replace(".", "_"), content)

    data = load_data()
    monitoring = data.setdefault("monitoring", {})
    ip_keys = monitoring.setdefault("ip_ssh_keys", {})
    ip_keys[ip] = {
        "key_path": path,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    save_data(data)

    return JSONResponse({"ok": True, "message": f"SSH ключ загружен для {ip}"})


@app.post("/api/monitoring/ssh-user/ip/{ip:path}")
async def api_monitoring_ssh_user(request: Request, ip: str, ssh_user: str = Form("ubuntu")):
    """Установить SSH пользователя для IP."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    monitoring = data.setdefault("monitoring", {})
    users = monitoring.setdefault("ip_ssh_users", {})
    users[ip] = ssh_user
    save_data(data)

    return JSONResponse({"ok": True, "message": f"SSH user для {ip}: {ssh_user}"})


@app.post("/api/monitoring/deploy/ip/{ip:path}")
async def api_monitoring_deploy_ip(request: Request, ip: str):
    """Развернуть агент мониторинга на одном IP."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()

    # Определяем арендатора для этого IP
    tenant_name = None
    for t in data.get("tenants", []):
        if ip in t.get("ips", []):
            tenant_name = t["name"]
            break

    key_path = get_ssh_key_path(data, ip, tenant_name)
    if not key_path:
        return JSONResponse({"ok": False, "error": f"SSH ключ не найден для {ip}"}, status_code=400)

    ssh_user = get_ssh_user(data, ip)

    # Определяем URL панели для агента
    panel_url = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    scheme = request.headers.get("X-Forwarded-Proto", "http")
    if panel_url:
        panel_url = f"{scheme}://{panel_url}"
    else:
        panel_url = str(request.base_url).rstrip("/")

    result = deploy_agent(ip, key_path, ssh_user, panel_url)

    if result["ok"]:
        # Сохраняем статус и API ключ
        monitoring = data.setdefault("monitoring", {})
        ip_status = monitoring.setdefault("ip_status", {})
        ip_status.setdefault(ip, {})["agent_deployed"] = True
        ip_status[ip]["api_key"] = result["api_key"]
        ip_status[ip]["deployed_at"] = datetime.utcnow().isoformat()

        # Сохраняем маппинг api_key -> ip для приёма отчётов
        api_keys = monitoring.setdefault("api_keys", {})
        api_keys[result["api_key"]] = ip

        save_data(data)

    return JSONResponse({"ok": result["ok"], "message": result["message"]})


@app.post("/api/monitoring/deploy/tenant/{tenant_name}")
async def api_monitoring_deploy_tenant(request: Request, tenant_name: str):
    """Развернуть агент на все IP арендатора."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    ips = get_tenant_ips(data, tenant_name)
    if not ips:
        return JSONResponse({"ok": False, "error": "У арендатора нет IP"}, status_code=400)

    # Определяем URL панели
    panel_url = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    scheme = request.headers.get("X-Forwarded-Proto", "http")
    if panel_url:
        panel_url = f"{scheme}://{panel_url}"
    else:
        panel_url = str(request.base_url).rstrip("/")

    def deploy_one(ip: str) -> dict:
        key_path = get_ssh_key_path(data, ip, tenant_name)
        if not key_path:
            return {"ip": ip, "ok": False, "message": "SSH ключ не найден"}
        ssh_user = get_ssh_user(data, ip)
        result = deploy_agent(ip, key_path, ssh_user, panel_url)
        return {"ip": ip, **result}

    with ThreadPoolExecutor(max_workers=MAX_SSH_WORKERS) as executor:
        results = list(executor.map(deploy_one, ips))

    # Сохраняем статусы
    monitoring = data.setdefault("monitoring", {})
    ip_status = monitoring.setdefault("ip_status", {})
    api_keys = monitoring.setdefault("api_keys", {})

    for r in results:
        if r["ok"]:
            ip_status.setdefault(r["ip"], {})["agent_deployed"] = True
            ip_status[r["ip"]]["api_key"] = r.get("api_key", "")
            ip_status[r["ip"]]["deployed_at"] = datetime.utcnow().isoformat()
            if r.get("api_key"):
                api_keys[r["api_key"]] = r["ip"]
    save_data(data)

    ok_count = sum(1 for r in results if r["ok"])
    return JSONResponse({
        "ok": ok_count > 0,
        "message": f"Развёрнуто: {ok_count}/{len(results)}",
        "results": results,
    })


@app.post("/api/monitoring/check/ip/{ip:path}")
async def api_monitoring_check_ip(request: Request, ip: str):
    """Проверить доступность одного IP по SSH."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    tenant_name = None
    for t in data.get("tenants", []):
        if ip in t.get("ips", []):
            tenant_name = t["name"]
            break

    key_path = get_ssh_key_path(data, ip, tenant_name)
    if not key_path:
        return JSONResponse({"ok": False, "error": "SSH ключ не найден"}, status_code=400)

    ssh_user = get_ssh_user(data, ip)
    result = check_ssh_reachable(ip, key_path, ssh_user)

    # Сохраняем результат
    monitoring = data.setdefault("monitoring", {})
    ip_status = monitoring.setdefault("ip_status", {})
    ip_status.setdefault(ip, {})["is_reachable"] = result["reachable"]
    ip_status[ip]["last_check"] = now_msk().isoformat()
    save_data(data)

    return JSONResponse({
        "ok": True,
        "ip": ip,
        "reachable": result["reachable"],
        "error": result.get("error"),
    })


@app.post("/api/monitoring/check/tenant/{tenant_name}")
async def api_monitoring_check_tenant(request: Request, tenant_name: str):
    """Проверить доступность всех IP арендатора."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    data = load_data()
    ips = get_tenant_ips(data, tenant_name)
    if not ips:
        return JSONResponse({"ok": False, "error": "У арендатора нет IP"}, status_code=400)

    def check_one(ip: str) -> dict:
        key_path = get_ssh_key_path(data, ip, tenant_name)
        if not key_path:
            return {"ip": ip, "reachable": False, "error": "SSH ключ не найден"}
        ssh_user = get_ssh_user(data, ip)
        result = check_ssh_reachable(ip, key_path, ssh_user)
        return {"ip": ip, **result}

    with ThreadPoolExecutor(max_workers=MAX_SSH_WORKERS) as executor:
        results = list(executor.map(check_one, ips))

    # Сохраняем результаты
    monitoring = data.setdefault("monitoring", {})
    ip_status = monitoring.setdefault("ip_status", {})
    now = now_msk().isoformat()
    for r in results:
        ip_status.setdefault(r["ip"], {})["is_reachable"] = r["reachable"]
        ip_status[r["ip"]]["last_check"] = now
    save_data(data)

    reachable = sum(1 for r in results if r["reachable"])
    return JSONResponse({
        "ok": True,
        "message": f"Доступно: {reachable}/{len(results)}",
        "results": results,
    })


@app.post("/api/monitoring/traffic/ip/{ip:path}")
async def api_monitoring_traffic_ip(
    request: Request, ip: str,
    days: Optional[int] = Form(None),
    date_from: Optional[str] = Form(None),
    date_to: Optional[str] = Form(None),
):
    """Собрать трафик с одного сервера."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    # Определяем период
    if date_from and date_to:
        period_label = f"{date_from} — {date_to}"
        effective_days = 0  # collect_traffic получит даты напрямую
    else:
        days = days or 1
        period_label = f"за {days} дн."
        date_from = None
        date_to = None

    data = load_data()
    tenant_name = None
    for t in data.get("tenants", []):
        if ip in t.get("ips", []):
            tenant_name = t["name"]
            break

    key_path = get_ssh_key_path(data, ip, tenant_name)
    if not key_path:
        return JSONResponse({"ok": False, "error": "SSH ключ не найден"}, status_code=400)

    ssh_user = get_ssh_user(data, ip)
    result = collect_traffic(ip, key_path, ssh_user, days or 0, date_from, date_to)

    if result["ok"]:
        # Сохраняем данные трафика
        monitoring = data.setdefault("monitoring", {})
        traffic_data = monitoring.setdefault("traffic_data", {})
        traffic_data[ip] = {
            "collected_at": now_msk().isoformat(),
            "days": days,
            "date_from": date_from,
            "date_to": date_to,
            "period": period_label,
            "total_rx_gb": result["total_rx_gb"],
            "total_tx_gb": result["total_tx_gb"],
            "total_gb": result["total_gb"],
            "interfaces": result["traffic"],
        }
        save_data(data)

    return JSONResponse(result)


@app.post("/api/monitoring/traffic/tenant/{tenant_name}")
async def api_monitoring_traffic_tenant(
    request: Request, tenant_name: str,
    days: Optional[int] = Form(None),
    date_from: Optional[str] = Form(None),
    date_to: Optional[str] = Form(None),
):
    """Собрать трафик со всех серверов арендатора."""
    if not get_current_user(request):
        raise HTTPException(status_code=401)

    # Определяем период
    if date_from and date_to:
        period_label = f"{date_from} — {date_to}"
    else:
        days = days or 1
        period_label = f"за {days} дн."
        date_from = None
        date_to = None

    data = load_data()
    ips = get_tenant_ips(data, tenant_name)
    if not ips:
        return JSONResponse({"ok": False, "error": "У арендатора нет IP"}, status_code=400)

    _days = days or 0
    _df, _dt = date_from, date_to

    def collect_one(ip: str) -> dict:
        key_path = get_ssh_key_path(data, ip, tenant_name)
        if not key_path:
            return {"ip": ip, "ok": False, "error": "SSH ключ не найден", "total_gb": 0}
        ssh_user = get_ssh_user(data, ip)
        result = collect_traffic(ip, key_path, ssh_user, _days, _df, _dt)
        return {"ip": ip, **result}

    with ThreadPoolExecutor(max_workers=MAX_SSH_WORKERS) as executor:
        results = list(executor.map(collect_one, ips))

    # Сохраняем и суммируем
    monitoring = data.setdefault("monitoring", {})
    traffic_data = monitoring.setdefault("traffic_data", {})
    total_gb = 0

    for r in results:
        if r.get("ok"):
            traffic_data[r["ip"]] = {
                "collected_at": now_msk().isoformat(),
                "days": days,
                "date_from": date_from,
                "date_to": date_to,
                "period": period_label,
                "total_rx_gb": r.get("total_rx_gb", 0),
                "total_tx_gb": r.get("total_tx_gb", 0),
                "total_gb": r.get("total_gb", 0),
                "interfaces": r.get("traffic", {}),
            }
            total_gb += r.get("total_gb", 0)
    save_data(data)

    ok_count = sum(1 for r in results if r.get("ok"))
    # Собираем предупреждения о снапшотах
    warnings = [r["warning"] for r in results if r.get("warning")]
    resp = {
        "ok": ok_count > 0,
        "message": f"Собрано с {ok_count}/{len(results)} серверов. Общий трафик: {round(total_gb, 2)} ГБ",
        "total_gb": round(total_gb, 2),
        "results": results,
    }
    if warnings:
        resp["warning"] = f"Снапшоты отсутствуют на {len(warnings)} из {len(results)} серверов. Обновите агент."
    return JSONResponse(resp)


@app.post("/api/v1/report")
async def api_v1_report(request: Request):
    """Приём отчётов от агентов мониторинга (ночной cron)."""
    api_key = request.headers.get("X-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    data = load_data()
    monitoring = data.get("monitoring", {})
    api_keys = monitoring.get("api_keys", {})

    ip = api_keys.get(api_key)
    if not ip:
        raise HTTPException(status_code=403, detail="Invalid API key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    interfaces = body.get("interfaces", {})
    if not interfaces:
        raise HTTPException(status_code=400, detail="No interface data")

    # Сохраняем данные трафика
    traffic_data = monitoring.setdefault("traffic_data", {})
    total_rx = sum(v.get("rx_bytes", 0) for v in interfaces.values())
    total_tx = sum(v.get("tx_bytes", 0) for v in interfaces.values())

    traffic_data[ip] = {
        "collected_at": now_msk().isoformat(),
        "total_rx_gb": round(total_rx / (1000**3), 2),
        "total_tx_gb": round(total_tx / (1000**3), 2),
        "total_gb": round((total_rx + total_tx) / (1000**3), 2),
        "interfaces": interfaces,
        "source": "agent",
    }

    # Обновляем статус
    ip_status = monitoring.setdefault("ip_status", {})
    ip_status.setdefault(ip, {})["last_report"] = now_msk().isoformat()
    ip_status[ip]["is_reachable"] = True

    save_data(data)
    logger.info(f"Traffic report from {ip}: {len(interfaces)} interfaces")

    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

"""
VK IP Panel — главный модуль приложения.
FastAPI + Jinja2 + Paramiko для SSH.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import (
    ADMIN_USER, ADMIN_PASS, SECRET_KEY,
    BASE_DIR, MAX_SSH_WORKERS, MAX_CLOUD_WORKERS,
)
from .data import (
    load_data, save_data,
    get_server_by_id, get_script_by_id,
    update_status_cache, get_cached_status, get_cached_cloud,
)
from .ssh import get_script_status, control_script, get_floating_ips_via_cli, change_script_project
from .openstack import get_project_floating_ips

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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "servers": servers,
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
    data["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

    logger.info(f"Projects refresh: {stats['total']} IPs")

    return {
        "ok": True,
        "total_ips": stats["total"],
        "attached": stats["attached"],
        "free": stats["free"],
        "last_update": data["projects_last_update"],
    }


# ─── IPs страница ─────────────────────────────────────────────

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

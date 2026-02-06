"""
Pydantic модели данных.
"""
from typing import Optional
from pydantic import BaseModel


class Script(BaseModel):
    """Скрипт ловли IP на сервере."""
    id: int
    name: str
    path: str
    service_name: str
    state_file: str
    account_name: Optional[str] = None
    project_name: Optional[str] = None


class Server(BaseModel):
    """Сервер с скриптами."""
    id: int
    name: str
    host: str
    port: int = 22
    user: str
    password: Optional[str] = None
    key_path: Optional[str] = None
    scripts: list[Script] = []


class ScriptStatus(BaseModel):
    """Статус скрипта."""
    server_id: int
    script_id: int
    server_name: str = ""
    script_name: str = ""
    running: bool = False
    cycles: int = 0
    success: int = 0
    last_ip: Optional[str] = None
    account: Optional[str] = None
    project: Optional[str] = None
    error: Optional[str] = None


class FloatingIP(BaseModel):
    """Floating IP из VK Cloud."""
    ip: str
    id: str
    status: Optional[str] = None
    fixed_ip: Optional[str] = None
    port_id: Optional[str] = None
    attached: bool = False
    server_name: Optional[str] = None


class CloudProject(BaseModel):
    """Проект VK Cloud."""
    name: str
    username: str
    password: str
    auth_url: str
    project_id: str


class ProjectCache(BaseModel):
    """Кэш данных проекта."""
    ips: list[FloatingIP] = []
    account: Optional[str] = None
    project: Optional[str] = None
    error: Optional[str] = None

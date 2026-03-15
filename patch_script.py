#!/usr/bin/env python3
"""
Патч script.py на серверах ловли IP:
1. Добавляет функцию panel_report_ip() после секции Telegram
2. Заменяет tg_send_html при SUCCESS на panel_report_ip
"""
import sys

PANEL_FUNC = '''
# =========================
# Panel Report (found IP)
# =========================
def panel_report_ip(ip: str, fip_id: str, subnet: str, service: str, server: str, project: str, account: str, logger: Optional[logging.Logger] = None) -> bool:
    """Отправляет найденный IP на панель VK IP Panel."""
    panel_url = os.getenv("PANEL_URL", "http://45.156.26.113:8080")
    url = panel_url.rstrip("/") + "/api/v1/found-ip"
    payload = {
        "ip": ip,
        "fip_id": fip_id,
        "subnet": subnet,
        "service": service,
        "server": server,
        "project": project,
        "account": account,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            if logger:
                logger.info(f"Panel report OK: {ip}")
            return True
        if logger:
            logger.warning(f"Panel report failed: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        if logger:
            logger.warning(f"Panel report error: {e!r}")
        return False

'''

def patch(content: str) -> str:
    # 1. Добавить panel_report_ip после секции Telegram (перед OpenStack CLI helpers)
    marker = "# =========================\n# OpenStack CLI helpers"
    if "panel_report_ip" not in content:
        if marker in content:
            content = content.replace(marker, PANEL_FUNC + marker)
        else:
            print("WARNING: OpenStack CLI helpers marker not found", file=sys.stderr)

    # 2. Заменить tg_send_html при SUCCESS на panel_report_ip
    # Ищем блок: tg_send_html( ... "🎯 <b>УСПЕХ!</b>" ...
    old_tg_block = '''                    tg_send_html(
                        f"🎯 <b>УСПЕХ!</b> {tag_html}\\n"
                        f"<b>{html_escape(SERVICE_NAME)}</b>\\n"
                        f"{identity_line}"
                        f"<code>{html_escape(sname)}</code> → <b>{html_escape(str(ip))}</b>\\n"
                        f"FIP id: <code>{html_escape(str(fid))}</code>",
                        logger
                    )'''
    
    new_panel_call = '''                    import socket
                    panel_report_ip(
                        ip=str(ip), fip_id=str(fid), subnet=sname,
                        service=SERVICE_NAME, server=socket.gethostname(),
                        project=os_proj, account=os_user, logger=logger
                    )'''

    if old_tg_block in content:
        content = content.replace(old_tg_block, new_panel_call)
    else:
        print("WARNING: tg_send_html SUCCESS block not found exactly, trying flexible match", file=sys.stderr)
        # Попробуем более гибкий поиск
        import re
        pattern = r'tg_send_html\(\s*f"🎯.*?УСПЕХ.*?logger\s*\)'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            content = content[:match.start()] + '''import socket
                    panel_report_ip(
                        ip=str(ip), fip_id=str(fid), subnet=sname,
                        service=SERVICE_NAME, server=socket.gethostname(),
                        project=os_proj, account=os_user, logger=logger
                    )''' + content[match.end():]
        else:
            print("ERROR: Could not find tg_send_html SUCCESS block", file=sys.stderr)
    
    return content


if __name__ == "__main__":
    content = sys.stdin.read()
    result = patch(content)
    sys.stdout.write(result)

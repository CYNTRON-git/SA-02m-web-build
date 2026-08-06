# -*- coding: utf-8 -*-
"""Общие константы и хелперы для dev/test-скриптов SA-02m."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HOST = os.environ.get("SA02M_HOST", "192.168.1.136")
USER = os.environ.get("SA02M_USER", "root")
PASSWORD = os.environ.get("SA02M_PASS", "cyntron")
WEB_BASE = "http://127.0.0.1:9999"
FLASHER_SOCK = "/run/sa02m-flasher/flasher.sock"

_WEB_TOKEN_ENV = "SA02M_WEB_TOKEN"


def web_cookie() -> str:
    """Cookie-заголовок для веб-слоя устройства: session_token с живым токеном.

    Токен НЕ хранится в репозитории. Раньше здесь была статическая константа;
    2026-07-12 её заменила серверная сессионная модель (единственный дом
    правила — www/network_config/cgi-bin/lib_web_auth.sh), и с тех пор все
    десять потребителей этого модуля молча получали с устройства
    `{"error":"unauthorized"}` вместо данных — HTTP 200, так что ничего
    не падало.

    Живой токен берётся из cookie браузерной сессии панели (DevTools →
    Application → Cookies → session_token) и передаётся через окружение:

        SA02M_WEB_TOKEN=<hex> python3 tools/test/test_panel_poll_loop.py
    """
    token = (os.environ.get(_WEB_TOKEN_ENV) or "").strip()
    if not token:
        raise SystemExit(
            f"{_WEB_TOKEN_ENV} не задан: экспортируйте живой session_token панели "
            f"(DevTools -> Application -> Cookies -> session_token). "
            f"Статического токена в репозитории больше нет."
        )
    return f"session_token={token}"


def __getattr__(name: str) -> str:
    """Разрешение имени WEB_COOKIE через PEP 562 — НЕ ленивое для потребителей.

    Все десять потребителей пишут `from sa02m_common import WEB_COOKIE`, а
    from-import разрешает модульный __getattr__ В МОМЕНТ ИМПОРТА. Поэтому при
    незаданном SA02M_WEB_TOKEN каждый из них падает сразу на импорте — громко,
    с понятным сообщением, ДО единого обращения к устройству. Это осознанный
    выбор, а не недоделка: все десять — инструменты доступа к веб-слою
    устройства, безтокенного режима работы у них нет, так что «упасть позже»
    ничего бы не выиграло, а править десять файлов ради этого не за чем.

    Отложенность здесь ровно одна: `import sa02m_common` (сам модуль) и доступ
    к HOST/FLASHER_SOCK/ssh_run работают без токена — токен требуется только
    тому, кто запрашивает имя WEB_COOKIE (любым способом импорта).
    """
    if name == "WEB_COOKIE":
        return web_cookie()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def ssh_exec(command: str, *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools/ssh/sa02m_remote.py"), "exec", command],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=timeout,
    )


def ssh_run(command: str, *, timeout: float = 120.0) -> str:
    """Remote command via sa02m_remote.py; returns combined stdout+stderr."""
    result = ssh_exec(command, timeout=timeout)
    return ((result.stdout or "") + (result.stderr or "")).strip()


def connect_ssh():
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    return client

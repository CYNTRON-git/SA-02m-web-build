# -*- coding: utf-8 -*-
"""
Конфигурация демона. Читается из /etc/sa02m_flasher.conf (shell-like KEY=VALUE),
переопределяется аргументами командной строки и переменными окружения SA02M_FLASHER_*.
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_CONF_PATH = Path("/etc/sa02m_flasher.conf")

DEFAULT_SOCKET_PATH = "/run/sa02m-flasher/flasher.sock"
DEFAULT_CACHE_DIR = "/var/lib/sa02m-flasher/firmware"
DEFAULT_LOG_DIR = "/var/log/sa02m-flasher"
DEFAULT_LOCK_DIR = "/var/lock"

DEFAULT_MANIFEST_URL = "https://cyntron.ru/upload/medialibrary/cyntron/firmware/index.json"
DEFAULT_FIRMWARE_BASE_URL = "https://cyntron.ru/upload/medialibrary/cyntron/firmware/"

# Фронтенд ожидает, что COM1..COM5 — симлинки, созданные в scripts/01-system.sh.
DEFAULT_PORTS_MAP: Dict[str, str] = {
    "COM1": "/dev/COM1",
    "COM2": "/dev/COM2",
    "COM3": "/dev/COM3",
    "COM4": "/dev/COM4",
    "COM5": "/dev/COM5",
}

# Единственное соответствие: RS-485-N → /dev/RS-485-N (для отображения в UI вместе с /dev/ttyS*).
DEFAULT_PORTS_LABELS: Dict[str, str] = {
    "COM1": "RS-485-0",
    "COM2": "RS-485-1",
    "COM3": "RS-485-2",
    "COM4": "RS-485-3",
    "COM5": "RS-485-4",
}

DEFAULT_MPLC_STOP_SERVICES: List[str] = ["mplc.service"]

# Лимиты и тайминги.
DEFAULT_MAX_JOB_SECONDS = 1800       # страховка на одну задачу (до 30 мин)
DEFAULT_SESSION_COOKIE = "session_token=cyntron_session"
DEFAULT_INTERNAL_TOKEN = ""          # общий секрет между nginx и демоном (если пусто — проверка только по cookie)


@dataclass
class FlasherConfig:
    socket_path: str = DEFAULT_SOCKET_PATH
    cache_dir: Path = field(default_factory=lambda: Path(DEFAULT_CACHE_DIR))
    log_dir: Path = field(default_factory=lambda: Path(DEFAULT_LOG_DIR))
    lock_dir: Path = field(default_factory=lambda: Path(DEFAULT_LOCK_DIR))
    manifest_url: str = DEFAULT_MANIFEST_URL
    firmware_base_url: str = DEFAULT_FIRMWARE_BASE_URL
    ports_map: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PORTS_MAP))
    ports_labels: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PORTS_LABELS))
    mplc_stop_services: List[str] = field(default_factory=lambda: list(DEFAULT_MPLC_STOP_SERVICES))
    max_job_seconds: int = DEFAULT_MAX_JOB_SECONDS
    session_cookie: str = DEFAULT_SESSION_COOKIE
    internal_token: str = DEFAULT_INTERNAL_TOKEN


def _parse_shell_conf(path: Path) -> Dict[str, str]:
    """Простой KEY=VALUE парсер (как в /etc/sa02m_hw.conf)."""
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        try:
            parts = shlex.split(val, posix=True)
        except ValueError:
            parts = [val.strip()]
        result[key] = parts[0] if len(parts) == 1 else " ".join(parts)
    return result


def load_config(conf_path: Optional[Path] = None) -> FlasherConfig:
    """Собрать конфиг из: defaults → /etc/sa02m_flasher.conf → переменные SA02M_FLASHER_*."""
    cfg = FlasherConfig()
    path = conf_path or DEFAULT_CONF_PATH
    file_vars = _parse_shell_conf(path)
    for src in (file_vars, os.environ):
        def g(key: str, default: Optional[str] = None) -> Optional[str]:
            return src.get(key, src.get("SA02M_FLASHER_" + key, default))

        v = g("SOCKET_PATH")
        if v:
            cfg.socket_path = v
        v = g("CACHE_DIR")
        if v:
            cfg.cache_dir = Path(v)
        v = g("LOG_DIR")
        if v:
            cfg.log_dir = Path(v)
        v = g("LOCK_DIR")
        if v:
            cfg.lock_dir = Path(v)
        v = g("MANIFEST_URL")
        if v:
            cfg.manifest_url = v
        v = g("FIRMWARE_BASE_URL")
        if v:
            cfg.firmware_base_url = v
        v = g("MPLC_STOP_SERVICES")
        if v is not None:
            cfg.mplc_stop_services = [s for s in v.replace(",", " ").split() if s]
        v = g("MAX_JOB_SECONDS")
        if v:
            try:
                cfg.max_job_seconds = int(v)
            except ValueError:
                pass
        v = g("SESSION_COOKIE")
        if v:
            cfg.session_cookie = v
        v = g("INTERNAL_TOKEN")
        if v is not None:
            cfg.internal_token = v
    return cfg

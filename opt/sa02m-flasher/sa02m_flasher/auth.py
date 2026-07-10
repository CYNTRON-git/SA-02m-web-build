# -*- coding: utf-8 -*-
"""
Проверка авторизации для API демона.

Механизм: серверные веб-сессии SA-02м. Успешный логин (www/.../login.cgi) выдаёт
случайный per-session токен и кладёт файл sha256(token) в каталог сессий (tmpfs).
Демон читает тот же каталог read-only и проверяет срок годности. Схема хэша обязана
совпадать с bash-библиотекой lib_web_session.sh:
    bash:   printf '%s' "$tok" | sha256sum
    python: hashlib.sha256(tok.encode()).hexdigest()

Дополнительно — общий секрет между nginx и демоном через header X-SA02M-Auth
(если задан INTERNAL_TOKEN в /etc/sa02m_flasher.conf).
"""
from __future__ import annotations

import hashlib
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Optional


def _cookie_value(header: Optional[str], name: str) -> Optional[str]:
    if not header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(header)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _session_token(cookie_header: Optional[str]) -> Optional[str]:
    """Достать session_token и провалидировать формат (как в lib_web_session.sh)."""
    tok = _cookie_value(cookie_header, "session_token")
    if not tok:
        return None
    tok = tok.strip()
    if not (32 <= len(tok) <= 128):
        return None
    if any(c not in "0123456789abcdef" for c in tok):
        return None
    return tok


def check_session_store(
    cookie_header: Optional[str],
    session_dir: str,
    now: Optional[float] = None,
) -> bool:
    """
    True, если cookie session_token соответствует непросроченной серверной сессии.
    Read-only: срок не продлевается (продление — за CGI-слоем, владельцем файлов).
    """
    tok = _session_token(cookie_header)
    if not tok:
        return False
    digest = hashlib.sha256(tok.encode("ascii", "strict")).hexdigest()
    path = Path(session_dir) / digest
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return False
    exp_str = first_line.split(" ", 1)[0]
    try:
        expiry = int(exp_str)
    except ValueError:
        return False
    return (now if now is not None else time.time()) < expiry


def check_internal_token(header_value: Optional[str], expected_token: str) -> bool:
    """Если токен задан — сравниваем в постоянное время."""
    if not expected_token:
        return True  # токен не требуется
    if not header_value:
        return False
    a = expected_token.encode("utf-8", "replace")
    b = header_value.encode("utf-8", "replace")
    if len(a) != len(b):
        return False
    res = 0
    for x, y in zip(a, b):
        res |= x ^ y
    return res == 0

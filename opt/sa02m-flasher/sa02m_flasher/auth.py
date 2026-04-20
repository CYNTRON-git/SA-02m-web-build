# -*- coding: utf-8 -*-
"""
Проверка авторизации для API демона.

Механизм: тот же cookie `session_token=cyntron_session`, что используется в
www/network_config/cgi-bin/*.cgi (см. status.cgi/login.cgi). Дополнительно —
общий секрет между nginx и демоном через header X-SA02M-Auth (если задан
INTERNAL_TOKEN в /etc/sa02m_flasher.conf).
"""
from __future__ import annotations

from http.cookies import SimpleCookie
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


def check_session(cookie_header: Optional[str], expected_cookie: str) -> bool:
    """
    expected_cookie задаётся в формате 'key=value' (как в status.cgi-регексе).
    Возврат True, если в заголовке Cookie присутствует та же пара.
    """
    if not expected_cookie or "=" not in expected_cookie:
        return False
    key, _, val = expected_cookie.partition("=")
    got = _cookie_value(cookie_header, key.strip())
    return got == val.strip()


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

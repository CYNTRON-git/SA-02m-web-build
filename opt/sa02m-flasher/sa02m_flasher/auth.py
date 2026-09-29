# -*- coding: utf-8 -*-
"""
Проверка авторизации для API демона.

Механизм: серверные веб-сессии SA-02м. Успешный логин (www/.../login.cgi) выдаёт
случайный per-session токен и кладёт файл sha256(token) в каталог сессий (tmpfs).
Демон читает тот же каталог read-only и проверяет срок годности. Схема хэша обязана
совпадать с bash-библиотекой lib_web_auth.sh:
    bash:   printf '%s' "$tok" | sha256sum
    python: hashlib.sha256(tok.encode()).hexdigest()

Дополнительно — общий секрет для локального вызывающего на unix-сокете через
header X-SA02M-Auth (если задан INTERNAL_TOKEN в /etc/sa02m_flasher.conf).
nginx этот заголовок сам НЕ ставит и клиентское значение НЕ пропускает: на
обеих flasher-локациях он перезаписан пустым (etc/nginx/network_config.conf,
гейт flasher-auth-header-strip) — заголовок не является клиентским
credential'ом и из браузера/облака до демона не доходит.
"""
from __future__ import annotations

from typing import Optional

# The session/CSRF rule itself lives in websession.py (one rule, two byte-identical
# copies across the daemons — row `websession-parity`); this module keeps the
# names service.py and the tests import, plus the flasher-only INTERNAL_TOKEN seam.
from .websession import (  # noqa: F401 — re-exported API
    _cookie_value,
    check_csrf,
    check_session_store,
    csrf_error_body,
    session_token_from_cookie as _session_token,
)


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

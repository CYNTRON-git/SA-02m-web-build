"""The OAuth token file: `/var/lib/sa02m-homeconnect/tokens.json`.

Readable only by the daemon user (P4): written 0600 through fsutil's
descriptor-safe atomic write; on load the file must be a regular single-link
file owned by this process's euid with no group/other bits — anything else is
refused unread (`token_store_insecure`), never "fixed" and used. The values
never leave this module and `oauth.py`: `TokenSet` redacts itself in repr,
and nothing here logs a value.

A refresh the server refuses (`invalid_grant`) replaces the file with a
revoked marker (`{"revoked_at": …}`) — the secret is gone, and the
«доступ отозван» state survives a restart until the integrator links again.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import constants as C
from .fsutil import UnsafeFile, atomic_write, read_private, remove_quietly

log = logging.getLogger("sa02m_homeconnect.tokens")

_MAX_BYTES = 16384


@dataclass(repr=False)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: int
    scope: str
    host: str
    client_id: str
    linked_at: int

    def __repr__(self) -> str:
        return "<TokenSet host=%s expires_at=%d (values redacted)>" % (self.host, self.expires_at)

    __str__ = __repr__

    def to_json(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": int(self.expires_at),
            "scope": self.scope,
            "host": self.host,
            "client_id": self.client_id,
            "linked_at": int(self.linked_at),
        }


@dataclass
class LoadResult:
    tokens: Optional[TokenSet] = None
    revoked_at: int = 0
    # "" | constants.REASON_TOKEN_STORE_INSECURE | REASON_TOKEN_STORE_CORRUPT
    problem: str = ""


def _str(data: Dict[str, Any], key: str, max_len: int = 8192) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError("field %s missing or malformed" % key)
    return value


def _int(data: Dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("field %s missing or malformed" % key)
    return value


class TokenStore:
    def __init__(self, path: str = C.TOKENS_FILE) -> None:
        self.path = path

    def load(self) -> LoadResult:
        try:
            raw, _st = read_private(self.path, _MAX_BYTES)
        except FileNotFoundError:
            return LoadResult()
        except UnsafeFile as exc:
            log.error("token file refused, not read: %s", exc)
            return LoadResult(problem=C.REASON_TOKEN_STORE_INSECURE)
        except OSError as exc:
            log.error("token file unreadable: %s", exc.strerror or type(exc).__name__)
            return LoadResult(problem=C.REASON_TOKEN_STORE_CORRUPT)
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            if "revoked_at" in data and "refresh_token" not in data:
                return LoadResult(revoked_at=_int(data, "revoked_at"))
            tokens = TokenSet(
                access_token=_str(data, "access_token"),
                refresh_token=_str(data, "refresh_token"),
                expires_at=_int(data, "expires_at"),
                scope=data.get("scope") if isinstance(data.get("scope"), str) else "",
                host=_str(data, "host", 32),
                client_id=_str(data, "client_id", 128),
                linked_at=_int(data, "linked_at"),
            )
        except (ValueError, UnicodeDecodeError) as exc:
            # The message names the field, never a value.
            log.error("token file malformed: %s", exc)
            return LoadResult(problem=C.REASON_TOKEN_STORE_CORRUPT)
        return LoadResult(tokens=tokens)

    def save(self, tokens: TokenSet) -> None:
        atomic_write(self.path, json.dumps(tokens.to_json(), sort_keys=True) + "\n", mode=0o600)

    def mark_revoked(self, now: int) -> None:
        atomic_write(self.path, json.dumps({"revoked_at": int(now)}) + "\n", mode=0o600)

    def clear(self) -> bool:
        return remove_quietly(self.path)

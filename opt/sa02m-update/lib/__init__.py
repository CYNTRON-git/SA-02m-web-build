# -*- coding: utf-8 -*-
"""SA-02m offline update helpers (validate, upload receive, transaction journal)."""

from __future__ import annotations

__all__ = [
    "ERROR_CODES",
    "PackageError",
]

# Shared error_code vocabulary (transaction.json / CGI).
ERROR_CODES = frozenset(
    {
        "E_TRAILER",
        "E_TAR",
        "E_SIG",
        "E_HASH",
        "E_COMPAT",
        "E_SPACE",
        "E_CMD",
        "E_TAR_TRAV",
        "E_TAR_SYMLINK",
        "E_TAR_DEVICE",
        "E_TAR_BOMB",
        "E_APPLY",
        "E_HEALTH",
        "E_LOCK",
        "E_CANCEL",
        "E_POWER",
        "E_INTERNAL",
        "E_UPLOAD",
        "E_MANIFEST",
    }
)


class PackageError(Exception):
    """Package / upload / transaction failure with stable error_code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_CODES:
            code = "E_INTERNAL"
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

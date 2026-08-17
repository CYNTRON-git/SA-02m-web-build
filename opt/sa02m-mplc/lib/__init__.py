# -*- coding: utf-8 -*-
"""SA-02m MPLC4 project-deploy helpers (upload parse, zip validate/extract).

Security-critical: the sole input is an attacker-controlled multipart upload on
the LAN. Every function here fails CLOSED — a malformed, oversized, or hostile
archive raises ProjectZipError and NOTHING is written outside the caller's fixed
destination. The four project files are the ONLY members ever read; the deploy
target path is a constant the caller pins, never derived from a zip entry name.
"""

from __future__ import annotations

__all__ = ["PROJECT_ERROR_CODES", "ProjectZipError"]

# Stable error_code vocabulary surfaced to the CGI/UI (contract mplc-project-deploy.md).
PROJECT_ERROR_CODES = frozenset(
    {
        "E_UPLOAD",     # multipart body malformed / field missing / empty
        "E_SIZE",       # body or an entry exceeds a cap (anti zip-bomb)
        "E_ZIP",        # not a valid zip / too many entries
        "E_TRAVERSAL",  # zip-slip: a member name escapes cfg/ (.., absolute, symlink)
        "E_MEMBERS",    # not the exact MasterSCADA export member set
        "E_PROJINFO",   # ProjInfo.json missing / not JSON / missing required keys
        "E_HASH",       # _files.xml SHA mismatch (only when verification is enabled)
        "E_INTERNAL",   # unexpected
    }
)


class ProjectZipError(Exception):
    """Project upload/zip failure with a stable error_code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in PROJECT_ERROR_CODES:
            code = "E_INTERNAL"
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

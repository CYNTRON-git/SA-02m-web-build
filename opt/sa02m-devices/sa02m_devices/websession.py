# -*- coding: utf-8 -*-
"""Panel web-session + CSRF rule for the HTTP daemons — the read-only Python mirror.

The rule is OWNED by the CGI layer (www/network_config/cgi-bin/lib_web_auth.sh):
a login mints a random hex token, stores ``<sha256(token)>`` in the session dir
(tmpfs, ``/run/sa02m-web-sessions``) with ``"<expiry_epoch> <user>"`` as its
first line, and a sibling ``<sha256(token)>.csrf`` holding the per-session CSRF
token the panel sends back in the ``X-SA02M-CSRF`` header. A daemon reads that
store read-only and MUST agree with the bash library byte for byte:

    bash:   printf '%s' "$tok" | sha256sum        python: hashlib.sha256(tok.encode()).hexdigest()
    bash:   web_csrf_validate (reason enum)        python: check_csrf() (same enum)

ONE RULE, TWO BYTE-IDENTICAL COPIES — this file lives in BOTH daemon packages:
    opt/sa02m-flasher/sa02m_flasher/websession.py
    opt/sa02m-devices/sa02m_devices/websession.py
pinned identical by the quality row ``websession-parity``. Why copies and not a
shared package (``opt/sa02m-websession``) or a cross-tree import: the trees are
deployed and refreshed INDEPENDENTLY (``update-www-only.sh``, the OTA map), and
the carel/led shared-home gates exist because exactly that class broke; ~100
lines of leaf code with no imports from either package is cheaper as a pinned
copy than as a third deploy path (docs/decisions/selective-csrf-policy.md
«Демоны»). Edit ONE copy, then ``cp`` it over the other — the row fails otherwise.

Never log a header value, a cookie token or a token-file content from here.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_SESSION_DIR = "/run/sa02m-web-sessions"  # login.cgi creates it (lib_web_auth.sh SA02M_SESSION_DIR)
CSRF_HEADER = "X-SA02M-CSRF"
# The closed reason enum web_csrf_validate leaves in WEB_CSRF_FAIL_REASON — a
# value the panel branches on (app.js sa02mHandleCsrfRejection), never free text.
CSRF_REASONS = ("no_session", "no_token_file", "no_header", "mismatch")


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


def session_token_from_cookie(cookie_header: Optional[str]) -> Optional[str]:
    """The ``session_token`` cookie value, validated to the store's shape (hex,
    32..128 chars — lib_web_auth.sh web_session__cookie_token); None otherwise.
    The shape check is what makes the token safe to hash into a file name."""
    tok = _cookie_value(cookie_header, "session_token")
    if not tok:
        return None
    tok = tok.strip()
    if not (32 <= len(tok) <= 128):
        return None
    if any(c not in "0123456789abcdef" for c in tok):
        return None
    return tok


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "strict")).hexdigest()


def check_session_store(
    cookie_header: Optional[str],
    session_dir: str,
    now: Optional[float] = None,
) -> bool:
    """True iff the cookie's session_token names a live (unexpired) server session.
    Read-only: the expiry is never extended here (renewal belongs to the CGI
    layer, the owner of the files). Fail closed on every read/parse error."""
    tok = session_token_from_cookie(cookie_header)
    if not tok:
        return False
    path = Path(session_dir) / _session_hash(tok)
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


def check_csrf(
    cookie_header: Optional[str],
    csrf_header: Optional[str],
    session_dir: str,
) -> Tuple[bool, str]:
    """Mirror of web_csrf_validate: ``(True, "")`` when ``csrf_header`` equals the
    session's ``<hash>.csrf`` content, else ``(False, reason)`` with reason from
    CSRF_REASONS. Call it AFTER check_session_store() and BEFORE any mutation —
    like the bash rule it does not re-check the session's expiry itself.

    Fail closed: no/invalid cookie token → no_session; no token file, an
    unreadable one, or one empty after CR-stripping → no_token_file; an absent or
    empty header → no_header; anything else that is not equal → mismatch. The file
    path is built only from the validated hex token's sha256, so no request value
    can steer it. Comparison is constant-time (hmac.compare_digest)."""
    tok = session_token_from_cookie(cookie_header)
    if not tok:
        return False, "no_session"
    path = Path(session_dir) / (_session_hash(tok) + ".csrf")
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "no_token_file"
    stored = raw.splitlines()[0] if raw.splitlines() else ""
    stored = stored.replace("\r", "")
    if not stored:
        return False, "no_token_file"
    got = csrf_header or ""
    if not got:
        return False, "no_header"
    if hmac.compare_digest(got.encode("utf-8", "replace"), stored.encode("utf-8", "replace")):
        return True, ""
    return False, "mismatch"


def csrf_error_body(reason: str) -> Dict[str, Any]:
    """The E_CSRF body the panel already understands — byte-for-byte the CGI
    layer's web_csrf_error_body (lib_web_auth.sh), with the reason closed to the
    enum (``unknown`` otherwise) so it is always safe to serialise."""
    return {
        "ok": False,
        "error": "csrf",
        "error_code": "E_CSRF",
        "reason": reason if reason in CSRF_REASONS else "unknown",
    }

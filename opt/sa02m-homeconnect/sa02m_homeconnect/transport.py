"""HTTPS to the BSH cloud — the one place a socket is opened.

* TLS verified against the system roots (`ssl.create_default_context()`).
* No proxy (environment proxies are ignored — the unit sets none, and a
  proxy would see the bearer token) and **no redirects**: urllib would carry
  the Authorization header to whatever host a 3xx names, so a 3xx is an error.
* https only. Plain http is accepted solely for a loopback base URL the test
  suite injects (`allow_loopback_http=True`); production URLs come from
  constants.HOSTS and are https.
* Every response body is capped (MAX_RESPONSE_BYTES); the event stream is
  handed back unread for the SSE reader, which caps its own lines.

Budget accounting is NOT here: callers charge `budget.Budget` before calling.
"""

from __future__ import annotations

import email.utils
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional

from . import constants as C


class NetworkError(Exception):
    """The cloud was not reached (DNS, TCP, TLS, timeout, reset)."""


class HttpError(Exception):
    """The cloud answered with a non-2xx status."""

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = int(status)
        self.headers = {k.lower(): v for k, v in dict(headers or {}).items()}
        self.body = body[: C.MAX_RESPONSE_BYTES]
        self.error_key = _error_key(self.body)
        super().__init__("HTTP %d %s" % (self.status, self.error_key or ""))

    def retry_after_s(self, now: Optional[float] = None) -> Optional[int]:
        return parse_retry_after(self.headers.get("retry-after"), now)


def _error_key(body: bytes) -> str:
    """`error.key` of a BSH error, or the OAuth `error` string; '' otherwise."""
    try:
        data = json.loads(body.decode("utf-8", "replace") or "null")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, str):
        return err[:80]
    if isinstance(err, dict) and isinstance(err.get("key"), str):
        return err["key"][:80]
    return ""


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[int]:
    """Seconds to wait from a Retry-After value (delta-seconds or HTTP-date),
    clamped to [1, RETRY_AFTER_MAX_S]; None when absent or unparseable."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        seconds = int(value[:9])
    else:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = int(when.timestamp() - (time.time() if now is None else now))
    return max(1, min(seconds, C.RETRY_AFTER_MAX_S))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None  # urllib then raises HTTPError(3xx) — handled as an error


def _check_base_url(base_url: str, allow_loopback_http: bool) -> str:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme == "https" and parts.hostname:
        return base_url.rstrip("/")
    if (parts.scheme == "http" and allow_loopback_http
            and parts.hostname in ("127.0.0.1", "localhost", "::1")):
        return base_url.rstrip("/")
    raise ValueError("refused base URL %r: https only" % base_url)


class Transport:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = C.HTTP_TIMEOUT_S,
        allow_loopback_http: bool = False,
    ) -> None:
        self.base_url = _check_base_url(base_url, allow_loopback_http)
        self.timeout = timeout
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )

    def _open(self, method: str, path: str, headers: Dict[str, str], body: Optional[bytes],
              timeout: float) -> Any:
        if not path.startswith("/"):
            raise ValueError("path must be absolute: %r" % path)
        req = urllib.request.Request(self.base_url + path, data=body, method=method)
        req.add_header("User-Agent", C.USER_AGENT)
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read(C.MAX_RESPONSE_BYTES + 1)
            except Exception:
                data = b""
            finally:
                exc.close()
            raise HttpError(exc.code, dict(exc.headers or {}), data) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                ssl.SSLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(str(reason)[:200]) from None

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        form: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send one request; return the decoded JSON body ({} when empty).
        Raises HttpError / NetworkError; a body that is not JSON is HttpError."""
        hdrs = dict(headers or {})
        body = None
        if form is not None:
            body = urllib.parse.urlencode(dict(form)).encode("ascii")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        resp = self._open(method, path, hdrs, body, timeout or self.timeout)
        try:
            try:
                data = resp.read(C.MAX_RESPONSE_BYTES + 1)
            except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                raise NetworkError("read failed: %s" % exc) from None
            status = resp.status
            resp_headers = dict(resp.headers or {})
        finally:
            resp.close()
        if len(data) > C.MAX_RESPONSE_BYTES:
            raise HttpError(status, resp_headers, b'{"error":"response_too_large"}')
        if not data.strip():
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HttpError(status, resp_headers, b'{"error":"invalid_json"}') from None

    def open_stream(self, path: str, *, headers: Optional[Dict[str, str]] = None,
                    read_timeout: float = C.SSE_READ_TIMEOUT_S) -> Any:
        """GET `path` and return the open response for line reading. The
        socket timeout is the per-read keep-alive deadline."""
        return self._open("GET", path, dict(headers or {}), None, read_timeout)

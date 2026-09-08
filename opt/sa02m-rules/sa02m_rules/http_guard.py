"""Outbound HTTP policy for scenarios — the `http` action and the sandbox
`Http` class share it (docs/contracts/cloud-scenarios.md §Outbound HTTP).

A cloud-pushed scenario must not turn the board into a request proxy into
its own LAN or loopback services (nginx/CGI, the flasher daemon,
sa02m-devices-api, mosquitto). The policy: the target host is resolved and
every address must be public — loopback, link-local, RFC1918/ULA, reserved,
multicast and unspecified ranges are refused — and a URL carrying
`user:pass@` is refused; the operator may list hosts that bypass the address
check in `/etc/sa02m-rules/http-allow.json` (`{"hosts": [...]}`, exact
hostname or literal address, case-insensitive). Redirects are followed at
most MAX_REDIRECTS times with every hop re-checked; at most
MAX_RESPONSE_BYTES of a body are read. Limit stated honestly: the check
resolves the name once and urllib resolves it again to connect, so a DNS
answer that changes between the two (rebinding) is not defended.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional, Set, Tuple

DEFAULT_ALLOW_PATH = "/etc/sa02m-rules/http-allow.json"
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_S = 5
URL_MAX = 500
BODY_MAX = 2000
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class HttpRefused(RuntimeError):
    """The request was refused by policy (never sent) or exceeded a cap."""


def allowed_hosts(path: Optional[str] = None) -> Set[str]:
    """Operator allow-list; absent/unreadable/malformed ⇒ empty (fail closed)."""
    path = path or os.environ.get("SA02M_RULES_HTTP_ALLOW", DEFAULT_ALLOW_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, list):
        return set()
    return {str(h).strip().lower().strip("[]") for h in hosts
            if isinstance(h, str) and h.strip()}


def _address_is_public(addr: ipaddress._BaseAddress) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (addr.is_loopback or addr.is_link_local or addr.is_private
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _resolve(host: str, port: int) -> Iterable[ipaddress._BaseAddress]:
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        yield ipaddress.ip_address(sockaddr[0])


def check_url(url: str, resolve: bool = True, allow: Optional[Set[str]] = None) -> str:
    """Return "" when `url` may be fetched, else the refusal reason.

    `resolve=False` is the static half (store validation: no DNS, literal
    addresses and userinfo only); `resolve=True` is the request-time check."""
    url = str(url or "")
    if len(url) > URL_MAX:
        return "url too long"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "bad url"
    if parts.scheme not in ("http", "https"):
        return "bad url"
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "bad url"
    if not host:
        return "bad url"
    host = host.lower()
    if allow is None:
        allow = allowed_hosts()
    if host in allow:
        return ""
    if parts.username is not None or parts.password is not None:
        return "refused: credentials in url"
    port = port or (443 if parts.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return "" if _address_is_public(literal) else "refused: private address"
    if not resolve:
        return ""
    try:
        addrs = list(_resolve(host, port))
    except (socket.gaierror, OSError, ValueError):
        return "refused: unresolvable host"
    if not addrs:
        return "refused: unresolvable host"
    if not all(_address_is_public(a) for a in addrs):
        return "refused: private address"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 3xx as HTTPError; fetch() walks hops itself


_OPENER = urllib.request.build_opener(_NoRedirect())


def fetch(method: str, url: str, body: Optional[str] = None,
          timeout: float = TIMEOUT_S, allow: Optional[Set[str]] = None) -> Tuple[int, int]:
    """Policy-checked request. Returns (status, bytes_read) — the body is
    never returned to a scenario. Raises HttpRefused on a policy refusal, a
    redirect past the cap, or a transport error. An HTTP error status
    (4xx/5xx) is returned as a status, not raised."""
    method = "POST" if str(method).upper() == "POST" else "GET"
    if allow is None:
        allow = allowed_hosts()
    data = None
    if method == "POST" and body is not None:
        data = str(body)[:BODY_MAX].encode("utf-8")
    hops = 0
    while True:
        reason = check_url(url, resolve=True, allow=allow)
        if reason:
            raise HttpRefused(reason)
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "text/plain; charset=utf-8")
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                chunk = resp.read(MAX_RESPONSE_BYTES)
                return int(resp.status), len(chunk)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in _REDIRECT_CODES and location:
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise HttpRefused("refused: too many redirects")
                url = urllib.parse.urljoin(url, location)
                if exc.code == 303 or (exc.code in (301, 302) and method == "POST"):
                    method, data = "GET", None
                continue
            return int(exc.code), 0
        except HttpRefused:
            raise
        except Exception as exc:  # URLError, socket timeout, TLS …
            raise HttpRefused("err %s" % str(exc)[:120])

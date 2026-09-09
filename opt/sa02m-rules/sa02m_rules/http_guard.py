"""Outbound HTTP policy for scenarios — the `http` action and the sandbox
`Http` class share it (docs/contracts/cloud-scenarios.md §Outbound HTTP).

A cloud-pushed scenario must not turn the board into a request proxy into
its own loopback services (nginx/CGI, the flasher daemon, sa02m-devices-api,
mosquitto) or the cloud metadata address. Refused: loopback, link-local
(169.254.169.254 included), multicast, reserved and unspecified addresses;
the name `localhost` and the whole `.localhost` tree; a URL carrying
`user:pass@`; a scheme other than http/https; a URL longer than URL_MAX
(judged before any resolve).

**The LAN is reachable by design** (cloud-and-board decision 2026-09-09):
RFC1918 and IPv6 ULA targets are allowed, so an operator's scenario can call
their own NAS or panel and the two halves of the product refuse the same
set — a scenario the cloud accepts does not die silently here. The board
keeps one fence the cloud half does not have: the host is RESOLVED and the
answer judged, and every redirect hop is re-checked.

The operator allow-list `/etc/sa02m-rules/http-allow.json`
(`{"hosts": [...]}`, exact hostname or literal address, case-insensitive)
is now the way to PERMIT a refused target — a loopback or link-local host
named deliberately; it never excuses `user:pass@`. At most MAX_REDIRECTS
hops and MAX_RESPONSE_BYTES of a body are read. Limit stated honestly: the
check resolves the name once and urllib resolves it again to connect, so a
DNS answer that changes between the two (rebinding) is not defended.
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
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
#: RFC 6761: the name and the whole tree below it are loopback by definition.
_LOOPBACK_NAME = "localhost"


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


def _address_refusal(addr: ipaddress._BaseAddress) -> str:
    """Empty string when the address may be reached, else the refusal reason.

    Private (RFC1918 / ULA / CGNAT) is deliberately absent from the refused
    set — see the module docstring for the decision that put it there."""
    # Every IPv6 form that CARRIES an IPv4 address is judged on the address it
    # carries: the mapped form, 6to4 (`2002::/16`) and Teredo (`2001::/32`).
    # Python reports 6to4 as `is_private` and not `is_loopback`, so since the
    # LAN allowance of 1.0.6.41 `http://[2002:7f00:1::]` — 127.0.0.1 in a 6to4
    # wrapper — would otherwise be REACHED (found by the cloud session's
    # 55-URL sweep, 2026-09-09; before the allowance it was refused only by
    # accident, as "private").
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
        elif addr.teredo is not None:
            # (server, client) — the client is the host that would be reached.
            addr = addr.teredo[1]
    if addr.is_loopback:
        return "refused: loopback address"
    if addr.is_link_local:
        return "refused: link-local address"
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return "refused: reserved address"
    # "This network" (RFC 1122 §3.2.1.3) is `is_private`, so the LAN
    # allowance would otherwise cover it — and Linux resolves 0.0.0.0 to
    # the local host, which is exactly the target this guard refuses.
    if isinstance(addr, ipaddress.IPv4Address) and addr in _THIS_NETWORK:
        return "refused: reserved address"
    return ""


def _literal_address(host: str) -> Optional[ipaddress._BaseAddress]:
    """The host as an ADDRESS in any spelling a resolver would accept, or
    None when it is a real name.

    Beyond `ipaddress.ip_address` this parses the legacy inet_aton forms
    glibc still accepts and `ip_address` rejects as hostnames — `127.1`,
    `0177.0.0.1`, `2130706433`, `0x7f.1`. Without this they fall through to
    the resolver as names, and on the board that resolver answers 127.0.0.1;
    the static half (store validation, no DNS) would not see it at all."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values = []
    for part in parts:
        if part[:2].lower() == "0x":
            base, digits = 16, part[2:]
        elif len(part) > 1 and part[0] == "0":
            base, digits = 8, part[1:]
        else:
            base, digits = 10, part
        if not digits or not digits.isascii() or not digits.isalnum():
            return None
        try:
            values.append(int(digits, base))
        except ValueError:
            return None
    # inet_aton: the LAST part covers every byte the earlier ones left.
    tail_bits = 8 * (5 - len(parts))
    if any(v > 0xFF for v in values[:-1]) or values[-1] >= (1 << tail_bits):
        return None
    packed = values[-1]
    for shift, value in enumerate(reversed(values[:-1])):
        packed |= value << (tail_bits + 8 * shift)
    return ipaddress.ip_address(packed)


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
    # Credentials are refused whatever the target — an allow-listed host
    # does not excuse shipping a password into someone's access log.
    if parts.username is not None or parts.password is not None:
        return "refused: credentials in url"
    if allow is None:
        allow = allowed_hosts()
    if host in allow:
        return ""
    if host == _LOOPBACK_NAME or host.endswith("." + _LOOPBACK_NAME):
        return "refused: loopback name"
    port = port or (443 if parts.scheme == "https" else 80)
    literal = _literal_address(host)
    if literal is not None:
        return _address_refusal(literal)
    if not resolve:
        return ""
    try:
        addrs = list(_resolve(host, port))
    except (socket.gaierror, OSError, ValueError):
        return "refused: unresolvable host"
    if not addrs:
        return "refused: unresolvable host"
    for addr in addrs:
        reason = _address_refusal(addr)
        if reason:
            return reason
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

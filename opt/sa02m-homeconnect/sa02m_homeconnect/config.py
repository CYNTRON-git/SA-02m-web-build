"""The client conf: `/etc/sa02m-homeconnect/sa02m-homeconnect.conf` (INI, non-secret).

Writers: the CGI (www-data, through api.py) and the service catalogue's
Пуск/Стоп sync (root). The daemon only reads. Every value is allow-listed on
write AND on read: a hand-edited host URL, a malformed client id or a control
mode other than `off` never reaches the cloud client.

`client_id` is the integrator's own Home Connect application (Operator
decision Q-B). `vendor_client_id` is the preset slot for a future CYNTRON
application: used only while `client_id` is empty, never written by the web
card, carried over unchanged by every save — adding a vendor id is a conf
change, not a code change.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from typing import List, Optional

from . import constants as C
from .fsutil import atomic_write, group_gid

_CLIENT_ID_RE = re.compile(C.CLIENT_ID_RE)
ACCOUNT = "account"
CONTROL = "control"


def valid_client_id(value: object) -> bool:
    return isinstance(value, str) and bool(_CLIENT_ID_RE.match(value))


def valid_host(value: object) -> bool:
    return isinstance(value, str) and value in C.HOSTS


def _parse_ts(raw: str) -> int:
    raw = raw.strip()
    if raw.isdigit() and len(raw) <= 12:
        return int(raw)
    return 0


@dataclass
class ClientConfig:
    enabled: bool = False
    client_id: str = ""
    vendor_client_id: str = ""
    host: str = C.DEFAULT_HOST
    # Epoch seconds of the card's last «Подключить»; 0 = none pending.
    link_requested_at: int = 0
    control_mode: str = "off"
    # Values in the file that were refused and replaced by the default.
    warnings: List[str] = field(default_factory=list)
    # The file EXISTS but could not be read (EACCES: the daemon's read ACL is
    # gone) — never the same as disabled (main.py: `conf_unreadable`).
    unreadable: bool = False

    @property
    def effective_client_id(self) -> str:
        if self.client_id:
            return self.client_id
        return self.vendor_client_id

    @property
    def client_id_source(self) -> str:
        if self.client_id:
            return "own"
        if self.vendor_client_id:
            return "vendor"
        return ""

    @property
    def base_url(self) -> str:
        return C.HOSTS[self.host]


def load(path: Optional[str] = None) -> ClientConfig:
    """Read the conf. Absent file / section ⇒ defaults (disabled); a file
    that exists but cannot be opened or read ⇒ `unreadable` (configparser's
    own read() would skip it silently and report «disabled»)."""
    target = path or C.CONF_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return ClientConfig()
    except UnicodeDecodeError as exc:
        return ClientConfig(warnings=["unreadable conf: %s" % exc])
    except OSError as exc:
        return ClientConfig(unreadable=True,
                            warnings=["unreadable conf: %s" % (exc.strerror or exc)])
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read_string(text, source=target)
    except configparser.Error as exc:
        return ClientConfig(warnings=["unreadable conf: %s" % exc])
    out = ClientConfig()
    if cfg.has_section(ACCOUNT):
        raw_enabled = cfg.get(ACCOUNT, "enabled", fallback="false").strip().lower()
        out.enabled = raw_enabled in ("1", "true", "yes", "on")
        for key in ("client_id", "vendor_client_id"):
            raw = cfg.get(ACCOUNT, key, fallback="").strip()
            if raw == "":
                continue
            if valid_client_id(raw):
                setattr(out, key, raw)
            else:
                out.warnings.append("%s refused (not %s)" % (key, C.CLIENT_ID_RE))
        host = cfg.get(ACCOUNT, "host", fallback=C.DEFAULT_HOST).strip()
        if valid_host(host):
            out.host = host
        else:
            out.warnings.append("host %r refused, using %s" % (host[:40], C.DEFAULT_HOST))
        out.link_requested_at = _parse_ts(cfg.get(ACCOUNT, "link_requested_at", fallback="0"))
    if cfg.has_section(CONTROL):
        mode = cfg.get(CONTROL, "mode", fallback="off").strip()
        if mode not in C.CONTROL_MODES:
            # READ-ONLY release (Q-E): nothing but `off` exists.
            out.warnings.append("control mode %r refused, read-only release" % mode[:20])
    return out


def render(conf: ClientConfig) -> str:
    return (
        "# SA-02m Home Connect — written by the web card (docs/contracts/home-connect.md)\n"
        "[%s]\n"
        "enabled = %s\n"
        "client_id = %s\n"
        "vendor_client_id = %s\n"
        "host = %s\n"
        "link_requested_at = %d\n"
        "\n"
        "[%s]\n"
        "# read-only release: `off` is the only mode\n"
        "mode = off\n"
    ) % (
        ACCOUNT,
        "true" if conf.enabled else "false",
        conf.client_id,
        conf.vendor_client_id,
        conf.host,
        int(conf.link_requested_at),
        CONTROL,
    )


def save(conf: ClientConfig, path: Optional[str] = None) -> None:
    """Validate, then replace the conf atomically.

    An existing regular conf keeps its mode/owner; otherwise root:www-data
    0660 (the group from group_gid()) applies.
    """
    for key in ("client_id", "vendor_client_id"):
        value = getattr(conf, key)
        if value and not valid_client_id(value):
            raise ValueError("invalid %s" % key)
    if not valid_host(conf.host):
        raise ValueError("invalid host")
    if isinstance(conf.link_requested_at, bool) or not isinstance(conf.link_requested_at, int) \
            or conf.link_requested_at < 0:
        raise ValueError("invalid link_requested_at")
    atomic_write(path or C.CONF_FILE, render(conf), mode=0o660, gid=group_gid(),
                 preserve=True)

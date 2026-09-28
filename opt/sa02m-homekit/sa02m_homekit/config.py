"""The bridge conf: `/etc/sa02m-homekit/sa02m-homekit.conf` (INI, non-secret).

Writers: the CGI (www-data, through api.py) and the service catalogue's
Пуск/Стоп sync (root, etc/sa02m-web-service-ctl.sh). The daemon only reads.
Every value is allow-listed on write AND on read: a hand-edited `ppp0` or a
port another board service owns never reaches the listener (D3).
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from typing import List, Optional

from . import constants as C
from .fsutil import atomic_write, group_gid

_INTERFACE_RE = re.compile(C.INTERFACE_RE)
SECTION = "bridge"


def valid_interface(value: object) -> bool:
    return isinstance(value, str) and bool(_INTERFACE_RE.match(value))


def valid_port(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return C.PORT_MIN <= value <= C.PORT_MAX and value not in C.FORBIDDEN_PORTS


def parse_port(raw: object) -> Optional[int]:
    """An int port from JSON/INI input, or None when it is not a valid one."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        port = raw
    elif isinstance(raw, str) and raw.strip().isdigit() and len(raw.strip()) <= 5:
        port = int(raw.strip())
    else:
        return None
    return port if valid_port(port) else None


@dataclass
class BridgeConfig:
    enabled: bool = False
    interface: str = C.DEFAULT_INTERFACE
    port: int = C.DEFAULT_PORT
    # Values in the file that were refused and replaced by the default.
    warnings: List[str] = field(default_factory=list)
    # The file EXISTS but could not be read (EACCES: the daemon's read ACL is
    # gone) — never the same as disabled (main.py: `conf_unreadable`).
    unreadable: bool = False


def load(path: Optional[str] = None) -> BridgeConfig:
    """Read the conf. Absent file / section ⇒ defaults (disabled); a file
    that exists but cannot be opened or read ⇒ `unreadable` (configparser's
    own read() would skip it silently and report «disabled»)."""
    target = path or C.CONF_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return BridgeConfig()
    except UnicodeDecodeError as exc:
        return BridgeConfig(warnings=["unreadable conf: %s" % exc])
    except OSError as exc:
        return BridgeConfig(unreadable=True,
                            warnings=["unreadable conf: %s" % (exc.strerror or exc)])
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text, source=target)
    except configparser.Error as exc:
        return BridgeConfig(warnings=["unreadable conf: %s" % exc])
    out = BridgeConfig()
    if not cfg.has_section(SECTION):
        return out
    raw_enabled = cfg.get(SECTION, "enabled", fallback="false").strip().lower()
    out.enabled = raw_enabled in ("1", "true", "yes", "on")
    iface = cfg.get(SECTION, "interface", fallback=C.DEFAULT_INTERFACE).strip()
    if valid_interface(iface):
        out.interface = iface
    else:
        out.warnings.append("interface %r refused, using %s" % (iface, C.DEFAULT_INTERFACE))
    raw_port = cfg.get(SECTION, "port", fallback=str(C.DEFAULT_PORT))
    port = parse_port(raw_port)
    if port is not None:
        out.port = port
    else:
        out.warnings.append("port %r refused, using %d" % (raw_port, C.DEFAULT_PORT))
    return out


def render(conf: BridgeConfig) -> str:
    return (
        "# SA-02m Apple HomeKit bridge — written by the web card "
        "(docs/contracts/homekit-bridge.md)\n"
        "[%s]\n"
        "enabled = %s\n"
        "interface = %s\n"
        "port = %d\n"
    ) % (SECTION, "true" if conf.enabled else "false", conf.interface, conf.port)


def save(conf: BridgeConfig, path: Optional[str] = None) -> None:
    """Validate, then replace the conf atomically.

    An existing regular conf keeps its mode/owner; otherwise the §13 default
    (root:www-data 0660 — the group from group_gid()) applies.
    """
    if not valid_interface(conf.interface):
        raise ValueError("invalid interface")
    if not valid_port(conf.port):
        raise ValueError("invalid port")
    atomic_write(path or C.CONF_FILE, render(conf), mode=0o660, gid=group_gid(),
                 preserve=True)

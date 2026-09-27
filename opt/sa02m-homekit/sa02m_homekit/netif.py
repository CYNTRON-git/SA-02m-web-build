"""The chosen wired interface: its IPv4 address and MAC (stdlib only).

The listener binds to exactly one address — the IPv4 of the interface the
integrator picked (D3, docs/contracts/homekit-bridge.md §Сеть). No address
⇒ the caller reports `no_interface` and nothing listens. Used by the daemon
(bind + bridge name) and by the CGI dispatch (the card's interface picker),
so it imports nothing outside the standard library.
"""

from __future__ import annotations

import fcntl
import os
import re
import socket
import struct
from typing import Optional

from . import constants as C

_SIOCGIFADDR = 0x8915
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_INTERFACE_RE = re.compile(C.INTERFACE_RE)


class PortInUse(OSError):
    """The chosen listen port is taken — nothing listens (state port_in_use).
    Homed here (stdlib) so the daemon loop can name it without pyhap."""


def interface_present(name: str, sys_net: Optional[str] = None) -> bool:
    if not _INTERFACE_RE.match(name or ""):
        return False
    return os.path.isdir(os.path.join(sys_net or C.SYS_CLASS_NET, name))


def ipv4_address(name: str) -> Optional[str]:
    """The primary IPv4 of `name`, or None (absent, down, no address, or a
    name outside the allow-list — a modem is never asked)."""
    if not _INTERFACE_RE.match(name or ""):
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name.encode("ascii")[:15])
        res = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, packed)
    except OSError:
        return None
    finally:
        sock.close()
    addr = socket.inet_ntoa(res[20:24])
    if addr.startswith("0.") or addr.startswith("169.254."):
        # 0.0.0.0 / link-local: no usable LAN address yet (DHCP pending).
        return None
    return addr


def mac_address(name: str, sys_net: Optional[str] = None) -> Optional[str]:
    """`aa:bb:cc:dd:ee:ff` of `name` from sysfs, or None."""
    if not _INTERFACE_RE.match(name or ""):
        return None
    path = os.path.join(sys_net or C.SYS_CLASS_NET, name, "address")
    try:
        with open(path, encoding="ascii") as fh:
            mac = fh.read().strip().lower()
    except (OSError, UnicodeDecodeError):
        return None
    return mac if _MAC_RE.match(mac) else None


def bridge_name(mac: Optional[str]) -> str:
    """`SA-02m <6 hex>` from the last three MAC bytes — unique on the LAN,
    so two boards never collide on the mDNS name. No MAC ⇒ `SA-02m`."""
    if not mac or not _MAC_RE.match(mac):
        return "SA-02m"
    return "SA-02m " + mac.replace(":", "")[-6:].upper()

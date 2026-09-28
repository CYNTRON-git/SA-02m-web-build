"""Transport choice and validation for a bridge device — the ONE home.

`device_bus(cfg)` says how a device entry reaches its slave: the RS-485 serial
path (no `transport`, or `transport: rtu` — today's defaults, byte-identical)
or Modbus TCP (`transport: tcp`, `type: template | carel` only). An invalid TCP
entry raises BusConfigError(reason); it is never downgraded to RTU and never
connected. `validate_devices()` runs the same rules over a whole device list
plus the endpoint cap, so the bridge loader and mqtt_config.cgi refuse the
same entries with the same reason codes.

Why the loader, not only the CGI: /etc/sa02m-modbus-mqtt.yaml is
0660 root:www-data, so anything running as www-data can write it and bypass a
save-time check. The root bridge is the trust boundary.

PURE and stdlib-only on purpose: the CGI imports this module from its python
heredoc as www-data, where pyserial/paho may be absent. Never import serial,
paho, yaml or another bridge_* module here (pinned by
tests/test_bridge_bus.py). Grammar, reason codes and limits are documented in
docs/contracts/bridge-modbus-tcp.md.
"""

from __future__ import annotations

import ipaddress
import math
import re

TRANSPORT_RTU = "rtu"
TRANSPORT_TCP = "tcp"

TCP_CAPABLE_TYPES = ("template", "carel")
# Literal copy of sa02m_carel.controls.BOTH (that package is not importable
# from the CGI); pinned equal by tests/test_bridge_bus.py.
CAREL_FAMILIES = ("crst", "uaria")
TCP_ENDPOINT_MAX = 16
TCP_DEFAULT_PORT = 502
TCP_TIMEOUT_DEFAULT_S = 1.0
TCP_TIMEOUT_MIN_S = 0.2
TCP_TIMEOUT_MAX_S = 5.0

RTU_DEFAULT_PORT = "/dev/COM1"
RTU_DEFAULT_BAUD = 115200

REASONS = (
    "transport_unknown",
    "type_not_tcp_capable",
    "carel_family_required",
    "host_missing",
    "host_not_ipv4_literal",
    "host_forbidden",
    "tcp_port_invalid",
    "unit_invalid",
    "timeout_invalid",
    "serial_keys_on_tcp",
    "tcp_endpoint_limit",
)

# Strict dotted-decimal: four 0..255 octets, no leading zeros, ASCII digits
# only. `inet_aton` reads "0177.0.0.1" as 127.0.0.1 while a lenient parser may
# read it as 177.0.0.1 — a deny-list bypass; a leading zero is refused outright.
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
_IPV4_RE = re.compile(r"%s(?:\.%s){3}" % (_OCTET, _OCTET))

# Loopback reaches the board's own services (1883, 8765, 9999…); 0.0.0.0 is
# the board too; multicast/reserved (incl. 255.255.255.255) are not a device.
_FORBIDDEN_NETS = tuple(ipaddress.IPv4Network(n) for n in (
    "127.0.0.0/8", "0.0.0.0/32", "224.0.0.0/4", "240.0.0.0/4"))


class BusConfigError(ValueError):
    """A device entry names a transport it cannot have; `reason` is a REASONS code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason + (": " + detail if detail else ""))
        self.reason = reason


class BusSpec:
    """How one device reaches its slave. Equal specs share one bus (port cycle,
    writeback worker, and — for TCP — one socket)."""

    __slots__ = ("transport", "key", "label", "port", "baudrate",
                 "host", "tcp_port", "timeout_s")

    def __init__(self, transport, key, label, port=None, baudrate=0,
                 host=None, tcp_port=0, timeout_s=0.0):
        self.transport = transport
        self.key = key
        self.label = label
        self.port = port
        self.baudrate = baudrate
        self.host = host
        self.tcp_port = tcp_port
        self.timeout_s = timeout_s

    def _tuple(self):
        return tuple(getattr(self, s) for s in self.__slots__)

    def __eq__(self, other):
        return isinstance(other, BusSpec) and self._tuple() == other._tuple()

    def __hash__(self):
        return hash(self._tuple())

    def __repr__(self):
        return "BusSpec(%s)" % ", ".join(
            "%s=%r" % (s, getattr(self, s)) for s in self.__slots__)


def _transport(cfg: dict) -> str:
    t = cfg.get("transport")
    if t is None or t == TRANSPORT_RTU:
        return TRANSPORT_RTU
    if t == TRANSPORT_TCP:
        return TRANSPORT_TCP
    raise BusConfigError("transport_unknown", repr(t))


def _int_in(value, lo: int, hi: int):
    """int within lo..hi from an int or a digit string; None otherwise.
    A bool is not a number here (YAML `true` must not read as 1)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value, re.ASCII):
        n = int(value)
    else:
        return None
    return n if lo <= n <= hi else None


def _timeout(value):
    if value is None:
        return TCP_TIMEOUT_DEFAULT_S
    if isinstance(value, bool):
        return None
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(t) or not TCP_TIMEOUT_MIN_S <= t <= TCP_TIMEOUT_MAX_S:
        return None
    return t


def canonical_host(host) -> str:
    """The canonical dotted form of a strict IPv4 literal, or BusConfigError."""
    if host is None or host == "":
        raise BusConfigError("host_missing")
    if not isinstance(host, str) or not _IPV4_RE.fullmatch(host):
        raise BusConfigError("host_not_ipv4_literal", repr(host))
    ip = ipaddress.IPv4Address(host)
    if any(ip in net for net in _FORBIDDEN_NETS):
        raise BusConfigError("host_forbidden", str(ip))
    return str(ip)


def _tcp_bus(cfg: dict) -> BusSpec:
    dev_type = str(cfg.get("type", "")).lower()
    if dev_type not in TCP_CAPABLE_TYPES:
        raise BusConfigError("type_not_tcp_capable", dev_type)
    # A serial key on a TCP entry is contradictory, and other YAML readers
    # (e.g. the Alice inventory) would attribute the device to that COM port.
    if "port" in cfg or "baudrate" in cfg:
        raise BusConfigError("serial_keys_on_tcp")
    host = canonical_host(cfg.get("host"))
    tcp_port = _int_in(cfg.get("tcp_port", TCP_DEFAULT_PORT), 1, 65535)
    if tcp_port is None:
        raise BusConfigError("tcp_port_invalid", repr(cfg.get("tcp_port")))
    # Unit 0 is the Modbus broadcast id: a TCP→RTU gateway forwards it to
    # every slave and no reply ever comes.
    if _int_in(cfg.get("address", 1), 1, 255) is None:
        raise BusConfigError("unit_invalid", repr(cfg.get("address")))
    timeout_s = _timeout(cfg.get("tcp_timeout_s"))
    if timeout_s is None:
        raise BusConfigError("timeout_invalid", repr(cfg.get("tcp_timeout_s")))
    if dev_type == "carel":
        # FC17 identity over Carel Ethernet is unverified, and a wrong family
        # reads (and writes) the wrong register map.
        fam = str(cfg.get("family") or "").strip().lower()
        if fam not in CAREL_FAMILIES:
            raise BusConfigError("carel_family_required", repr(cfg.get("family")))
    label = "%s:%d" % (host, tcp_port)
    return BusSpec(TRANSPORT_TCP, "tcp:" + label, label, host=host,
                   tcp_port=tcp_port, timeout_s=timeout_s)


def device_bus(cfg: dict) -> BusSpec:
    """The bus of one device entry. RTU: today's defaults; TCP: validated."""
    if _transport(cfg) == TRANSPORT_TCP:
        return _tcp_bus(cfg)
    port = cfg.get("port", RTU_DEFAULT_PORT)
    baud = int(cfg.get("baudrate", RTU_DEFAULT_BAUD))
    return BusSpec(TRANSPORT_RTU, f"{port}:{baud}",
                   str(port).replace("/dev/", ""), port=port, baudrate=baud)


def validate_devices(devices) -> list:
    """[{"index", "id", "reason"}] for every entry the bridge would refuse.

    Only entries that carry a non-RTU `transport` are judged: an RTU-only list
    always validates clean, so no existing config gains a refusal. Endpoints
    past TCP_ENDPOINT_MAX distinct host:port are refused in list order.
    """
    out = []
    endpoints: set = set()
    for index, cfg in enumerate(devices or []):
        if not isinstance(cfg, dict):
            continue
        try:
            if _transport(cfg) != TRANSPORT_TCP:
                continue
            bus = _tcp_bus(cfg)
        except BusConfigError as e:
            out.append({"index": index, "id": cfg.get("id"), "reason": e.reason})
            continue
        if bus.key not in endpoints and len(endpoints) >= TCP_ENDPOINT_MAX:
            out.append({"index": index, "id": cfg.get("id"),
                        "reason": "tcp_endpoint_limit"})
            continue
        endpoints.add(bus.key)
    return out


def capabilities() -> dict:
    """What the web modal may offer (mqtt_config.cgi GET `capabilities`)."""
    return {
        "tcp_types": list(TCP_CAPABLE_TYPES),
        "carel_families": list(CAREL_FAMILIES),
        "tcp_default_port": TCP_DEFAULT_PORT,
    }

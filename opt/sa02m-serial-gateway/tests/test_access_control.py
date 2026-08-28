"""Network access control for the RS-485 gateway: bind address + IP allow-list.

WHY THIS SUITE EXISTS. Until 1.0.6.24 all three gateway modes bound a hardcoded
`0.0.0.0` and the config schema had no bind address, no IP allow-list and no key
of any kind: enabling a port in the panel put Modbus process control (asset S1)
on every interface with no authentication, and the operator had no device-side
way to narrow it (2026-08-28 audit, finding H3).

WHAT IS PINNED, and why each line matters:

  * THE DEFAULT DOES NOT MOVE. Absent or empty configuration must behave exactly
    as it did before this change — bind `0.0.0.0`, no filtering. A deployed SCADA
    client polling :502 must not lose its connection to an update (Operator
    decision D, 2026-08-28). This is the first block below because it is the one
    regression that would be a field outage, not a bug report.

  * THE FAILURE DIRECTION IS CLOSED, AND IT IS THE SAME IN BOTH DAEMONS. A
    malformed `allow_from` entry or `bind` value refuses the PORT — it never
    falls back to the open default and never silently drops the bad entry while
    keeping the good ones. An allow-list that fails open is worse than none: the
    operator believes access is narrowed and it is not. `sa02m-mqtt-opcua`
    carries the same rule (its own suite pins it there); the two packages deploy
    independently, so the code is not shared, the SEMANTICS are.

  * PARSING HAPPENS BEFORE THE PORT LOCK. A bad allow-list must not take the
    RS-485 line away from MPLC4/the MQTT bridge on its way to failing
    (`docs/agent-rules/sa02m-domain.md`, the port-lease invariant), and it must
    not take the other four ports down with it.

  * A REFUSED PEER IS REFUSED BEFORE ANY BYTE IS READ, in all three modes —
    including `transparent`, where the client is otherwise registered for the
    RS-485 fan-out before the read loop starts.

Run: python3 -m pytest opt/sa02m-serial-gateway/tests/test_access_control.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

# fcntl is Unix-only; same shim as test_exchange.py (the daemon runs on ARM Linux).
try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    import types

    _fcntl_stub = types.ModuleType('fcntl')
    _fcntl_stub.LOCK_EX = 2
    _fcntl_stub.LOCK_NB = 4
    _fcntl_stub.LOCK_UN = 8
    _fcntl_stub.flock = lambda *a, **k: None
    sys.modules['fcntl'] = _fcntl_stub

import serial_gateway as sg  # noqa: E402


# ── The default that must not move ────────────────────────────────────────────

def test_absent_allow_from_means_no_filtering():
    assert sg._parse_allow_from(None) is None
    assert sg._parse_allow_from({}.get('allow_from')) is None


def test_empty_allow_from_means_no_filtering_not_deny_all():
    # A present-but-empty list is what the web UI writes for "no restriction".
    # Reading it as "deny all" would black-hole every port the panel saves.
    assert sg._parse_allow_from([]) is None
    assert sg._parse_allow_from('') is None
    assert sg._parse_allow_from('   ') is None


def test_no_filtering_allows_every_peer():
    for host in ('10.0.0.1', '192.168.1.5', '::1', 'garbage', None):
        assert sg._peer_allowed(None, host) is True


def test_absent_bind_is_the_shipped_default():
    assert sg._parse_bind(None) == '0.0.0.0'
    assert sg._parse_bind('') == '0.0.0.0'


# ── Allow-list parsing ────────────────────────────────────────────────────────

def test_single_address_list_parses():
    nets = sg._parse_allow_from(['192.168.1.10'])
    assert sg._peer_allowed(nets, '192.168.1.10') is True
    assert sg._peer_allowed(nets, '192.168.1.11') is False


def test_cidr_membership():
    nets = sg._parse_allow_from(['192.168.1.0/24', '10.0.0.5'])
    assert sg._peer_allowed(nets, '192.168.1.200') is True
    assert sg._peer_allowed(nets, '10.0.0.5') is True
    assert sg._peer_allowed(nets, '10.0.0.6') is False


def test_comma_or_space_separated_string_parses():
    # The form a hand-edited YAML produces: `allow_from: 192.168.1.10, 10.0.0.0/8`
    nets = sg._parse_allow_from('192.168.1.10, 10.0.0.0/8')
    assert sg._peer_allowed(nets, '10.1.2.3') is True
    assert sg._peer_allowed(nets, '192.168.1.10') is True
    assert sg._peer_allowed(nets, '192.168.1.11') is False


def test_ipv6_rule_and_peer():
    nets = sg._parse_allow_from(['fd00::/8'])
    assert sg._peer_allowed(nets, 'fd00::1') is True
    assert sg._peer_allowed(nets, '2001:db8::1') is False


def test_ipv4_mapped_ipv6_peer_matches_an_ipv4_rule():
    # A dual-stack listener reports an IPv4 client as ::ffff:192.168.1.10.
    # Without normalisation the operator's 192.168.1.0/24 rule would refuse
    # exactly the client it was written for.
    nets = sg._parse_allow_from(['192.168.1.0/24'])
    assert sg._peer_allowed(nets, '::ffff:192.168.1.10') is True
    assert sg._peer_allowed(nets, '::ffff:10.0.0.1') is False


# ── Fail-closed: a malformed value refuses, never defaults ────────────────────

def test_malformed_entry_raises_and_names_the_value():
    with pytest.raises(sg.AccessConfigError) as exc:
        sg._parse_allow_from(['192.168.1.999'])
    assert '192.168.1.999' in str(exc.value)


def test_one_bad_entry_refuses_the_whole_list():
    # Never "keep the good ones": a half-applied allow-list is an allow-list the
    # operator cannot reason about.
    with pytest.raises(sg.AccessConfigError):
        sg._parse_allow_from(['192.168.1.10', 'not-an-ip'])


def test_hostname_is_refused_not_resolved():
    with pytest.raises(sg.AccessConfigError):
        sg._parse_allow_from(['scada.example.com'])


def test_non_string_entries_are_refused():
    for bad in ([42], [None], [{'ip': '1.2.3.4'}], {'a': 'b'}, 42):
        with pytest.raises(sg.AccessConfigError):
            sg._parse_allow_from(bad)


def test_malformed_bind_refuses_and_does_not_fall_back_to_open():
    for bad in ('0.0.0.0.0', 'localhost', '192.168.1.256', 5):
        with pytest.raises(sg.AccessConfigError):
            sg._parse_bind(bad)


def test_valid_bind_values_pass_through():
    assert sg._parse_bind('127.0.0.1') == '127.0.0.1'
    assert sg._parse_bind('192.168.1.5') == '192.168.1.5'
    assert sg._parse_bind('::1') == '::1'


def test_unparseable_peer_is_refused_when_filtering_is_on():
    nets = sg._parse_allow_from(['192.168.1.0/24'])
    for host in (None, '', 'garbage'):
        assert sg._peer_allowed(nets, host) is False


# ── The bind address actually reaches the listener ────────────────────────────

class _FakeServer:
    def close(self):
        pass

    async def wait_closed(self):
        pass


def _drop_task(coro, **kw):
    """Stand in for asyncio.create_task; closes the coroutine so the RS-485
    read loop is not left un-awaited (transparent mode starts one)."""
    coro.close()
    return None


def _record_start_server(monkeypatch):
    calls = []

    async def fake_start_server(handler, host, port, **kw):
        calls.append((host, port))
        return _FakeServer()

    monkeypatch.setattr(sg.asyncio, 'start_server', fake_start_server)
    return calls


def _make(cls, cfg):
    return cls('COM1', cfg, worker=None, stats=sg.PortStats(),
               executor=None)


@pytest.mark.parametrize('cls,default_port', [
    (sg.ModbusTcpGateway, 502),
    (sg.RtuOverTcpGateway, 8502),
    (sg.TransparentGateway, 9502),
])
def test_bind_defaults_to_all_interfaces(monkeypatch, cls, default_port):
    calls = _record_start_server(monkeypatch)
    monkeypatch.setattr(sg.asyncio, 'create_task', _drop_task)
    asyncio.run(_make(cls, {}).start())
    assert calls == [('0.0.0.0', default_port)]


@pytest.mark.parametrize('cls', [
    sg.ModbusTcpGateway, sg.RtuOverTcpGateway, sg.TransparentGateway,
])
def test_configured_bind_reaches_the_listener(monkeypatch, cls):
    calls = _record_start_server(monkeypatch)
    monkeypatch.setattr(sg.asyncio, 'create_task', _drop_task)
    asyncio.run(_make(cls, {'bind': '127.0.0.1', 'tcp_port': 1502}).start())
    assert calls == [('127.0.0.1', 1502)]


# ── A refused peer never reaches the bus ──────────────────────────────────────

class _FakeWriter:
    def __init__(self, peer):
        self._peer = peer
        self.closed = False

    def get_extra_info(self, key):
        return self._peer if key == 'peername' else None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def write(self, data):
        raise AssertionError('a refused peer must never be written to')


class _ExplodingReader:
    """Any read attempt means the connection was NOT refused."""

    def __init__(self):
        self.read_attempted = False

    async def readexactly(self, n):
        self.read_attempted = True
        raise asyncio.IncompleteReadError(b'', n)

    async def read(self, n):
        self.read_attempted = True
        return b''


@pytest.mark.parametrize('cls', [
    sg.ModbusTcpGateway, sg.RtuOverTcpGateway, sg.TransparentGateway,
])
def test_peer_outside_the_allow_list_is_refused_before_any_read(cls):
    gw = _make(cls, {'allow_from': ['192.168.1.0/24']})
    reader, writer = _ExplodingReader(), _FakeWriter(('10.0.0.9', 51000))
    asyncio.run(gw._handle_client(reader, writer))
    assert writer.closed is True
    assert reader.read_attempted is False
    assert gw._stats.tcp_clients == 0
    assert gw._stats.refused == 1


def test_transparent_refusal_never_registers_the_client_for_fanout():
    # Transparent mode fans RS-485 RX out to every registered client, so a
    # refused peer that got registered would still RECEIVE bus traffic even
    # though it can send none. Asserting the set is empty after the handler
    # returns would be vacuous (the `finally` discards it either way) — this
    # peeks at the registry from inside the read loop.
    gw = _make(sg.TransparentGateway, {'allow_from': ['192.168.1.0/24']})
    seen = []

    class _PeekReader(_ExplodingReader):
        async def read(self, n):
            seen.append(set(gw._clients))
            return await super().read(n)

    asyncio.run(gw._handle_client(_PeekReader(), _FakeWriter(('192.168.1.7', 1))))
    assert len(seen) == 1 and len(seen[0]) == 1, 'an allowed peer must be fanned out to'

    seen.clear()
    asyncio.run(gw._handle_client(_PeekReader(), _FakeWriter(('10.0.0.9', 1))))
    assert seen == [], 'a refused peer must never enter the fan-out registry'


@pytest.mark.parametrize('cls', [
    sg.ModbusTcpGateway, sg.RtuOverTcpGateway, sg.TransparentGateway,
])
def test_allow_listed_peer_is_served(cls):
    gw = _make(cls, {'allow_from': ['192.168.1.0/24']})
    reader, writer = _ExplodingReader(), _FakeWriter(('192.168.1.7', 51000))
    asyncio.run(gw._handle_client(reader, writer))
    assert reader.read_attempted is True
    assert gw._stats.refused == 0


@pytest.mark.parametrize('cls', [
    sg.ModbusTcpGateway, sg.RtuOverTcpGateway, sg.TransparentGateway,
])
def test_without_an_allow_list_every_peer_is_served(cls):
    gw = _make(cls, {})
    reader, writer = _ExplodingReader(), _FakeWriter(('203.0.113.9', 51000))
    asyncio.run(gw._handle_client(reader, writer))
    assert reader.read_attempted is True
    assert gw._stats.refused == 0


# ── Port-level fail-closed ────────────────────────────────────────────────────

def test_bad_access_config_refuses_the_port_before_taking_the_rs485_lock():
    taken = []

    class _SpyLock(sg.PortLock):
        def acquire(self):
            taken.append(1)
            return True

    pg = sg.PortGateway('COM1', {'enabled': True, 'mode': 'modbus_tcp',
                                 'allow_from': ['nonsense']}, executor=None)
    pg._lock = _SpyLock('COM1')
    assert asyncio.run(pg.start()) is False
    assert taken == [], 'the port lock must not be taken to fail a config parse'
    assert 'allow_from' in pg.stats.last_error


def test_bad_bind_refuses_the_port():
    pg = sg.PortGateway('COM1', {'enabled': True, 'mode': 'modbus_tcp',
                                 'bind': 'localhost'}, executor=None)
    assert asyncio.run(pg.start()) is False
    assert 'bind' in pg.stats.last_error


def test_refused_counter_is_published_in_the_status_payload():
    # The panel and any SCADA reader see the restriction working; without this
    # a refused connection is visible only in the journal.
    assert 'refused' in sg.PortStats().to_dict()

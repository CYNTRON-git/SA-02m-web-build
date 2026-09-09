#!/usr/bin/env python3
"""The telemetry daemon takes the shared-bus lock the web CGI already takes.

The defect this pins (1.0.6.42 item 6). The PCA9536's output port is ONE byte
with four owners -- this daemon, `www/network_config/cgi-bin/lib_hw.sh`,
`etc/sa02m-beeper-override.sh` and MPLC4/KLogic. The CGI serialises its access
with an flock on SA02M_I2C_LOCK_FILE and refuses outright while an owner unit
or process holds the bus; the daemon did neither. Driving a channel is a
read-modify-write, so an interleaving owner does not merely clobber a bit, it
loses a whole command -- and one of the bits is the discrete output that
commutes real equipment. On bench 1.135 that output carries bench 1.136's
power.

What is asserted here, and why each case exists:

  * the lock brackets the WHOLE read-modify-write, not just the write --
    asserted as a timeline (lock, read, write, unlock), because a lock taken
    around the write alone would pass a "the lock was taken" assertion while
    leaving the race exactly where it was;
  * a second holder is never overridden: the write is refused, no byte reaches
    the expander, and the refusal names the lock file;
  * the wait is BOUNDED and it is a real wait -- a holder that lets go inside
    the window is waited out, one that does not is refused inside it;
  * the owner gate (SA02M_I2C_OWNER_UNITS / _PROCS, SA02M_I2C_RESPECT_OWNER) is
    read from the same conf and honoured, and an owner probe that gives no
    answer refuses rather than assuming the bus is free;
  * the READ path takes the lock but is deliberately NOT owner-gated -- the one
    place this daemon does not mirror the CGI, recorded in
    PCA9536Control.read_channels and pinned here so it stays a decision;
  * the fallback constants still match lib_hw.sh, which is the reference.

SAFETY -- this suite must never reach a real I2C bus. Same two independent
guards as tests/test_telemetry_hw_map.py: `_i2cget`/`_i2cset` are replaced by a
fake register file, AND `subprocess.run` is replaced by a raiser, so any path
that bypassed the shims fails loudly instead of reaching `i2cset`. The owner
probes are shimmed for the same reason (they spawn `systemctl`/`pgrep`), with
the raiser left standing behind them.

PORTABILITY -- `fcntl` does not exist on a Windows dev host, and a suite that
silently tests nothing there is the hollow-gate shape this project keeps
finding (docs/agent-rules/quality-gate-rigor.md). So the POLICY is driven
through an injected flock whose semantics are the ones the daemon depends on,
on every host, and the OS primitive itself is covered by the skipUnless case in
TestARealSecondHolder -- authoritative where it runs (CI, the board), reported
as a skip where it does not.
"""
from __future__ import annotations

import os
import sys
import time
import types
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BRIDGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_DIR))

try:
    import paho.mqtt.client  # noqa: F401
except ImportError:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_client = types.ModuleType("paho.mqtt.client")
    _paho_client.Client = object
    _paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    _paho.mqtt = _paho_mqtt
    _paho_mqtt.client = _paho_client
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_client

import sa02m_telemetry as tel  # noqa: E402

REG_OUT = 0x01
REG_DIR = 0x03
ALL_OFF = 0x0F          # active-low, every output off (pre-start's PCA9536_OFF)
BIT_DO = 1              # the shipped map; TestShippedConfIsTheOneHome in
BIT_BEEPER = 2          # test_telemetry_hw_map.py pins it against etc/
BIT_ALARM_LED = 0

CONF_TEMPLATE = """\
SA02M_HW_BACKEND=i2c_expander
SA02M_I2C_EXP_BUS=2
SA02M_I2C_EXP_ADDR=0x41
SA02M_I2C_ACTIVE_LOW_MASK=auto
SA02M_I2C_EXTRA_OUTPUT_MASK=0x08
SA02M_I2C_BIT_DO=1
SA02M_I2C_BIT_BEEPER=2
SA02M_I2C_BIT_ALARM_LED=0
SA02M_I2C_BIT_USB_POWER=
SA02M_I2C_LOCK_FILE={lock_file}
SA02M_I2C_LOCK_WAIT_SEC={lock_wait}
"""


class _Msg:
    def __init__(self, topic: str, payload: bytes = b"1"):
        self.topic = topic
        self.payload = payload
        self.retain = False


class FakeFlock:
    """Enough of `fcntl` for the daemon's bounded wait, on any host.

    Holder identity is the fd, exactly as flock(2) keys on the open file
    description -- which is also why the real-primitive case below can contend
    with itself from a second fd in one process.
    """

    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self, timeline: list):
        self.timeline = timeline
        self.holder = None          # an fd, or the sentinel "other"
        self.release_after = None   # attempts after which "other" lets go
        self.attempts = 0

    # -- test-side controls -------------------------------------------------
    def hold_elsewhere(self, release_after=None):
        self.holder = "other"
        self.release_after = release_after
        self.attempts = 0

    @property
    def held(self) -> bool:
        return self.holder is not None

    # -- the fcntl surface the daemon uses ----------------------------------
    def flock(self, fd, op):
        if op & self.LOCK_UN:
            if self.holder == fd:
                self.holder = None
                self.timeline.append("unlock")
            return
        if self.holder is not None and self.holder != fd:
            self.attempts += 1
            if self.release_after is None or self.attempts < self.release_after:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            self.holder = None
        self.holder = fd
        self.timeline.append("lock")


class FakeExpander:
    """The two PCA9536 registers, and every access recorded on the timeline."""

    def __init__(self, timeline: list, out: int = ALL_OFF, readable: bool = True):
        self.timeline = timeline
        self.regs = {REG_OUT: out, REG_DIR: 0xFF}
        self.writes: list[tuple[int, int, int, int]] = []
        self.reads: list[tuple[int, int, int]] = []
        self.readable = readable

    def get(self, bus: int, addr: int, reg: int):
        self.reads.append((bus, addr, reg))
        self.timeline.append("read")
        if not self.readable:
            return None
        return self.regs.get(reg)

    def set(self, bus: int, addr: int, reg: int, value: int) -> bool:
        self.writes.append((bus, addr, reg, value))
        self.timeline.append("write" if reg == REG_OUT else "write-dir")
        self.regs[reg] = value & 0xFF
        return True

    @property
    def out_writes(self) -> list[int]:
        return [v for (_b, _a, r, v) in self.writes if r == REG_OUT]


class LockTestCase(unittest.TestCase):
    """A fake expander, a temp conf and lock file, and the no-real-bus guards.

    The owner probes default to "no tool, no owner", so a case that does not
    say otherwise runs on a free bus -- and, importantly, runs the same way on
    a CI box that really has systemctl as on one that does not.
    """

    use_fake_flock = True

    def setUp(self):
        self.timeline: list[str] = []
        self.exp = FakeExpander(self.timeline)
        self.flock = FakeFlock(self.timeline)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conf_path = Path(self._tmp.name) / "sa02m_hw.conf"
        self.lock_path = Path(self._tmp.name) / "pca9536.lock"

        self.probe_calls: list[list[str]] = []
        self.available: set[str] = set()
        self.probe_rc: dict[str, int | None] = {}

        def fake_probe(argv):
            self.probe_calls.append(list(argv))
            return self.probe_rc.get(" ".join(argv), 1)

        patches = [
            mock.patch.object(tel, "_i2cget", self.exp.get),
            mock.patch.object(tel, "_i2cset", self.exp.set),
            mock.patch.object(tel, "_run_probe", fake_probe),
            mock.patch.object(tel, "_probe_available",
                              lambda tool: tool in self.available),
            # The second, independent guard: nothing here may spawn a process.
            # A path that bypassed the shims above would reach the real i2cset
            # -- on bench 1.135 that cuts bench 1.136's power.
            mock.patch.object(
                tel.subprocess, "run",
                mock.Mock(side_effect=AssertionError(
                    "a test tried to run a real subprocess")),
            ),
        ]
        if self.use_fake_flock:
            patches.append(mock.patch.object(tel, "fcntl", self.flock))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write_conf(self, *, lock_file=None, lock_wait="1", extra="") -> Path:
        self.conf_path.write_text(
            CONF_TEMPLATE.format(
                lock_file=lock_file if lock_file is not None else self.lock_path,
                lock_wait=lock_wait,
            ) + extra,
            encoding="utf-8",
        )
        os.environ["SA02M_HW_CONF"] = str(self.conf_path)
        self.addCleanup(os.environ.pop, "SA02M_HW_CONF", None)
        return self.conf_path

    def owner_unit_active(self, unit="mplc4.service"):
        self.available.add("systemctl")
        self.probe_rc[f"systemctl is-active --quiet {unit}"] = 0

    def owner_proc_active(self, proc="mplc4"):
        self.available.add("pgrep")
        self.probe_rc[f"pgrep -x {proc}"] = 0

    def make_client(self):
        published: list[tuple[str, str]] = []
        return types.SimpleNamespace(
            _hw=None,
            _device_id="SA-02m",
            published=published,
            _pub=lambda suffix, value, retain=True: published.append(
                (suffix, value)),
        )

    def ready_client(self, **conf):
        """A client whose hardware is initialised and whose timeline is clean."""
        self.write_conf(**conf)
        stub = self.make_client()
        tel.TelemetryClient.init_hw(stub)
        self.assertIsNotNone(stub._hw, "hardware control was disabled at init")
        self.timeline.clear()
        self.exp.writes.clear()
        self.exp.reads.clear()
        self.probe_calls.clear()
        return stub

    def send(self, stub, ctrl: str, payload: bytes = b"1"):
        cb = tel.TelemetryClient._make_hw_cb(stub, ctrl)
        cb(None, None, _Msg(f"/devices/SA-02m/controls/{ctrl}/on", payload))


class TestTheLockBracketsTheWholeReadModifyWrite(LockTestCase):
    """Not "a lock was taken" -- WHICH operations it covers."""

    def test_read_and_write_both_happen_under_one_held_lock(self):
        """RED before the fix: the timeline was ('read', 'write') with no lock
        at all. A fix that locked only the write would give
        ('read', 'lock', 'write', 'unlock') -- still racing, and still passing
        a naive assertion -- so the whole sequence is pinned."""
        stub = self.ready_client()

        self.send(stub, "do", b"1")

        self.assertEqual(
            self.timeline, ["lock", "read", "write", "unlock"],
            "the read-modify-write must run inside ONE held lock: the read and "
            "the write of the same byte cannot be separable by another owner",
        )

    def test_the_lock_is_released_after_the_write(self):
        """A lock never released would wedge the CGI and the beeper worker on
        the next command instead of racing them."""
        stub = self.ready_client()
        self.send(stub, "do", b"1")
        self.assertFalse(self.flock.held, "the bus lock was left held")

    def test_a_second_command_takes_the_lock_again(self):
        stub = self.ready_client()
        self.send(stub, "do", b"1")
        self.send(stub, "do", b"0")
        self.assertEqual(self.timeline.count("lock"), 2)
        self.assertEqual(self.timeline.count("unlock"), 2)

    def test_the_configured_lock_file_is_the_one_opened(self):
        """The CGI's lock only excludes us if it is the SAME file. Reading the
        path from the conf is what makes that true on a board that moved it."""
        moved = Path(self._tmp.name) / "moved.lock"
        stub = self.ready_client(lock_file=moved)
        self.send(stub, "do", b"1")
        self.assertTrue(moved.exists(),
                        "the daemon did not open the configured lock file")
        self.assertFalse(self.lock_path.exists())

    def test_the_direction_register_write_is_locked_too(self):
        """init() writes register 3. It is a write on the shared bus like any
        other, and it used to go out with nothing held."""
        self.write_conf()
        stub = self.make_client()
        tel.TelemetryClient.init_hw(stub)
        self.assertEqual(self.timeline, ["lock", "write-dir", "unlock"])


class TestASecondHolderIsNotOverridden(LockTestCase):
    """Refuse, never write on a bus another owner holds."""

    def test_a_held_lock_refuses_the_write_and_nothing_reaches_the_bus(self):
        """RED before the fix: the byte went out regardless of who held the
        lock."""
        stub = self.ready_client()
        self.flock.hold_elsewhere()

        # The bus assertions sit INSIDE the assertLogs block on purpose: they
        # must be what fails first. A refusal that also forgot to log would
        # otherwise report itself as "no WARNING" and bury the fact that a byte
        # went out on a bus somebody else held.
        with self.assertLogs(tel.log, level="WARNING") as caught:
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [],
                             "a write reached the expander while another owner "
                             "held the bus lock")
            self.assertEqual(self.exp.reads, [], "even the read went out")
        self.assertTrue(
            any(str(self.lock_path) in line for line in caught.output),
            f"the refusal must name the lock file it could not take: "
            f"{caught.output}")

    def test_a_refused_write_publishes_no_state(self):
        """Publishing the commanded value on a refused write would report the
        board into a state it is not in -- the retained-value defect that made
        the app show the opposite of the hardware."""
        stub = self.ready_client()
        self.flock.hold_elsewhere()

        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "do", b"1")

        self.assertEqual(stub.published, [])

    def test_a_holder_that_lets_go_inside_the_window_is_waited_out(self):
        """The wait is a real wait, not a bare LOCK_NB: the CGI's own writes
        are short, and refusing a command because someone was mid-byte would
        make the smart-home switch flaky for no reason."""
        stub = self.ready_client(lock_wait="1")
        self.flock.hold_elsewhere(release_after=3)

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << BIT_DO)])
        self.assertGreaterEqual(self.flock.attempts, 3,
                                "the daemon did not retry the lock at all")

    def test_the_wait_is_bounded_by_the_configured_value(self):
        """Unbounded would be worse than refusing: these callbacks run on
        paho's single network thread, so a command parked on the lock parks the
        MQTT keepalive with it."""
        stub = self.ready_client(lock_wait="0.2")
        self.flock.hold_elsewhere()

        started = time.monotonic()
        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [])
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0,
                        "the wait was not bounded by SA02M_I2C_LOCK_WAIT_SEC")

    def test_an_unparseable_wait_refuses_rather_than_guessing(self):
        """`flock -w banana` fails in lib_hw.sh too, and the CGI refuses. A
        wait we cannot read is not one to invent on a shared bus."""
        stub = self.ready_client(lock_wait="banana")

        with self.assertLogs(tel.log, level="WARNING") as caught:
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [])
        self.assertTrue(
            any("SA02M_I2C_LOCK_WAIT_SEC" in line for line in caught.output),
            f"the refusal must name the key it could not read: {caught.output}")


@unittest.skipUnless(tel.fcntl is not None, "no fcntl on this host (Windows)")
class TestARealSecondHolder(LockTestCase):
    """The OS primitive, not our model of it -- authoritative where it runs.

    flock(2) keys on the open file description, so a second `open()` of the
    same path in this same process contends exactly as the CGI's shell would.
    """

    use_fake_flock = False

    def test_a_real_flock_held_on_the_file_refuses_the_daemon(self):
        stub = self.ready_client(lock_wait="0.2")
        holder = os.open(str(self.lock_path), os.O_RDWR)
        try:
            tel.fcntl.flock(holder, tel.fcntl.LOCK_EX | tel.fcntl.LOCK_NB)
            with self.assertLogs(tel.log, level="WARNING"):
                self.send(stub, "do", b"1")
        finally:
            tel.fcntl.flock(holder, tel.fcntl.LOCK_UN)
            os.close(holder)

        self.assertEqual(self.exp.writes, [],
                         "a real second holder did not stop the write")

    def test_the_daemon_writes_once_the_real_holder_lets_go(self):
        """Non-vacuity for the case above: the refusal must come from the lock,
        not from the fixture being broken."""
        stub = self.ready_client(lock_wait="0.2")
        holder = os.open(str(self.lock_path), os.O_RDWR)
        try:
            tel.fcntl.flock(holder, tel.fcntl.LOCK_EX | tel.fcntl.LOCK_NB)
            tel.fcntl.flock(holder, tel.fcntl.LOCK_UN)
        finally:
            os.close(holder)

        self.send(stub, "do", b"1")
        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << BIT_DO)])


class TestTheOwnerGate(LockTestCase):
    """MPLC4/KLogic hold the bus without taking our lock -- so we ask."""

    def test_an_active_owner_unit_refuses_the_write(self):
        """RED before the fix: SA02M_I2C_OWNER_UNITS was not read at all."""
        stub = self.ready_client()
        self.owner_unit_active("mplc4.service")

        with self.assertLogs(tel.log, level="WARNING") as caught:
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [])
        self.assertTrue(any("mplc4.service" in line for line in caught.output),
                        f"the refusal must name the owner: {caught.output}")

    def test_an_active_owner_process_refuses_the_write(self):
        stub = self.ready_client()
        self.owner_proc_active("klogic")

        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [])

    def test_the_gate_is_not_reached_when_the_lock_would_have_been_free(self):
        """Non-vacuity: the same fixture with no owner active must write."""
        stub = self.ready_client()
        self.available.update({"systemctl", "pgrep"})

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << BIT_DO)])

    def test_a_non_service_entry_is_ignored_as_in_the_cgi(self):
        """lib_hw.sh skips any owner-unit name that is not `*.service`; a
        divergence here would refuse where the CGI writes."""
        stub = self.ready_client(
            extra='SA02M_I2C_OWNER_UNITS="mplc4 something.timer"\n')
        self.available.add("systemctl")
        self.probe_rc["systemctl is-active --quiet mplc4"] = 0

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << BIT_DO)])
        self.assertEqual(self.probe_calls, [],
                         "a non-.service owner entry was probed anyway")

    def test_respect_owner_zero_switches_the_gate_off(self):
        """The documented escape hatch, honoured identically by both consumers."""
        stub = self.ready_client(extra="SA02M_I2C_RESPECT_OWNER=0\n")
        self.owner_unit_active("mplc4.service")

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << BIT_DO)])
        self.assertEqual(self.probe_calls, [],
                         "the gate was switched off but still probed")

    def test_a_probe_that_gives_no_answer_refuses(self):
        """Fail closed. A wedged systemctl must not read as "the bus is free" --
        the shell would simply block there, which a paho callback thread cannot
        afford, so the daemon refuses instead."""
        stub = self.ready_client()
        self.available.add("systemctl")
        self.probe_rc["systemctl is-active --quiet mplc.service"] = None

        with self.assertLogs(tel.log, level="WARNING") as caught:
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [])
        self.assertTrue(any("no answer" in line for line in caught.output),
                        f"the refusal must say the probe went unanswered: "
                        f"{caught.output}")

    def test_a_blank_owner_list_keeps_the_shipped_owners(self):
        """`_read_conf_value` cannot tell an absent key from an empty one, so
        blank resolves to the defaults -- the fail-safe direction. Reading it
        as "no owners" would let the daemon write on a bus MPLC4 holds."""
        stub = self.ready_client(extra='SA02M_I2C_OWNER_UNITS=""\n')
        self.owner_unit_active("mplc.service")

        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [])

    def test_a_conf_named_owner_the_defaults_do_not_carry_is_honoured(self):
        """The list is READ, not assumed: the property a hard-coded copy of the
        default set could not have."""
        stub = self.ready_client(
            extra='SA02M_I2C_OWNER_PROCS="site-plc"\n')
        self.owner_proc_active("site-plc")

        with self.assertLogs(tel.log, level="WARNING") as caught:
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [])
        self.assertTrue(any("site-plc" in line for line in caught.output))


class TestTheReadPath(LockTestCase):
    """Locked, single-shot, and deliberately not owner-gated."""

    def test_the_publish_read_is_taken_under_the_lock(self):
        """A read racing a foreign read-modify-write can observe a byte no
        instant of the port ever had, and MQTT retains what we publish."""
        stub = self.ready_client()

        tel.TelemetryClient._publish_metrics(stub)

        self.assertEqual(self.timeline, ["lock", "read", "unlock"])

    def test_one_port_read_serves_every_channel(self):
        """Three separate reads could straddle another owner's write and
        publish three channels from three different states of one byte."""
        stub = self.ready_client()

        tel.TelemetryClient._publish_metrics(stub)

        self.assertEqual(len(self.exp.reads), 1)
        states = dict(stub.published)
        for ctrl in ("do", "beeper", "alarm_led"):
            self.assertEqual(states.get(f"controls/{ctrl}"), "0")

    def test_a_locked_out_read_publishes_nothing_and_says_so(self):
        """Silence beats a fabricated reading; a silent skip beats neither."""
        stub = self.ready_client(lock_wait="0.2")
        self.flock.hold_elsewhere()

        with self.assertLogs(tel.log, level="WARNING") as caught:
            tel.TelemetryClient._publish_metrics(stub)
            states = dict(stub.published)
            for ctrl in ("do", "beeper", "alarm_led"):
                self.assertNotIn(f"controls/{ctrl}", states)
        self.assertTrue(any("not published" in line for line in caught.output),
                        f"a skipped poll left no trace: {caught.output}")

    def test_an_owner_holding_the_bus_stops_writes_but_not_reads(self):
        """The recorded divergence from lib_hw.sh (PCA9536Control.read_channels
        carries the reasoning). The CGI refuses an owner-active READ so the
        panel can draw its busy badge; this daemon has no badge, and mirroring
        it would silence do/beeper/alarm_led telemetry on every board running
        MPLC4 -- exactly when the PLC is driving those pins."""
        stub = self.ready_client()
        self.owner_unit_active("mplc4.service")

        tel.TelemetryClient._publish_metrics(stub)

        states = dict(stub.published)
        self.assertEqual(states.get("controls/do"), "0",
                         "state stopped being published while MPLC4 runs")

        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "do", b"1")
            self.assertEqual(self.exp.writes, [],
                             "a write went out on a bus MPLC4 holds")

    def test_the_poll_path_spawns_no_owner_probe(self):
        """The gate costs up to nine forks; on the 30 s poll of a shared ARM
        target that is a cost the read path does not pay."""
        stub = self.ready_client()
        self.available.update({"systemctl", "pgrep"})

        tel.TelemetryClient._publish_metrics(stub)

        self.assertEqual(self.probe_calls, [])


class TestDeferredDirectionRegister(LockTestCase):
    """A busy bus at startup must not disable hardware control for good."""

    def test_a_busy_bus_at_init_leaves_control_enabled(self):
        """init_hw() runs ONCE per process and MPLC4 is usually up before this
        service. Disabling on a busy bus would mean the board never accepts a
        command again, however long the bus stays free afterwards."""
        self.write_conf()
        stub = self.make_client()
        self.owner_unit_active("mplc4.service")

        with self.assertLogs(tel.log, level="WARNING"):
            tel.TelemetryClient.init_hw(stub)

        self.assertIsNotNone(stub._hw, "a busy bus disabled hardware control")

    def test_the_deferred_direction_write_lands_with_the_first_command(self):
        self.write_conf()
        stub = self.make_client()
        self.owner_unit_active("mplc4.service")
        with self.assertLogs(tel.log, level="WARNING"):
            tel.TelemetryClient.init_hw(stub)
        self.assertEqual(self.exp.writes, [])

        self.probe_rc.clear()           # the owner let go
        self.timeline.clear()
        self.send(stub, "do", b"1")

        self.assertEqual(self.timeline,
                         ["lock", "write-dir", "read", "write", "unlock"])

    def test_a_real_io_failure_at_init_still_disables_control(self):
        """The pre-existing behaviour, unchanged: an expander that will not
        answer is not a busy bus."""
        self.write_conf()
        self.exp.set = lambda *a, **k: False
        with mock.patch.object(tel, "_i2cset", self.exp.set):
            stub = self.make_client()
            with self.assertLogs(tel.log, level="WARNING"):
                tel.TelemetryClient.init_hw(stub)
        self.assertIsNone(stub._hw)


class TestTheFallbacksMatchTheCgi(unittest.TestCase):
    """The drift alarm for the constants a Python daemon cannot source.

    lib_hw.sh is the reference consumer and 1.0.6.42 deliberately does not
    touch it. Its `:-` defaults are duplicated in sa02m_telemetry.py because no
    Python process can source a shell file -- so they are pinned here, both
    against lib_hw.sh and against the shipped conf. Non-vacuous: a pattern that
    stops matching lib_hw.sh FAILS rather than passing on zero matches.
    """

    REPO = Path(__file__).resolve().parents[3]
    LIB_HW = "www/network_config/cgi-bin/lib_hw.sh"

    def setUp(self):
        self.lib = self.REPO / self.LIB_HW
        self.assertTrue(self.lib.is_file(), f"{self.lib} is missing")
        self.text = self.lib.read_text(encoding="utf-8", errors="replace")

    def _default(self, key: str) -> str:
        import re
        m = re.search(r'^%s="\$\{%s:-([^}]*)\}"\s*$' % (key, key),
                      self.text, re.MULTILINE)
        self.assertIsNotNone(
            m, f"{self.LIB_HW} no longer declares a default for {key} in the "
               f"expected form — the drift alarm cannot read it, which is a "
               f"FAILURE, not a pass")
        return m.group(1)

    def test_lock_file_default_matches(self):
        self.assertEqual(self._default("SA02M_I2C_LOCK_FILE"),
                         tel.HW_LOCK_FILE_DEFAULT,
                         "the daemon and the CGI would take DIFFERENT locks on "
                         "a board whose conf does not set the path — which is "
                         "no lock at all")

    def test_lock_wait_default_matches(self):
        self.assertEqual(float(self._default("SA02M_I2C_LOCK_WAIT_SEC")),
                         tel.HW_LOCK_WAIT_SEC_DEFAULT)

    def test_owner_unit_and_proc_defaults_match(self):
        self.assertEqual(tuple(self._default("SA02M_I2C_OWNER_UNITS").split()),
                         tel.HW_OWNER_UNITS_DEFAULT)
        self.assertEqual(tuple(self._default("SA02M_I2C_OWNER_PROCS").split()),
                         tel.HW_OWNER_PROCS_DEFAULT)

    def test_the_respect_owner_off_values_match(self):
        """Mirrored literally, not normalised: `False` does not switch the gate
        off in the shell either."""
        import re
        m = re.search(r'case "\$\{SA02M_I2C_RESPECT_OWNER:-1\}" in\s*\n\s*'
                      r'([^)]*)\)\s*return 1', self.text)
        self.assertIsNotNone(m, "lib_hw.sh no longer carries the "
                                "SA02M_I2C_RESPECT_OWNER case list")
        self.assertEqual(tuple(m.group(1).split("|")),
                         tel.HW_RESPECT_OWNER_OFF)

    def test_the_shipped_conf_sets_every_key_the_daemon_now_reads(self):
        """The fallbacks are a safety net, not the live values: on a shipped
        board all five come from the conf, and this fails if one is dropped."""
        conf = (self.REPO / "etc" / "sa02m_hw.conf").read_text(
            encoding="utf-8", errors="replace")
        for key in ("SA02M_I2C_LOCK_FILE", "SA02M_I2C_LOCK_WAIT_SEC",
                    "SA02M_I2C_OWNER_UNITS", "SA02M_I2C_OWNER_PROCS",
                    "SA02M_I2C_RESPECT_OWNER"):
            self.assertRegex(conf, r"(?m)^%s=\S" % key,
                             f"etc/sa02m_hw.conf no longer sets {key}")


if __name__ == "__main__":
    unittest.main()

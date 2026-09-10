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
fake register file, AND `subprocess.run` is replaced by a raiser -- a
BaseException, so the helpers' own `except Exception` cannot swallow it -- and
any path that bypassed the shims fails loudly instead of reaching `i2cset`.
TestTheNoRealBusGuardFires proves that on the real helpers -- each suite
installs its own guard, so each proves its own rather than trusting the twin.
The owner probes are shimmed for the same reason (they spawn
`systemctl`/`pgrep`), with the raiser left standing behind them.

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

# The unshimmed helpers, captured before any patching (see the guard class).
REAL_I2CGET = tel._i2cget
REAL_I2CSET = tel._i2cset


class RealSubprocessAttempt(BaseException):
    """The no-real-bus guard's own exception, deliberately NOT an Exception.

    `_i2cget`/`_i2cset` wrap their subprocess call in a blanket
    `except Exception`, so an AssertionError raised by the guard is swallowed
    and a bypassed shim comes back as a quiet None/False -- the guard reads as
    silent approval of exactly the path it exists to catch. A BaseException
    passes straight through that catch and unittest still records it as an
    error, so "fails loudly" is true as written. TestTheNoRealBusGuardFires
    proves it on the real helpers.
    """


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
SA02M_BEEPER_WEB_OVERRIDE_SEC={override_sec}
SA02M_BEEPER_OVERRIDE_FILE={override_file}
SA02M_BEEPER_OVERRIDE_WORKER={override_worker}
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
        # The beeper override lives in the sandbox too: NOTHING here may touch
        # a real /run path, for the same reason nothing may reach a real bus.
        self.override_dir = Path(self._tmp.name) / "hw-override"
        self.override_path = self.override_dir / "beeper.env"
        self.worker_path = Path(self._tmp.name) / "sa02m-beeper-override.sh"
        self.worker_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(self.worker_path, 0o755)

        self.probe_calls: list[list[str]] = []
        self.available: set[str] = set()
        self.probe_rc: dict[str, int | None] = {}

        def fake_probe(argv):
            self.probe_calls.append(list(argv))
            return self.probe_rc.get(" ".join(argv), 1)

        # The override worker is SPAWNED, not run to completion, so it is the
        # one path that reaches subprocess.Popen rather than .run. The shim is
        # the observation point AND the third no-real-bus guard: an argv that
        # points anywhere outside this sandbox fails loudly, so a test can
        # never launch the real /usr/local/sbin worker (which drives the
        # expander, and on bench 1.135 that byte carries bench 1.136's power).
        self.spawns: list[tuple[list[str], dict]] = []

        def fake_popen(argv, **kwargs):
            self.spawns.append(([str(a) for a in argv], kwargs))
            if not str(argv[0]).startswith(self._tmp.name):
                raise RealSubprocessAttempt(
                    f"a test tried to spawn something outside the sandbox: "
                    f"{argv}")
            return types.SimpleNamespace(pid=4242)

        patches = [
            mock.patch.object(tel.subprocess, "Popen", fake_popen),
            mock.patch.object(tel, "_i2cget", self.exp.get),
            mock.patch.object(tel, "_i2cset", self.exp.set),
            mock.patch.object(tel, "_run_probe", fake_probe),
            mock.patch.object(tel, "_probe_available",
                              lambda tool: tool in self.available),
            # The second, independent guard: nothing here may spawn a process.
            # A path that bypassed the shims above would reach the real i2cset
            # -- on bench 1.135 that cuts bench 1.136's power. RealSubprocess-
            # Attempt, not AssertionError: the daemon's blanket `except
            # Exception` swallows the latter (see that class).
            mock.patch.object(
                tel.subprocess, "run",
                mock.Mock(side_effect=RealSubprocessAttempt(
                    "a test tried to run a real subprocess")),
            ),
        ]
        if self.use_fake_flock:
            patches.append(mock.patch.object(tel, "fcntl", self.flock))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write_conf(self, *, lock_file=None, lock_wait="1", extra="",
                   override_sec="7", override_file=None,
                   override_worker=None) -> Path:
        self.conf_path.write_text(
            CONF_TEMPLATE.format(
                lock_file=lock_file if lock_file is not None else self.lock_path,
                lock_wait=lock_wait,
                override_sec=override_sec,
                override_file=(override_file if override_file is not None
                               else self.override_path),
                override_worker=(override_worker if override_worker is not None
                                 else self.worker_path),
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
        self.spawns.clear()
        return stub

    def read_override(self) -> dict:
        """The override file parsed the way etc/sa02m-beeper-override.sh reads
        it: shell `key=value` lines, sourced."""
        parsed = {}
        for line in self.override_path.read_text(
                encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                parsed[key.strip()] = value.strip()
        return parsed

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


class TestTheBeeperOverridePreEmptsABusyBus(LockTestCase):
    """1.0.6.43, the Operator's decision: a cloud «beep» pre-empts the PLC.

    1.0.6.42 made this daemon refuse EVERY hardware command while MPLC4 holds
    the expander. The web panel does something different for exactly one
    channel — `lib_hw.sh sa02m_hw_i2c_write_channel_web` writes
    SA02M_BEEPER_OVERRIDE_FILE and starts SA02M_BEEPER_OVERRIDE_WORKER, so the
    buzzer sounds while the PLC keeps the bus. The daemon now mirrors that, and
    ONLY for `beeper`: `do` carries real equipment (on bench 1.135, bench
    1.136's power) and `alarm_led` is not a 7 s pulse, so a widening that
    leaked to either is the defect these cases exist to catch — hence the
    one-channel-wide pin below sits beside the success pin, not after it.

    The file now has TWO producers, this daemon and the CGI. What that costs is
    pinned here too: the write is temp + rename (no reader ever sees half a
    file), the last writer wins carrying its own TTL, and the daemon neither
    reads its own value back as proof nor deletes the file.
    """

    def _busy_client(self, **conf):
        stub = self.ready_client(**conf)
        self.owner_unit_active("mplc4.service")
        return stub

    def test_a_beeper_command_on_a_busy_bus_writes_the_override_file(self):
        """RED before 1.0.6.43: the command was refused and no file appeared."""
        stub = self._busy_client()
        before = int(time.time())

        self.send(stub, "beeper", b"1")

        self.assertTrue(self.override_path.is_file(),
                        "no override file was written for a beeper command on "
                        "a bus MPLC4 holds")
        fields = self.read_override()
        self.assertEqual(fields.get("value"), "1")
        expires = int(fields["expires_at"])
        self.assertGreaterEqual(expires, before + 7)
        self.assertLessEqual(expires, int(time.time()) + 7,
                             "expires_at is not now + the configured TTL")

    def test_the_override_path_touches_no_byte_on_the_held_bus(self):
        """The point of the override: the PLC keeps the bus. A daemon that
        wrote the expander here would be the 1.0.6.42 defect with extra steps."""
        stub = self._busy_client()

        self.send(stub, "beeper", b"1")

        self.assertEqual(self.exp.writes, [], "a byte went out on a held bus")
        self.assertEqual(self.exp.reads, [], "even the read went out")
        self.assertEqual(self.timeline, [], "the bus lock was taken at all")

    def test_the_worker_is_started_detached(self):
        """`nohup … & disown` in the CGI. Not detached, the worker dies with
        the paho callback thread's process group on the next restart — and the
        buzzer stays on until the TTL nobody is enforcing any more."""
        stub = self._busy_client()

        self.send(stub, "beeper", b"1")

        self.assertEqual([argv for argv, _kw in self.spawns],
                         [[str(self.worker_path)]],
                         "the override worker was not started exactly once")
        _argv, kwargs = self.spawns[0]
        self.assertTrue(kwargs.get("start_new_session"),
                        "the worker was not detached from this process group")
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs.get(stream), tel.subprocess.DEVNULL,
                             f"the worker's {stream} was left attached")

    def test_an_absent_worker_is_not_an_error(self):
        """The CGI's `[ -x "$worker" ] || return 0`. A board whose worker was
        not installed still gets the file — the next panel click's worker, or
        the next install, applies it — and no WARNING is logged for it."""
        missing = Path(self._tmp.name) / "nowhere" / "worker.sh"
        stub = self._busy_client(override_worker=missing)

        with self.assertLogs(tel.log, level="INFO") as caught:
            self.send(stub, "beeper", b"1")

        self.assertTrue(self.override_path.is_file())
        self.assertEqual(self.spawns, [])
        self.assertEqual([line for line in caught.output
                          if line.startswith("WARNING")], [],
                         f"an absent worker was reported as a failure: "
                         f"{caught.output}")

    def test_the_command_is_accepted_and_the_journal_names_the_override(self):
        """A success that reads like a plain bus write in the journal would
        hide the one fact an operator needs: the pin was NOT driven by us."""
        stub = self._busy_client()

        with self.assertLogs(tel.log, level="INFO") as caught:
            self.send(stub, "beeper", b"1")

        joined = " ".join(caught.output)
        self.assertIn("override", joined,
                      f"the journal does not say the command took the override "
                      f"path: {caught.output}")
        self.assertIn("mplc4.service", joined,
                      f"the journal does not name the owner we yielded the bus "
                      f"to: {caught.output}")
        self.assertIn(("controls/beeper", "1"), stub.published,
                      "an accepted command published no state")

    def test_a_beeper_off_writes_value_zero(self):
        stub = self._busy_client()

        self.send(stub, "beeper", b"0")

        self.assertEqual(self.read_override().get("value"), "0")
        self.assertIn(("controls/beeper", "0"), stub.published)

    def test_the_ttl_comes_from_the_conf(self):
        stub = self._busy_client(override_sec="3")
        before = int(time.time())

        self.send(stub, "beeper", b"1")

        expires = int(self.read_override()["expires_at"])
        self.assertGreaterEqual(expires, before + 3)
        self.assertLessEqual(expires, int(time.time()) + 3)

    def test_a_non_numeric_ttl_falls_back_to_the_cgi_default(self):
        """`[[ "$ttl" =~ ^[0-9]+$ ]] || ttl=7` in lib_hw.sh. A garbage TTL must
        not become an expires_at the worker reads as already expired (silence)
        or as never expiring (a buzzer nobody stops)."""
        stub = self._busy_client(override_sec="banana")
        before = int(time.time())

        self.send(stub, "beeper", b"1")

        expires = int(self.read_override()["expires_at"])
        self.assertGreaterEqual(expires, before + 7)
        self.assertLessEqual(expires, int(time.time()) + 7)

    def test_the_file_is_written_by_temp_and_rename(self):
        """The file has two producers now. A reader — the worker, or the other
        producer's next read — must never see half of it, so the bytes land
        under a temp name in the same directory and arrive by one rename."""
        stub = self._busy_client()
        renames: list[tuple[str, str, str]] = []
        real_replace = tel.os.replace

        def watched_replace(src, dst):
            renames.append((str(src), str(dst),
                            Path(src).read_text(encoding="utf-8")))
            return real_replace(src, dst)

        with mock.patch.object(tel.os, "replace", watched_replace):
            self.send(stub, "beeper", b"1")

        self.assertEqual(len(renames), 1, "the file did not arrive by rename")
        src, dst, staged = renames[0]
        self.assertEqual(dst, str(self.override_path))
        self.assertNotEqual(src, dst, "the temp name IS the live path")
        self.assertEqual(Path(src).parent, self.override_path.parent,
                         "a rename across filesystems is not atomic")
        self.assertIn("value=1", staged)
        self.assertIn("expires_at=", staged,
                      "the rename staged an incomplete file")
        self.assertEqual(
            sorted(p.name for p in self.override_dir.iterdir()),
            [self.override_path.name], "a temp file was left behind")

    def test_the_daemon_does_not_delete_or_read_back_a_foreign_override(self):
        """Last writer wins, carrying its own TTL — the accepted cost of the
        second producer. The panel's file is REPLACED by ours, never removed,
        and our own value is never read back as proof that anything happened."""
        stub = self._busy_client()
        self.override_dir.mkdir(parents=True, exist_ok=True)
        self.override_path.write_text("value=0\nexpires_at=99999999999\n",
                                      encoding="utf-8")

        self.send(stub, "beeper", b"1")

        fields = self.read_override()
        self.assertEqual(fields.get("value"), "1",
                         "the daemon did not win as the last writer")
        self.assertLess(int(fields["expires_at"]), 99999999999,
                        "the daemon carried the other producer's TTL")

    def test_a_refused_channel_leaves_a_foreign_override_alone(self):
        """A `do` refusal must not touch the buzzer's file — the panel may have
        a beep in flight while the cloud is being told no."""
        stub = self._busy_client()
        self.override_dir.mkdir(parents=True, exist_ok=True)
        self.override_path.write_text("value=1\nexpires_at=99999999999\n",
                                      encoding="utf-8")

        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "do", b"1")

        self.assertEqual(self.read_override(),
                         {"value": "1", "expires_at": "99999999999"},
                         "a refused channel rewrote the beeper's override")

    def test_do_and_alarm_led_on_a_busy_bus_still_refuse(self):
        """The widening is EXACTLY one channel wide. `do` commutes real
        equipment — on bench 1.135 it carries bench 1.136's power — and the
        panel refuses both of these on a held bus too."""
        for channel in ("do", "alarm_led"):
            with self.subTest(channel=channel):
                stub = self._busy_client()

                # Every substantive assertion sits INSIDE the assertLogs block,
                # for the reason TestASecondHolderIsNotOverridden states: a
                # widening that reached this channel writes the override file
                # and logs INFO, so a block ending on "no WARNING" would report
                # the missing log line and bury the fact that `do` just took
                # the buzzer's path.
                with self.assertLogs(tel.log, level="WARNING") as caught:
                    self.send(stub, channel, b"1")
                    self.assertFalse(
                        self.override_path.exists(),
                        f"a {channel} command took the beeper's override path")
                    self.assertEqual(self.spawns, [],
                                     f"a {channel} command started the beeper "
                                     f"worker")
                    self.assertEqual(stub.published, [],
                                     f"a refused {channel} published state")
                    self.assertEqual(self.exp.writes, [],
                                     "a byte went out on a held bus")

                self.assertTrue(
                    any("mplc4.service" in line for line in caught.output),
                    f"the refusal must still name the owner: {caught.output}")

    def test_a_free_bus_still_drives_the_beeper_on_the_bus(self):
        """Non-vacuity, and the branch order: the override is the BUSY-bus
        path only. A daemon that always took it would stop driving the buzzer
        on the boards that have no PLC at all."""
        stub = self.ready_client()

        self.send(stub, "beeper", b"1")

        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << BIT_BEEPER)])
        self.assertFalse(self.override_path.exists(),
                         "a free bus was answered with an override file")
        self.assertEqual(self.spawns, [])

    def test_an_unwritable_override_is_a_refusal_not_a_silent_success(self):
        """The CGI's `|| return "$SA02M_HW_RC_IO"`. Publishing «on» for a file
        that never landed is the retained-lie shape 1.0.6.42 removed."""
        stub = self._busy_client()

        with mock.patch.object(tel.os, "replace",
                               mock.Mock(side_effect=OSError(13, "denied"))):
            with self.assertLogs(tel.log, level="WARNING") as caught:
                self.send(stub, "beeper", b"1")

        self.assertEqual(stub.published, [],
                         "a failed override published state anyway")
        self.assertEqual(self.spawns, [],
                         "the worker was started for a file that never landed")
        self.assertTrue(
            any(str(self.override_path) in line for line in caught.output),
            f"the refusal must name the file it could not write: "
            f"{caught.output}")

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_the_file_and_its_directory_carry_the_modes_the_cgi_declares(self):
        """664 / 775, so the OTHER producer — www-data's CGI — can still
        replace a file this root daemon wrote, and the worker can read it."""
        stub = self._busy_client()

        self.send(stub, "beeper", b"1")

        self.assertEqual(self.override_path.stat().st_mode & 0o777, 0o664)
        self.assertEqual(self.override_dir.stat().st_mode & 0o777, 0o775)


class TestTheOverrideFileMatchesItsConsumer(unittest.TestCase):
    """The file's shape is a contract with etc/sa02m-beeper-override.sh.

    The worker is the CONSUMER and it is not touched by this branch, so the
    keys it validates are read out of it rather than restated here: a rename on
    either side must fail this, not ship a file the worker silently rejects
    (`read_override` returning 1 = the buzzer simply never sounds, with no
    error anywhere). Shape (c) of docs/agent-rules/quality-gate-rigor.md — the
    file that can BREAK this guarantee is the one that is read.
    """

    REPO = Path(__file__).resolve().parents[3]
    WORKER = "etc/sa02m-beeper-override.sh"

    def test_the_worker_validates_exactly_the_keys_the_daemon_writes(self):
        text = (self.REPO / self.WORKER).read_text(encoding="utf-8",
                                                   errors="replace")
        self.assertIn('case "$value" in 0|1', text,
                      f"{self.WORKER} no longer validates `value` as 0|1 — the "
                      f"key the daemon writes may have been renamed")
        self.assertIn('case "$expires_at" in', text,
                      f"{self.WORKER} no longer validates `expires_at`")
        self.assertIn(tel.HW_BEEPER_OVERRIDE_FILE_DEFAULT, text,
                      f"{self.WORKER} and the daemon default to DIFFERENT "
                      f"override files — the worker would poll a path nobody "
                      f"writes")


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



class TestTheNoRealBusGuardFires(LockTestCase):
    """This module's SAFETY paragraph, measured instead of asserted.

    RED before the 1.0.6.42 review finding that produced it: with the raiser
    raising AssertionError, `_i2cget` came back None and `_i2cset` False --
    their blanket `except Exception` ate the guard, so "fails loudly" was
    false while the suite printed ok. Twin of the case in
    test_telemetry_hw_map.py, because the guard is installed per suite.
    """

    def test_a_bypassed_i2cget_fails_loudly(self):
        with self.assertRaises(RealSubprocessAttempt):
            REAL_I2CGET(2, 0x41, REG_OUT)

    def test_a_bypassed_i2cset_fails_loudly(self):
        with self.assertRaises(RealSubprocessAttempt):
            REAL_I2CSET(2, 0x41, REG_OUT, ALL_OFF)

    def test_the_shims_are_still_what_the_suite_actually_calls(self):
        """Non-vacuity: every other case here must reach the fake register
        file, not this raiser."""
        self.assertIsNot(tel._i2cget, REAL_I2CGET)
        self.assertIsNot(tel._i2cset, REAL_I2CSET)


class TestTheFallbacksMatchTheCgi(unittest.TestCase):
    """The drift alarm for EVERY /etc/sa02m_hw.conf key this daemon reads.

    lib_hw.sh is the reference consumer and 1.0.6.42 deliberately does not
    touch it. No Python process can source a shell file, so each `:-` default
    it declares is duplicated in sa02m_telemetry.py -- and a duplicate that
    drifts unnoticed is the defect this release exists to remove.

    The first version of this pin covered the five lock/owner constants by
    hand and MISSED SA02M_I2C_EXTRA_OUTPUT_MASK, whose daemon-side fallback of
    0 dropped bit3 -- KLogic's blue LED -- out of the direction register on
    every board whose conf predates 1.0.5.64, while the CGI kept writing it
    back as an output. A hand-written list of five was the wrong shape, so the
    ledger below is enumerated from the daemon's own SOURCE: a key it reads
    that the ledger does not name FAILS, and a ledger row for a key it no
    longer reads FAILS as stale (docs/agent-rules/quality-gate-rigor.md shapes
    (b) and (g)).

    Three halves per key, because none of them catches what the others do:

      * the default DECLARED in lib_hw.sh is read out of that file and
        compared -- never restated as a literal here;
      * the daemon really READS the key: the probe conf carries a value that
        is NOT the default and the loaded profile shows it. Without this the
        drop-one case would pass for a key nothing consumes;
      * dropping that key from the probe conf resolves to the CGI's default,
        which is the divergence B1 actually was.
    """

    REPO = Path(__file__).resolve().parents[3]
    LIB_HW = "www/network_config/cgi-bin/lib_hw.sh"
    DAEMON = "opt/sa02m-modbus-mqtt/sa02m_telemetry.py"

    # The probe conf: every key set to something the CGI would NOT default to,
    # so "the key was dropped" is observable rather than a coincidence.
    PROBE_BITS = {"do": 1, "beeper": 2, "alarm_led": 0}
    PROBE_EXTRA_MASK = 0x04
    PROBE_LOCK_FILE = "/run/lock/probe-not-the-default.lock"
    PROBE_OVERRIDE_FILE = "/run/probe-not-the-default/beeper.env"
    PROBE_OVERRIDE_WORKER = "/usr/local/sbin/probe-not-the-default.sh"
    PROBE_VALUES = (
        ("SA02M_HW_BACKEND", "disabled"),
        ("SA02M_I2C_EXP_BUS", "5"),
        ("SA02M_I2C_EXP_ADDR", "0x42"),
        ("SA02M_I2C_ACTIVE_LOW_MASK", "0x02"),
        ("SA02M_I2C_EXTRA_OUTPUT_MASK", "0x04"),
        ("SA02M_I2C_LOCK_FILE", PROBE_LOCK_FILE),
        ("SA02M_I2C_LOCK_WAIT_SEC", "4"),
        ("SA02M_I2C_OWNER_UNITS", '"probe-a.service probe-b.service"'),
        ("SA02M_I2C_OWNER_PROCS", '"probe-a probe-b"'),
        ("SA02M_I2C_RESPECT_OWNER", "0"),
        ("SA02M_BEEPER_WEB_OVERRIDE_SEC", "3"),
        ("SA02M_BEEPER_OVERRIDE_FILE", PROBE_OVERRIDE_FILE),
        ("SA02M_BEEPER_OVERRIDE_WORKER", PROBE_OVERRIDE_WORKER),
        ("SA02M_I2C_BIT_DO", "1"),
        ("SA02M_I2C_BIT_BEEPER", "2"),
        ("SA02M_I2C_BIT_ALARM_LED", "0"),
        ("SA02M_I2C_BIT_USB_POWER", ""),
    )

    # Keys the daemon reads by PREFIX, one per channel. Each is handled by its
    # own case below rather than by the scalar ledger.
    PREFIX_KEYS = ("SA02M_I2C_BIT_", "SA02M_GPIO_")

    def setUp(self):
        self.lib = self.REPO / self.LIB_HW
        self.assertTrue(self.lib.is_file(), f"{self.lib} is missing")
        self.text = self.lib.read_text(encoding="utf-8", errors="replace")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    # -- reading the reference ---------------------------------------------
    def _default(self, key: str) -> str:
        import re
        m = re.search(r'^%s="\$\{%s:-([^}]*)\}"\s*$' % (key, key),
                      self.text, re.MULTILINE)
        self.assertIsNotNone(
            m, f"{self.LIB_HW} no longer declares a default for {key} in the "
               f"expected form — the drift alarm cannot read it, which is a "
               f"FAILURE, not a pass")
        return m.group(1)

    def _respect_owner_off_values(self) -> tuple[str, ...]:
        import re
        m = re.search(r'case "\$\{SA02M_I2C_RESPECT_OWNER:-1\}" in\s*\n\s*'
                      r'([^)]*)\)\s*return 1', self.text)
        self.assertIsNotNone(m, "lib_hw.sh no longer carries the "
                                "SA02M_I2C_RESPECT_OWNER case list")
        return tuple(m.group(1).split("|"))

    # -- the ledger ---------------------------------------------------------
    def _backend_for(self, default: str) -> str:
        """lib_hw.sh sa02m_hw_backend(), on a conf declaring no GPIO line.

        Mapped, not guessed: an unrecognised default fails the case rather
        than falling through to i2c_expander, which is what the shell's `*)`
        does and would hide a default we no longer understand.
        """
        table = {"off": "disabled", "disabled": "disabled", "none": "disabled",
                 "i2c": "i2c_expander", "i2c_expander": "i2c_expander",
                 "gpio": "gpio_sysfs", "gpio_sysfs": "gpio_sysfs",
                 "auto": "i2c_expander", "": "i2c_expander"}
        self.assertIn(default, table,
                      "lib_hw.sh's SA02M_HW_BACKEND default is a value this "
                      "pin does not know how to resolve")
        return table[default]

    def _active_low_for(self, default: str) -> int:
        """lib_hw.sh sa02m_hw_i2c_active_low_mask_dec, on the probe conf."""
        if default in ("auto", ""):
            mask = self.PROBE_EXTRA_MASK
            for bit in self.PROBE_BITS.values():
                mask |= 1 << bit
            return mask & 0x0F
        return int(default, 0) & 0x0F

    def _ledger(self) -> dict:
        """key -> (probe, expected-from-lib-default, expected-on-probe-conf)."""
        return {
            "SA02M_HW_BACKEND": (
                lambda p: p.backend, self._backend_for, "disabled"),
            "SA02M_I2C_EXP_BUS": (
                lambda p: p.bus, lambda d: int(d), 5),
            "SA02M_I2C_EXP_ADDR": (
                lambda p: p.addr, lambda d: int(d, 0), 0x42),
            "SA02M_I2C_ACTIVE_LOW_MASK": (
                lambda p: p.active_low_mask, self._active_low_for, 0x02),
            "SA02M_I2C_EXTRA_OUTPUT_MASK": (
                lambda p: p.extra_output_mask,
                lambda d: int(d, 0) & 0x0F, 0x04),
            "SA02M_I2C_LOCK_FILE": (
                lambda p: p.lock_file, lambda d: d, self.PROBE_LOCK_FILE),
            "SA02M_I2C_LOCK_WAIT_SEC": (
                lambda p: p.lock_wait_s, lambda d: float(d), 4.0),
            "SA02M_I2C_OWNER_UNITS": (
                lambda p: p.owner_units, lambda d: tuple(d.split()),
                ("probe-a.service", "probe-b.service")),
            "SA02M_I2C_OWNER_PROCS": (
                lambda p: p.owner_procs, lambda d: tuple(d.split()),
                ("probe-a", "probe-b")),
            "SA02M_I2C_RESPECT_OWNER": (
                lambda p: p.respect_owner,
                lambda d: d not in self._respect_owner_off_values(), False),
            # 1.0.6.43: the beeper override, now written by this daemon too.
            # Read through the SAME `val("…")` idiom the enumeration above
            # scans for, deliberately — the ledger's known blind spot (a
            # `_read_conf_value` call, a single-quoted val) is recorded in
            # .ai-dev/backlog.md, and adding keys it cannot see is the defect
            # that entry exists to prevent.
            "SA02M_BEEPER_WEB_OVERRIDE_SEC": (
                lambda p: p.beeper_override_sec, lambda d: int(d), 3),
            "SA02M_BEEPER_OVERRIDE_FILE": (
                lambda p: p.beeper_override_file, lambda d: d,
                self.PROBE_OVERRIDE_FILE),
            "SA02M_BEEPER_OVERRIDE_WORKER": (
                lambda p: p.beeper_override_worker, lambda d: d,
                self.PROBE_OVERRIDE_WORKER),
        }

    def _write_conf(self, *, drop: str = "") -> str:
        lines = [f"{k}={v}" for k, v in self.PROBE_VALUES if k != drop]
        path = Path(self._tmp.name) / f"hw{drop}.conf"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    # -- the three halves ---------------------------------------------------
    def test_every_conf_key_the_daemon_reads_is_in_this_ledger(self):
        """The enumeration itself, so a seventh duplicated default cannot be
        added without either a pin or a deliberate row here (B1's real cause:
        the pin covered five of six keys while its description promised all)."""
        import re
        src = (self.REPO / self.DAEMON).read_text(encoding="utf-8",
                                                  errors="replace")
        found = set(re.findall(r'val\("(SA02M_[A-Z0-9_]*)"', src))
        self.assertEqual(
            found, set(self._ledger()) | set(self.PREFIX_KEYS),
            "the set of /etc/sa02m_hw.conf keys sa02m_telemetry.py reads no "
            "longer matches this ledger — a new one needs its pin, a removed "
            "one needs its row deleted, and a regex that matched nothing at "
            "all is a FAILURE, not a pass")

    def test_the_probe_conf_is_read_key_by_key(self):
        """Non-vacuity for the drop-one case: each key is really consumed, so
        removing it below proves something."""
        profile = tel.HwProfile.load(self._write_conf())
        for key, (probe, _expect, on_probe_conf) in self._ledger().items():
            with self.subTest(key=key):
                self.assertEqual(probe(profile), on_probe_conf,
                                 f"the daemon does not read {key} from the "
                                 f"conf at all")

    def test_each_absent_key_falls_back_to_the_cgi_default(self):
        """The B1 case, generalised: a conf written before a key existed must
        resolve exactly as lib_hw.sh resolves it on the same file."""
        for key, (probe, expect, _alt) in self._ledger().items():
            with self.subTest(key=key):
                default = self._default(key)
                profile = tel.HwProfile.load(self._write_conf(drop=key))
                self.assertEqual(
                    probe(profile), expect(default),
                    f"{key} absent: the daemon resolves something other than "
                    f"lib_hw.sh's `:-{default}` — the two consumers of ONE "
                    f"register disagree on a board that simply predates the "
                    f"key")

    # -- the constants, pinned at their source too ---------------------------
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

    def test_extra_output_mask_default_matches(self):
        """bit3 is KLogic's blue LED: a daemon defaulting to 0 here writes the
        direction register with it as an INPUT (review finding B1)."""
        self.assertEqual(int(self._default("SA02M_I2C_EXTRA_OUTPUT_MASK"), 0),
                         tel.HW_EXTRA_OUTPUT_MASK_DEFAULT)

    def test_the_beeper_override_defaults_match(self):
        """The two producers must agree on the PATH above all: a daemon writing
        one file while the worker polls another is a buzzer that never sounds,
        with no error on either side. The TTL is the same class one step down —
        a 7 s pulse from the panel and a 30 s one from the cloud would be two
        products."""
        self.assertEqual(int(self._default("SA02M_BEEPER_WEB_OVERRIDE_SEC")),
                         tel.HW_BEEPER_OVERRIDE_SEC_DEFAULT)
        self.assertEqual(self._default("SA02M_BEEPER_OVERRIDE_FILE"),
                         tel.HW_BEEPER_OVERRIDE_FILE_DEFAULT)
        self.assertEqual(self._default("SA02M_BEEPER_OVERRIDE_WORKER"),
                         tel.HW_BEEPER_OVERRIDE_WORKER_DEFAULT)

    def test_the_respect_owner_off_values_match(self):
        """Mirrored literally, not normalised: `False` does not switch the gate
        off in the shell either."""
        self.assertEqual(self._respect_owner_off_values(),
                         tel.HW_RESPECT_OWNER_OFF)

    # -- the keys with no mirrored default, and why -------------------------
    def test_a_missing_bit_is_refused_rather_than_defaulted(self):
        """SA02M_I2C_BIT_* is the one prefix the daemon deliberately does NOT
        mirror: lib_hw.sh defaults it (`:-1`, `:-2`, `:-0`) and this daemon
        refuses instead, because a guessed pin is the 1.0.6.42 defect itself.
        Pinned as behaviour so "deliberate" stays measurable — the CGI-side
        values are pinned against the shipped conf in test_telemetry_hw_map.py
        TestShippedConfIsTheOneHome.
        """
        for channel in ("do", "beeper", "alarm_led"):
            with self.subTest(channel=channel):
                key = "SA02M_I2C_BIT_" + channel.upper()
                self.assertNotEqual(self._default(key), "",
                                    f"{self.LIB_HW} no longer defaults {key} — "
                                    f"this case would then pin nothing")
                profile = tel.HwProfile.load(self._write_conf(drop=key))
                self.assertNotIn(channel, profile.bits)
                self.assertIn(channel, profile.refusals)

    def test_the_gpio_keys_carry_no_default_on_either_side(self):
        """SA02M_GPIO_* is read only to answer «is a sysfs line configured» in
        `auto`. Empty on both sides, so there is no value to drift — pinned so
        that stays true rather than assumed."""
        for channel in tel.HW_MASK_CHANNELS:
            with self.subTest(channel=channel):
                self.assertEqual(self._default("SA02M_GPIO_" + channel.upper()),
                                 "")

    def test_the_shipped_conf_sets_every_key_the_daemon_now_reads(self):
        """The fallbacks are a safety net, not the live values: on a shipped
        board every ledger key comes from the conf, and this fails if one is
        dropped. Derived from the ledger, so a new row is covered here too."""
        import re
        conf = (self.REPO / "etc" / "sa02m_hw.conf").read_text(
            encoding="utf-8", errors="replace")
        for key in self._ledger():
            with self.subTest(key=key):
                self.assertRegex(conf, r"(?m)^%s=\S" % key,
                                 f"etc/sa02m_hw.conf no longer sets {key}")
        self.assertTrue(re.search(r"(?m)^SA02M_I2C_BIT_DO=\S", conf))


if __name__ == "__main__":
    unittest.main()

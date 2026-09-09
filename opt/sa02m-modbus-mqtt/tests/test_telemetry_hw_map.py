#!/usr/bin/env python3
"""The telemetry daemon drives the PCA9536 bits /etc/sa02m_hw.conf names.

The defect this pins (1.0.6.42). `_make_hw_cb` carried
``bit_map = {"do": 0, "beeper": 1, "alarm_led": 2}`` in code and never opened
`/etc/sa02m_hw.conf`. The board, the config and the CGI layer all say the
opposite -- bit0 = alarm LED, bit1 = DO, bit2 = buzzer -- so the daemon's
`beeper` command drove the DISCRETE OUTPUT. On bench 1.135 that output carries
bench 1.136's power, which is how it was found.

The defect had a second half, and a fix for only the first would have been
worse than no fix: the outputs are ACTIVE-LOW (`lib_hw.sh`
sa02m_hw_i2c_write_channel_locked, `etc/sa02m-beeper-override.sh` apply_once,
`etc/sa02m-pre-start.sh` PCA9536_OFF=0x0f all agree), while
`PCA9536Control.set_bit(bit, True)` drove the pin HIGH -- which is OFF. So
every command was both shifted AND inverted, and a map-only fix would have
left the daemon commanding the right pin to the wrong level while looking
repaired. Both halves are asserted here.

SAFETY -- this suite must never reach a real I2C bus. Bench 1.135's expander
drives bench 1.136's power, so a stray write cuts a board. Two independent
guards, not one: `_i2cget`/`_i2cset` are replaced by a fake register file, AND
`subprocess.run` is replaced by a raiser, so a code path that ever bypassed
the shims fails loudly instead of reaching `i2cset`.

Stub idiom mirrors tests/test_telemetry_device_id.py -- sa02m_telemetry.py
calls sys.exit() at import when paho is absent, so the paho stub is mandatory
or the whole py-unit discovery aborts.
"""
from __future__ import annotations

import os
import sys
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

# The shipped values, quoted from etc/sa02m_hw.conf. Not a second home: the
# suite ASSERTS the tracked conf still carries them (TestShippedConfIsTheOneHome
# below), so a change to the real file that this fixture did not follow turns
# the run RED instead of leaving the tests passing against a stale idea.
SHIPPED_BIT_DO = 1
SHIPPED_BIT_BEEPER = 2
SHIPPED_BIT_ALARM_LED = 0

REG_OUT = 0x01
REG_DIR = 0x03

# Active-low, all four outputs off. What pre-start writes as PCA9536_OFF.
ALL_OFF = 0x0F

CONF_TEMPLATE = """\
SA02M_HW_BACKEND={backend}
SA02M_I2C_EXP_BUS={bus}
SA02M_I2C_EXP_ADDR={addr}
SA02M_I2C_ACTIVE_LOW_MASK={active_low}
SA02M_I2C_EXTRA_OUTPUT_MASK=0x08
SA02M_I2C_BIT_DO={bit_do}
SA02M_I2C_BIT_BEEPER={bit_beeper}
SA02M_I2C_BIT_ALARM_LED={bit_alarm_led}
SA02M_I2C_BIT_USB_POWER=
SA02M_I2C_LOCK_FILE={lock_file}
"""


class _Msg:
    def __init__(self, topic: str, payload: bytes = b"1", retain: bool = False):
        self.topic = topic
        self.payload = payload
        self.retain = retain


class FakeExpander:
    """A PCA9536's two registers, plus a log of every write that reached it."""

    def __init__(self, out: int = ALL_OFF, readable: bool = True):
        self.regs = {REG_OUT: out, REG_DIR: 0xFF}
        self.writes: list[tuple[int, int, int, int]] = []
        self.reads: list[tuple[int, int, int]] = []
        self.readable = readable

    def get(self, bus: int, addr: int, reg: int):
        self.reads.append((bus, addr, reg))
        if not self.readable:
            return None
        return self.regs.get(reg)

    def set(self, bus: int, addr: int, reg: int, value: int) -> bool:
        self.writes.append((bus, addr, reg, value))
        self.regs[reg] = value & 0xFF
        return True

    @property
    def out_writes(self) -> list[int]:
        return [v for (_b, _a, r, v) in self.writes if r == REG_OUT]


class HwTestCase(unittest.TestCase):
    """Base: a fake expander, a temp hw.conf, and the no-real-bus guards."""

    def setUp(self):
        self.exp = FakeExpander()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conf_path = Path(self._tmp.name) / "sa02m_hw.conf"
        # Never the real /run/lock path, and never a real owner probe: since
        # 1.0.6.42 item 6 every bus operation takes the flock and asks whether
        # MPLC4 holds the expander, so a suite that let either reach the host
        # would pass or fail on what happens to be installed on it. The lock
        # tests are tests/test_telemetry_hw_lock.py; here both are pinned to
        # "free bus" so these cases keep measuring the channel map alone.
        self.lock_path = Path(self._tmp.name) / "pca9536.lock"

        patches = [
            mock.patch.object(tel, "_i2cget", self.exp.get),
            mock.patch.object(tel, "_i2cset", self.exp.set),
            mock.patch.object(tel, "_probe_available", lambda tool: False),
            mock.patch.object(tel, "_run_probe", mock.Mock(side_effect=AssertionError(
                "the owner gate probed a tool it had been told was absent"))),
            # The second, independent guard: nothing in this suite may spawn a
            # process. If a path ever bypassed the shims above it would reach
            # the real i2cset -- on bench 1.135 that cuts 1.136's power.
            mock.patch.object(
                tel.subprocess, "run",
                mock.Mock(side_effect=AssertionError(
                    "a test tried to run a real i2c subprocess")),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write_conf(
        self, *, backend="i2c_expander", bus=2, addr="0x41", active_low="auto",
        bit_do=SHIPPED_BIT_DO, bit_beeper=SHIPPED_BIT_BEEPER,
        bit_alarm_led=SHIPPED_BIT_ALARM_LED, extra="",
    ) -> Path:
        body = CONF_TEMPLATE.format(
            backend=backend, bus=bus, addr=addr, active_low=active_low,
            bit_do=bit_do, bit_beeper=bit_beeper, bit_alarm_led=bit_alarm_led,
            lock_file=self.lock_path,
        ) + extra
        self.conf_path.write_text(body, encoding="utf-8")
        os.environ["SA02M_HW_CONF"] = str(self.conf_path)
        self.addCleanup(os.environ.pop, "SA02M_HW_CONF", None)
        return self.conf_path

    def make_client(self):
        """A stub `self` carrying only what the hardware paths touch."""
        published: list[tuple[str, str]] = []
        stub = types.SimpleNamespace(
            _hw=None,
            _device_id="SA-02m",
            published=published,
            _pub=lambda suffix, value, retain=True: published.append((suffix, value)),
        )
        return stub

    def init_hw(self, stub):
        tel.TelemetryClient.init_hw(stub)
        return stub._hw

    def send(self, stub, ctrl: str, payload: bytes):
        cb = tel.TelemetryClient._make_hw_cb(stub, ctrl)
        cb(None, None, _Msg(f"/devices/SA-02m/controls/{ctrl}/on", payload))


class TestCommandsLandOnTheConfiguredBit(HwTestCase):
    """The headline defect: which physical pin a command reaches."""

    def test_do_on_drives_the_configured_do_bit(self):
        """RED before the fix: `do` landed on bit 0 (the alarm LED) at the
        wrong level. The configured DO bit is 1 and the channel is active-low,
        so ON must CLEAR bit 1 and touch nothing else."""
        self.write_conf()
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        self.send(stub, "do", b"1")

        self.assertEqual(
            self.exp.out_writes, [ALL_OFF & ~(1 << SHIPPED_BIT_DO)],
            "a `do` command must clear exactly the configured DO bit "
            f"(bit {SHIPPED_BIT_DO}) and leave every other bit alone",
        )

    def test_beeper_on_never_touches_the_discrete_output(self):
        """The safety case, stated as its own assertion: whatever else a
        `beeper` command does, the DO bit must come out unchanged. This is the
        bit that carried bench 1.136's power."""
        self.write_conf()
        stub = self.make_client()
        self.init_hw(stub)
        before = self.exp.regs[REG_OUT]

        self.send(stub, "beeper", b"1")

        do_mask = 1 << SHIPPED_BIT_DO
        self.assertEqual(
            self.exp.regs[REG_OUT] & do_mask, before & do_mask,
            "a `beeper` command changed the DISCRETE OUTPUT bit",
        )

    def test_every_channel_maps_to_its_own_configured_bit(self):
        self.write_conf()
        for ctrl, bit in (
            ("do", SHIPPED_BIT_DO),
            ("beeper", SHIPPED_BIT_BEEPER),
            ("alarm_led", SHIPPED_BIT_ALARM_LED),
        ):
            with self.subTest(ctrl=ctrl):
                self.exp.regs[REG_OUT] = ALL_OFF
                stub = self.make_client()
                self.init_hw(stub)
                self.exp.writes.clear()
                self.send(stub, ctrl, b"1")
                self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << bit)])

    def test_a_remapped_config_moves_the_pin(self):
        """The map is READ, not assumed. Same code, a conf that swaps DO onto
        bit 3, and the write must follow the conf -- the property a hard-coded
        map cannot have, whatever value it is hard-coded to."""
        self.write_conf(bit_do=3)
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << 3)])


class TestPolarity(HwTestCase):
    """The second half. A map-only fix leaves every command inverted."""

    def test_on_clears_and_off_sets_an_active_low_channel(self):
        """`lib_hw.sh:525-530`: on an active-low channel logical 1 clears the
        bit and logical 0 sets it. RED before the fix, which did the reverse."""
        self.write_conf()
        stub = self.make_client()
        self.init_hw(stub)
        mask = 1 << SHIPPED_BIT_DO

        self.send(stub, "do", b"1")
        self.assertEqual(self.exp.regs[REG_OUT] & mask, 0,
                         "logical ON must pull an active-low output LOW")

        self.send(stub, "do", b"0")
        self.assertEqual(self.exp.regs[REG_OUT] & mask, mask,
                         "logical OFF must leave an active-low output HIGH")

    def test_an_explicit_active_high_mask_is_honoured(self):
        """Polarity is read too, not swapped for a new constant: a conf that
        declares no channel active-low must write straight through."""
        self.write_conf(active_low="0x0")
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.regs[REG_OUT] = 0x00
        mask = 1 << SHIPPED_BIT_DO

        self.send(stub, "do", b"1")
        self.assertEqual(self.exp.regs[REG_OUT] & mask, mask)

    def test_published_state_is_the_logical_level_not_the_raw_bit(self):
        """The read path shares the polarity rule. With all outputs off
        (0x0F on an active-low port) every control must publish "0" -- before
        the fix the raw bits were published and the app showed the opposite of
        the hardware."""
        self.write_conf()
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.regs[REG_OUT] = ALL_OFF

        tel.TelemetryClient._publish_metrics(stub)

        states = dict(stub.published)
        for ctrl in ("do", "beeper", "alarm_led"):
            self.assertEqual(states.get(f"controls/{ctrl}"), "0",
                             f"{ctrl} reported on while the port reads all-off")


class TestUnconfiguredChannelIsRefused(HwTestCase):
    """Never drive a guessed pin. A wrong write is worse than no write."""

    def test_absent_conf_refuses_every_channel_and_logs(self):
        os.environ["SA02M_HW_CONF"] = str(self.conf_path / "does-not-exist")
        self.addCleanup(os.environ.pop, "SA02M_HW_CONF", None)
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        with self.assertLogs(tel.log, level="WARNING") as caught:
            self.send(stub, "do", b"1")

        self.assertEqual(self.exp.out_writes, [],
                         "a channel with no config was driven anyway")
        self.assertTrue(any("do" in line for line in caught.output),
                        "the refusal must name the channel it refused")

    def test_unparseable_bit_is_refused_not_guessed(self):
        self.write_conf(bit_do="banana")
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "do", b"1")
        self.assertEqual(self.exp.out_writes, [])

    def test_out_of_range_bit_is_refused(self):
        """The PCA9536 has four pins. `lib_hw.sh:247` accepts only 0..3."""
        self.write_conf(bit_beeper=7)
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        with self.assertLogs(tel.log, level="WARNING"):
            self.send(stub, "beeper", b"1")
        self.assertEqual(self.exp.out_writes, [])

    def test_one_bad_channel_does_not_disable_the_good_ones(self):
        """Refusal is per channel: an integrator who blanked the beeper bit
        keeps working DO and alarm-LED control."""
        self.write_conf(bit_beeper="")
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        self.send(stub, "do", b"1")
        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << SHIPPED_BIT_DO)])

    def test_a_refused_channel_publishes_no_state(self):
        """Silence beats a fabricated reading: with no bit for the channel the
        daemon cannot know its level, so it must not publish one."""
        self.write_conf(bit_beeper="")
        stub = self.make_client()
        self.init_hw(stub)

        tel.TelemetryClient._publish_metrics(stub)

        states = dict(stub.published)
        self.assertNotIn("controls/beeper", states)
        self.assertIn("controls/do", states)


class TestBackendIsHonoured(HwTestCase):
    """SA02M_HW_BACKEND had three documented values and the daemon read none."""

    def test_disabled_never_touches_the_bus(self):
        """`disabled` is what an integrator sets to keep software off the
        expander. Before the fix the daemon wrote to it regardless."""
        self.write_conf(backend="disabled")
        stub = self.make_client()
        self.init_hw(stub)

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.writes, [],
                         "backend=disabled still reached the I2C bus")
        self.assertEqual(self.exp.reads, [],
                         "backend=disabled still read the I2C bus")

    def test_gpio_sysfs_refuses_rather_than_driving_the_expander(self):
        """The daemon implements no sysfs GPIO backend. The choice recorded in
        1.0.6.42: refuse and say so, because the alternative it used to take --
        drive the PCA9536 anyway -- writes to hardware the operator explicitly
        routed elsewhere."""
        self.write_conf(backend="gpio_sysfs")
        stub = self.make_client()
        self.init_hw(stub)

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.writes, [])

    def test_auto_with_no_gpio_pins_resolves_to_the_expander(self):
        """`lib_hw.sh:48-57`: auto means sysfs only when a GPIO pin is set."""
        self.write_conf(backend="auto")
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.out_writes, [ALL_OFF & ~(1 << SHIPPED_BIT_DO)])

    def test_auto_with_a_gpio_pin_resolves_to_sysfs(self):
        self.write_conf(backend="auto", extra="SA02M_GPIO_DO=42\n")
        stub = self.make_client()
        self.init_hw(stub)

        self.send(stub, "do", b"1")

        self.assertEqual(self.exp.writes, [])


class TestBusAddressComeFromTheConf(HwTestCase):
    def test_a_relocated_expander_is_followed(self):
        self.write_conf(bus=5, addr="0x42")
        stub = self.make_client()
        self.init_hw(stub)
        self.exp.writes.clear()

        self.send(stub, "do", b"1")

        self.assertTrue(self.exp.writes, "no write reached the expander at all")
        for (bus, addr, _reg, _val) in self.exp.writes:
            self.assertEqual((bus, addr), (5, 0x42))


class TestShippedConfIsTheOneHome(unittest.TestCase):
    """Non-vacuity for this whole suite.

    Every test above is written against a fixture. If the tracked
    `etc/sa02m_hw.conf` ever disagreed with that fixture, the suite would keep
    passing while the daemon drove the wrong pin on a real board -- the exact
    shape of the defect being fixed. So the fixture is pinned to the shipped
    file, and to `lib_hw.sh`'s defaults, which is the consumer the daemon was
    made to agree with.
    """

    REPO = Path(__file__).resolve().parents[3]

    def _conf_values(self, path: Path, pattern: str) -> dict:
        import re
        text = path.read_text(encoding="utf-8", errors="replace")
        return dict(re.findall(pattern, text, re.MULTILINE))

    def test_shipped_conf_matches_the_fixture(self):
        conf = self.REPO / "etc" / "sa02m_hw.conf"
        self.assertTrue(conf.is_file(), f"{conf} is missing -- the one home is gone")
        vals = self._conf_values(conf, r"^(SA02M_I2C_BIT_\w+)=(\S*)\s*$")
        self.assertEqual(vals.get("SA02M_I2C_BIT_DO"), str(SHIPPED_BIT_DO))
        self.assertEqual(vals.get("SA02M_I2C_BIT_BEEPER"), str(SHIPPED_BIT_BEEPER))
        self.assertEqual(vals.get("SA02M_I2C_BIT_ALARM_LED"), str(SHIPPED_BIT_ALARM_LED))

    def test_cgi_defaults_match_the_shipped_conf(self):
        """The daemon and the web buttons must reach the same pin. lib_hw.sh is
        deliberately NOT touched by 1.0.6.42; this is the drift alarm."""
        lib = self.REPO / "www" / "network_config" / "cgi-bin" / "lib_hw.sh"
        self.assertTrue(lib.is_file(), f"{lib} is missing")
        vals = self._conf_values(
            lib, r'^(SA02M_I2C_BIT_\w+)="\$\{SA02M_I2C_BIT_\w+:-(\d*)\}"\s*$')
        self.assertEqual(vals.get("SA02M_I2C_BIT_DO"), str(SHIPPED_BIT_DO))
        self.assertEqual(vals.get("SA02M_I2C_BIT_BEEPER"), str(SHIPPED_BIT_BEEPER))
        self.assertEqual(vals.get("SA02M_I2C_BIT_ALARM_LED"), str(SHIPPED_BIT_ALARM_LED))


if __name__ == "__main__":
    unittest.main()

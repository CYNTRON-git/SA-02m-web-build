#!/usr/bin/env python3
"""One telemetry instance per board, and a bounded, NAMED fight for its client id.

The record this pins (bench 1.135, 2026-08-27): the broker log showed
`already connected, closing old connection` at ~1 Hz for hours, the journal
alternated `MQTT connected` / `MQTT disconnected: Unspecified error`, and the
board's own MQTT device flapped offline with it. The daemon never rebuilds its
paho client — that cadence is the exact signature of TWO clients sharing one
client id: paho resets its reconnect delay to `min_delay` after every
successful CONNACK, so two same-id clients evict each other forever, one
second apart. Nothing in the daemon prevented a second copy (a hand-started
one, a stray unit) and nothing named the fight in the journal.

Three seams are pinned here, each on OUR side of paho — paho's real reconnect
loop and mosquitto's takeover are bench acceptance (plan V1–V3), not unit
tests; what a unit test can pin is which paho calls we make, in which order,
with which values:

  * the instance lock: a second copy exits with status 75 naming the lock file
    and the holder's pid, BEFORE it ever reaches the broker (run() takes the
    lock ahead of connect() — pinned in test_telemetry_device_id.py, the
    run()-order stub). The guard itself fails OPEN, once and loudly, when the
    primitive or the lock directory is missing: it prevents a duplicate, it is
    not a safety floor, and a daemon that refuses to start because a tmpfs
    directory is absent is worse than an unguarded one;
  * the eviction detector: three sessions shorter than SHORT_SESSION_S raise
    paho's minimum reconnect delay to STORM_MIN_DELAY_S and log ONE warning
    naming the client id and the broker's own phrase, at most once every
    STORM_WARN_EVERY_S; a session that outlives HEALTHY_SESSION_S restores the
    delay and says so. Session DURATION only, never the reason code (v1 int vs
    v2 ReasonCode) — a broker restart is one long session and one reconnect,
    and must never trip it;
  * the first-connect loop waits on the stop event, not time.sleep, so SIGTERM
    while the broker is down exits promptly instead of five seconds late.

PORTABILITY — the real flock(2) case skips where `fcntl` is absent (the Windows
dev box) and is authoritative on CI and the board; every other case drives an
injected fcntl so the POLICY is measured on every host (the idiom of
tests/test_telemetry_hw_lock.py). A skip is reported as a skip, never as a pass.

Stub idiom mirrors tests/test_telemetry_device_id.py — sa02m_telemetry.py
calls sys.exit() at import when paho is absent, so the stub is mandatory.
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

# The plan's names, spelled once here so a renamed constant fails these cases
# by name rather than by an AttributeError three frames down.
LOCK_ENV = "SA02M_TELEMETRY_LOCK_FILE"
# os.geteuid does not exist on Windows; decorators evaluate at import.
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
STORM_PHRASE = "already connected, closing old connection"


class RecordingClient:
    """The paho surface TelemetryClient touches, every call recorded.

    `connect_failures` scripts the first-connect loop: that many connect()
    calls raise before one succeeds.
    """

    def __init__(self, *a, **kw):
        self.client_id = kw.get("client_id", a[0] if a else None)
        self.delay_calls: list[tuple[int, int]] = []
        self.published: list[tuple[str, str]] = []
        self.subscribed: list[str] = []
        self.connect_calls = 0
        self.loop_starts = 0
        self.connect_failures = 0
        self.on_connect = None
        self.on_disconnect = None

    def will_set(self, *a, **kw):
        pass

    def reconnect_delay_set(self, min_delay, max_delay):
        self.delay_calls.append((min_delay, max_delay))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def message_callback_add(self, topic, cb):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))

    def connect(self, host, port, keepalive=60):
        self.connect_calls += 1
        if self.connect_calls <= self.connect_failures:
            raise ConnectionRefusedError(111, "Connection refused")

    def loop_start(self):
        self.loop_starts += 1


class FakeFcntl:
    """Enough of `fcntl` for the instance lock on a host that has none.

    Never contends: the CONTENTION case is the real primitive's (skipIf on
    this host), this fake exists so the acquire path, the pid hint and the
    unopenable-path branch are measured on every host.
    """

    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def flock(self, fd, op):
        self.calls.append((fd, op))


class SessionTestCase(unittest.TestCase):
    """A TelemetryClient built on a RecordingClient, with a clean id environment."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(tel.DEVICE_ID_ENV, None)
        os.environ.pop(LOCK_ENV, None)
        for p in (
            mock.patch.object(tel.mqtt, "Client", RecordingClient),
            mock.patch.object(tel, "DEVICE_ID_CONF", str(BRIDGE_DIR / "no-such.conf")),
            mock.patch.object(tel.socket, "gethostname", return_value="SA-02m"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def make(self) -> "tel.TelemetryClient":
        return tel.TelemetryClient()


# ── Item 1: the instance lock ────────────────────────────────────────────────
class TestTheInstanceLock(SessionTestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "sa02m-telemetry.lock"
        os.environ[LOCK_ENV] = str(self.lock_path)

    def _release(self, client):
        fd = getattr(client, "_instance_lock_fd", None)
        if fd is not None:
            os.close(fd)            # on the real primitive this drops the flock too

    def _fcntl(self):
        """The real module where it exists, the fake elsewhere."""
        return tel.fcntl if tel.fcntl is not None else FakeFcntl()

    def test_the_lock_file_is_its_own_not_the_bus_lock(self):
        """Sharing the PCA9536 lock would make every CGI write look like a
        second daemon. Env override spelled the way the other sandbox knobs
        are (SA02M_HW_CONF, SA02M_TELEMETRY_CONF)."""
        self.assertEqual(tel.INSTANCE_LOCK_ENV, LOCK_ENV)
        self.assertEqual(tel.INSTANCE_LOCK_FILE_DEFAULT,
                         "/run/lock/sa02m-telemetry.lock")
        self.assertNotEqual(tel.INSTANCE_LOCK_FILE_DEFAULT, tel.HW_LOCK_FILE_DEFAULT)
        self.assertEqual(tel.EXIT_ANOTHER_INSTANCE, 75)

    def test_the_first_copy_takes_the_lock_and_leaves_a_pid_hint(self):
        """The fd is kept for the process lifetime (the kernel releases it on
        any death — there is no stale-lock path to get wrong); the pid in the
        file is a hint for the journal line, the flock is the truth."""
        fake = self._fcntl()
        client = self.make()
        with mock.patch.object(tel, "fcntl", fake):
            client._take_instance_lock()
        self.addCleanup(self._release, client)
        self.assertIsNotNone(client._instance_lock_fd)
        self.assertEqual(self.lock_path.read_text(encoding="utf-8").strip(),
                         str(os.getpid()))
        if isinstance(fake, FakeFcntl):
            self.assertEqual(fake.calls,
                             [(client._instance_lock_fd,
                               fake.LOCK_EX | fake.LOCK_NB)])

    @unittest.skipUnless(os.name == "posix", "POSIX file modes only")
    def test_the_lock_file_is_open_to_its_owner_only(self):
        """0600: unlike the PCA9536 lock this one is not shared with www-data,
        so nothing but this daemon may even OPEN it. Readable is not harmless
        here: flock(2) takes LOCK_EX through a read-only fd, so a 0644 file
        let any local uid hold the lock and keep the unit in its exit-75
        restart loop (review round 2, N1). The expectation moved from 0644 to
        0600 by that finding's order — a tightened pin, not a loosened one."""
        client = self.make()
        client._take_instance_lock()
        self.addCleanup(self._release, client)
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(tel.fcntl is None,
                     "no fcntl on this host (Windows) — the OS primitive is "
                     "measured on CI and the board")
    def test_a_second_copy_exits_75_naming_the_lock_and_the_holder(self):
        """T1a. flock(2) keys on the open file description, so a second open()
        of the same path in this process contends exactly as a second daemon
        would. RED before 1.0.6.68: there was no such function at all."""
        first = self.make()
        first._take_instance_lock()
        self.addCleanup(self._release, first)

        second = self.make()
        with self.assertLogs(tel.log, level="ERROR") as caught:
            with self.assertRaises(SystemExit) as ctx:
                second._take_instance_lock()

        self.assertEqual(ctx.exception.code, tel.EXIT_ANOTHER_INSTANCE)
        line = " ".join(caught.output)
        self.assertIn(str(self.lock_path), line,
                      f"the refusal must name the lock file: {caught.output}")
        self.assertIn(str(os.getpid()), line,
                      f"the refusal must name the holder's pid: {caught.output}")
        self.assertIn("status 75", line)
        self.assertIsNone(second._instance_lock_fd)

    @unittest.skipIf(tel.fcntl is None,
                     "no fcntl on this host (Windows) — the OS primitive is "
                     "measured on CI and the board")
    def test_the_lock_is_free_again_once_the_holder_is_gone(self):
        """Non-vacuity for the case above: the refusal comes from the lock,
        not from a broken fixture — and the unit's 10 s retry really does come
        up once the stray dies."""
        first = self.make()
        first._take_instance_lock()
        self._release(first)

        second = self.make()
        second._take_instance_lock()          # must not raise
        self.addCleanup(self._release, second)
        self.assertIsNotNone(second._instance_lock_fd)

    def test_no_fcntl_warns_once_and_runs_unguarded(self):
        """T1c. Fail-OPEN on the guard itself: a dev host without the
        primitive gets one loud line, not a dead daemon."""
        client = self.make()
        with mock.patch.object(tel, "fcntl", None):
            with self.assertLogs(tel.log, level="WARNING") as caught:
                client._take_instance_lock()
        warns = [ln for ln in caught.output if "instance lock unavailable" in ln]
        self.assertEqual(len(warns), 1, caught.output)
        self.assertIn("unguarded", warns[0])
        self.assertIsNone(client._instance_lock_fd)
        self.assertFalse(self.lock_path.exists())

    def test_an_unopenable_lock_path_warns_once_and_runs_unguarded(self):
        """T1d. /run/lock missing (a tmpfs nobody mounted) — the same fail-open,
        naming the path it could not open so the line is actionable."""
        missing = Path(self._tmp.name) / "no-such-dir" / "sa02m-telemetry.lock"
        os.environ[LOCK_ENV] = str(missing)
        client = self.make()
        with mock.patch.object(tel, "fcntl", self._fcntl()):
            with self.assertLogs(tel.log, level="WARNING") as caught:
                client._take_instance_lock()
        warns = [ln for ln in caught.output if "instance lock unavailable" in ln]
        self.assertEqual(len(warns), 1, caught.output)
        self.assertIn(str(missing), warns[0])
        self.assertIn("unguarded", warns[0])
        self.assertIsNone(client._instance_lock_fd)
        self.assertFalse(missing.parent.exists(),
                         "the guard created the missing directory itself")


@unittest.skipUnless(os.name == "posix", "symlinks, hard links and uids are POSIX")
class TestTheInstanceLockTrustsOnlyItsOwnFile(SessionTestCase):
    """Review F1 (1.0.6.68). /run/lock is world-writable, and this daemon runs
    as root and TRUNCATES and WRITES the file it opens. Before the fix the open
    followed a planted symlink, so any local uid — www-data after a CGI
    compromise — could point the lock path at a root file and have the next
    service start rewrite it as `<pid>\\n`, mode 0644 (reproduced on WSL with
    fs.protected_symlinks/regular = 0: a root 0600 secret came back 0644).

    The guard's answer to anything that is not a regular file of our own with a
    single link is the SAME fail-open it already had for a missing /run/lock:
    one WARN, run unguarded, touch nothing. Never exit 75 on such a file —
    a foreign owner keeping it flocked would otherwise hold the unit in a
    permanent restart loop, a denial of the board's MQTT device.

    That closes the loop only for a file another uid OWNS. A file that is ours
    but that another uid can OPEN is the other half of the same denial —
    TestTheInstanceLockCannotBeSquatted below (the lock is 0600).
    """

    VICTIM = b"root:SECRET-HASH:19000:0:99999:7:::\n"

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.lock_path = self.dir / "sa02m-telemetry.lock"
        os.environ[LOCK_ENV] = str(self.lock_path)
        self.victim = self.dir / "victim"
        self.victim.write_bytes(self.VICTIM)
        os.chmod(self.victim, 0o600)

    def _release(self, client):
        fd = getattr(client, "_instance_lock_fd", None)
        if fd is not None:
            os.close(fd)

    def take(self, client):
        """Call the guard; a SystemExit here is the permanent-restart failure."""
        self.addCleanup(self._release, client)
        with self.assertLogs(tel.log, level="WARNING") as caught:
            try:
                client._take_instance_lock()
            except SystemExit as exc:
                self.fail(f"exited {exc.code} on a lock file that is not ours — "
                          f"the unit would restart forever")
        return [ln for ln in caught.output if "instance lock unavailable" in ln]

    def assert_victim_untouched(self):
        self.assertEqual(self.victim.read_bytes(), self.VICTIM,
                         "root wrote through the lock path into another file")
        self.assertEqual(self.victim.stat().st_mode & 0o777, 0o600,
                         "root changed the mode of another file")

    def test_a_planted_symlink_is_never_followed(self):
        """RED before the fix: the victim came back `<pid>\\n`, mode 0644."""
        os.symlink(self.victim, self.lock_path)
        client = self.make()

        warns = self.take(client)

        self.assert_victim_untouched()
        self.assertEqual(len(warns), 1)
        self.assertIn(str(self.lock_path), warns[0])
        self.assertIsNone(client._instance_lock_fd)

    def test_a_dangling_symlink_creates_nothing(self):
        """O_CREAT through a dangling link would make root create the file the
        link names — an attacker-chosen path. RED before the fix."""
        target = self.dir / "created-by-root"
        os.symlink(target, self.lock_path)
        client = self.make()

        warns = self.take(client)

        self.assertFalse(os.path.lexists(target),
                         "root created a file at the symlink's target")
        self.assertEqual(len(warns), 1)
        self.assertIsNone(client._instance_lock_fd)

    def test_a_hard_link_to_another_file_is_never_written(self):
        """A second name for someone else's file is not our lock file either:
        st_nlink must be 1. RED before the fix."""
        os.link(self.victim, self.lock_path)
        client = self.make()

        warns = self.take(client)

        self.assert_victim_untouched()
        self.assertEqual(len(warns), 1)
        self.assertIn("link", warns[0])

    def test_a_foreign_owned_file_its_owner_keeps_flocked_is_not_a_second_copy(self):
        """The owner check runs BEFORE the flock: a file another uid owns (and
        holds) is refused as untrusted — WARN and run unguarded — never read as
        «another instance holds the lock». RED before the fix: SystemExit(75).
        The uid is made foreign by patching geteuid, so this runs as any user;
        the case below does the same with a real chown where it can."""
        self.lock_path.write_bytes(b"4242\n")
        os.chmod(self.lock_path, 0o600)
        holder = None
        if tel.fcntl is not None:
            holder = os.open(str(self.lock_path), os.O_RDWR)
            tel.fcntl.flock(holder, tel.fcntl.LOCK_EX | tel.fcntl.LOCK_NB)
            self.addCleanup(os.close, holder)
        client = self.make()

        with mock.patch.object(tel.os, "geteuid",
                               return_value=self.lock_path.stat().st_uid + 1):
            warns = self.take(client)

        self.assertEqual(self.lock_path.read_bytes(), b"4242\n")
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(warns), 1)
        self.assertIn("owned by uid", warns[0])
        self.assertIsNone(client._instance_lock_fd)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0,
                         "a real chown to another uid needs root")
    def test_a_really_chowned_file_is_not_trusted(self):
        """The same with the kernel's own uid, as the board would see it."""
        self.lock_path.write_bytes(b"4242\n")
        os.chown(self.lock_path, 65534, 65534)
        client = self.make()

        warns = self.take(client)

        self.assertEqual(self.lock_path.read_bytes(), b"4242\n")
        self.assertEqual(self.lock_path.stat().st_uid, 65534)
        self.assertEqual(len(warns), 1)

    def test_our_own_leftover_file_is_still_taken(self):
        """Non-vacuity for every refusal above: a regular, single-link file of
        our own — what a previous instance leaves behind — is taken, and the
        pid hint rewritten."""
        self.lock_path.write_bytes(b"999999\n")
        client = self.make()

        client._take_instance_lock()
        self.addCleanup(self._release, client)

        self.assertIsNotNone(client._instance_lock_fd)
        self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))


@unittest.skipUnless(os.name == "posix" and tel.fcntl is not None,
                     "file modes, flock and setuid are POSIX")
class TestTheInstanceLockCannotBeSquatted(SessionTestCase):
    """Review round 2, N1 (1.0.6.68). The trust check above refuses a lock file
    another uid OWNS; it did nothing about one another uid can merely OPEN.
    flock(2) takes LOCK_EX through a read-only fd, so while the daemon created
    its lock 0644 (and re-applied 0644 on every start) any local uid could open
    it, hold the lock, and turn every later start into «another instance holds
    … exiting with status 75» — Restart=on-failure makes that a permanent loop,
    and every OTA restarts the unit (reproduced on WSL with `su nobody`).

    Now the file is created 0600 and narrowed to 0600 through the fd BEFORE the
    flock, so a leftover 0644 from an earlier build is closed on the first
    start — even one that ends in a legitimate exit 75.

    What this cannot undo, stated rather than implied: a uid that opened a
    wider leftover file BEFORE that first narrowing keeps its fd, and with it
    the ability to hold the lock until it closes it or the board reboots
    (/run/lock is a tmpfs). Only a board that ran a pre-fix build of this
    branch can carry such a file.
    """

    NOBODY = 65534

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Sticky and world-writable like /run/lock, so a refusal below comes
        # from the FILE's mode — a 0700 temp dir would refuse `nobody` for the
        # wrong reason and the case would pass while testing nothing.
        os.chmod(self._tmp.name, 0o1777)
        self.lock_path = Path(self._tmp.name) / "sa02m-telemetry.lock"
        os.environ[LOCK_ENV] = str(self.lock_path)

    def _release(self, client):
        fd = getattr(client, "_instance_lock_fd", None)
        if fd is not None:
            os.close(fd)
            client._instance_lock_fd = None

    def as_nobody(self, hold: bool):
        """In a forked child running as uid/gid 65534: open the lock file
        read-only and, when `hold`, take LOCK_EX|LOCK_NB and keep it.

        Returns (result, release): result is 0 when the child got the fd (and
        the lock, if asked), else the errno it hit; release() ends the child,
        which drops whatever it held."""
        res_r, res_w = os.pipe()
        go_r, go_w = os.pipe()
        pid = os.fork()
        if pid == 0:                                    # the squatter
            code = b"-1"
            try:
                os.close(res_r)
                os.close(go_w)
                os.setgroups([])
                os.setgid(self.NOBODY)
                os.setuid(self.NOBODY)
                try:
                    fd = os.open(str(self.lock_path), os.O_RDONLY)
                except OSError as exc:
                    code = str(exc.errno).encode()
                else:
                    if hold:
                        tel.fcntl.flock(fd, tel.fcntl.LOCK_EX | tel.fcntl.LOCK_NB)
                    code = b"0"
                os.write(res_w, code)
                os.read(go_r, 1)                        # hold until released
            finally:
                os._exit(0)
        os.close(res_w)
        os.close(go_r)
        result = int(os.read(res_r, 16) or b"-1")
        os.close(res_r)
        released = []

        def release():
            if not released:
                released.append(True)
                os.close(go_w)
                os.waitpid(pid, 0)
        self.addCleanup(release)
        return result, release

    def take_and_exit(self):
        """A first daemon takes the lock and exits (its fd, and flock, gone)."""
        client = self.make()
        client._take_instance_lock()
        self.assertIsNotNone(client._instance_lock_fd)
        self._release(client)

    @unittest.skipUnless(IS_ROOT, "setuid to nobody needs root")
    def test_nobody_cannot_even_open_the_lock_file(self):
        """RED before the fix: the open succeeded (result 0, not EACCES)."""
        import errno
        self.take_and_exit()

        result, release = self.as_nobody(hold=False)
        release()

        self.assertEqual(result, errno.EACCES,
                         "another uid could open the instance lock — and a "
                         "read-only fd is enough to flock it")

    @unittest.skipUnless(IS_ROOT, "setuid to nobody needs root")
    def test_a_squatter_cannot_keep_the_service_down(self):
        """The reviewer's scenario end to end: root takes the lock and exits,
        `nobody` tries to hold it, root starts again. RED before the fix:
        SystemExit 75, the permanent restart loop."""
        self.take_and_exit()
        result, release = self.as_nobody(hold=True)

        client = self.make()
        try:
            client._take_instance_lock()
        except SystemExit as exc:
            self.fail(f"a lock held by uid {self.NOBODY} made the daemon exit "
                      f"{exc.code} (squatter open result {result})")
        finally:
            release()
        self.addCleanup(self._release, client)
        self.assertIsNotNone(client._instance_lock_fd)

    def test_a_leftover_world_readable_file_is_narrowed(self):
        """A 0644 file left by an earlier build: taken, and 0600 after.
        RED before the fix: the daemon re-applied 0644 itself."""
        self.lock_path.write_bytes(b"1\n")
        os.chmod(self.lock_path, 0o644)
        client = self.make()

        client._take_instance_lock()
        self.addCleanup(self._release, client)

        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)

    def test_the_narrowing_happens_before_the_flock(self):
        """Even a start that ends in a legitimate exit 75 (another copy of the
        daemon holds the lock) leaves the file 0600, so the next start cannot
        find it open to everyone. Pins the ORDER: a narrowing moved after the
        flock would never run on this path. RED before the fix (0644)."""
        self.lock_path.write_bytes(b"1\n")
        os.chmod(self.lock_path, 0o644)
        holder = os.open(str(self.lock_path), os.O_RDWR)
        self.addCleanup(os.close, holder)
        tel.fcntl.flock(holder, tel.fcntl.LOCK_EX | tel.fcntl.LOCK_NB)
        client = self.make()

        with self.assertLogs(tel.log, level="ERROR"):
            with self.assertRaises(SystemExit) as ctx:
                client._take_instance_lock()

        self.assertEqual(ctx.exception.code, tel.EXIT_ANOTHER_INSTANCE)
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)

    def test_a_failed_narrowing_is_logged_and_the_lock_is_still_taken(self):
        """D2: fchmod failing is one warning, then the start continues.
        RED before the warning: assertLogs finds nothing at WARNING."""
        def boom(*_a, **_k):
            raise OSError(1, "fchmod refused")

        client = self.make()
        with mock.patch.object(tel.os, "fchmod", boom):
            with self.assertLogs(tel.log, level="WARNING") as caught:
                client._take_instance_lock()
        self.addCleanup(self._release, client)

        self.assertIsNotNone(client._instance_lock_fd)
        self.assertTrue(
            any("not narrowed" in line for line in caught.output),
            caught.output)

    def test_a_new_lock_is_created_0600_when_fchmod_does_nothing(self):
        """D3: the create mode is 0600 on its own. A no-op fchmod leaves
        whatever os.open used. RED if the open mode is 0644 and fchmod is
        what narrows a leftover — this file did not exist, so only the
        open mode can make it 0600."""
        self.assertFalse(self.lock_path.exists())

        def noop(*_a, **_k):
            return None

        client = self.make()
        with mock.patch.object(tel.os, "fchmod", noop):
            client._take_instance_lock()
        self.addCleanup(self._release, client)

        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)


# ── Item 2: the eviction detector ────────────────────────────────────────────
class TestTheEvictionDetector(SessionTestCase):
    """Three short sessions are a storm; one long one is a broker restart."""

    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        p = mock.patch.object(tel.time, "monotonic", lambda: self.now[0])
        p.start()
        self.addCleanup(p.stop)
        self.client = self.make()
        self.paho = self.client._client

    def session(self, seconds: float) -> None:
        """One connect → disconnect pair lasting `seconds` (v1 callback shape)."""
        self.client._on_connect(self.paho, None, {}, 0)
        self.now[0] += seconds
        self.client._on_disconnect(self.paho, None, 0)

    def storm_lines(self, output):
        return [ln for ln in output if "another client is using client id" in ln]

    def test_the_constants_are_the_planned_ones(self):
        self.assertEqual(tel.SHORT_SESSION_S, 10.0)
        self.assertEqual(tel.STORM_SESSIONS, 3)
        self.assertEqual((tel.NORMAL_MIN_DELAY_S, tel.STORM_MIN_DELAY_S,
                          tel.RECONNECT_MAX_DELAY_S), (1, 10, 120))
        self.assertEqual(tel.HEALTHY_SESSION_S, 60.0)
        self.assertEqual(tel.STORM_WARN_EVERY_S, 60.0)

    def test_three_short_sessions_raise_the_delay_once_and_warn_once(self):
        """T2a. RED before 1.0.6.68: _on_disconnect only logged the plain line."""
        with self.assertLogs(tel.log, level="WARNING") as caught:
            for _ in range(3):
                self.session(2.0)

        storm = self.storm_lines(caught.output)
        self.assertEqual(len(storm), 1, caught.output)
        self.assertIn(repr(self.paho.client_id), storm[0],
                      "the WARN must name the client id the intruder is using")
        self.assertIn(STORM_PHRASE, storm[0],
                      "the WARN must quote the broker's own phrase, so the two "
                      "logs can be matched by hand")
        self.assertIn(f"{tel.MQTT_BROKER}:{tel.MQTT_PORT}", storm[0])
        self.assertEqual(self.paho.delay_calls,
                         [(tel.STORM_MIN_DELAY_S, tel.RECONNECT_MAX_DELAY_S)])

    def test_two_short_sessions_are_not_yet_a_storm(self):
        with self.assertLogs(tel.log, level="WARNING") as caught:
            self.session(2.0)
            self.session(2.0)
        self.assertEqual(self.storm_lines(caught.output), [])
        self.assertEqual(self.paho.delay_calls, [])

    def test_a_fourth_short_session_inside_a_minute_does_not_warn_again(self):
        """The WARN is rate-limited; the delay is raised once, not per eviction."""
        with self.assertLogs(tel.log, level="WARNING") as caught:
            for _ in range(4):
                self.session(2.0)
        self.assertEqual(len(self.storm_lines(caught.output)), 1, caught.output)
        self.assertEqual(len(self.paho.delay_calls), 1)

    def test_the_warning_repeats_once_a_minute_while_the_storm_persists(self):
        """Bounded journal volume: at the 10 s cadence the fight is ≤6
        evictions/min, and the named WARN one line per minute on top."""
        with self.assertLogs(tel.log, level="WARNING") as caught:
            for _ in range(40):          # 80 s of 2 s sessions
                self.session(2.0)
        self.assertEqual(len(self.storm_lines(caught.output)), 2, caught.output)
        self.assertEqual(len(self.paho.delay_calls), 1)

    def test_a_healthy_session_restores_the_delay_and_says_so(self):
        """T2a's tail: a session of 61 s ⇒ reconnect_delay_set(1, 120) + INFO."""
        for _ in range(3):
            self.session(2.0)
        self.paho.delay_calls.clear()

        with self.assertLogs(tel.log, level="INFO") as caught:
            self.session(61.0)

        self.assertEqual(self.paho.delay_calls,
                         [(tel.NORMAL_MIN_DELAY_S, tel.RECONNECT_MAX_DELAY_S)])
        self.assertTrue(any("reconnect delay back to" in ln for ln in caught.output),
                        caught.output)

    def test_a_medium_session_resets_the_count_but_keeps_the_storm_delay(self):
        """Between SHORT_SESSION_S and HEALTHY_SESSION_S: not an eviction, not
        yet health. The count restarts; the 10 s floor stays until a session
        outlives HEALTHY_SESSION_S — the number the WARN itself promises."""
        for _ in range(3):
            self.session(2.0)
        self.session(30.0)
        self.assertEqual(self.paho.delay_calls,
                         [(tel.STORM_MIN_DELAY_S, tel.RECONNECT_MAX_DELAY_S)])
        self.assertEqual(self.client._short_sessions, 0)
        self.assertTrue(self.client._storm_active)

    def test_a_broker_restart_never_trips_the_detector(self):
        """Two long sessions, one reconnect between them: the counter is 0 and
        the delay untouched — a restart still reconnects after 1 s."""
        self.session(3600.0)
        self.session(3600.0)
        self.assertEqual(self.paho.delay_calls, [])
        self.assertEqual(self.client._short_sessions, 0)
        self.assertFalse(self.client._storm_active)

    def test_a_disconnect_after_stop_counts_nothing(self):
        """T2b. run() disconnects on purpose at shutdown; that is not a session
        lost and must not leave a WARN behind in the last lines of the journal."""
        for _ in range(2):
            self.session(2.0)
        tel._stop.set()
        try:
            with self.assertLogs(tel.log, level="WARNING") as caught:
                self.session(2.0)
        finally:
            tel._stop.clear()
        warns = [ln for ln in caught.output if ln.startswith("WARNING")]
        self.assertEqual(len(warns), 1, caught.output)
        self.assertIn("MQTT disconnected", warns[0])
        self.assertEqual(self.storm_lines(caught.output), [])
        self.assertEqual(self.paho.delay_calls, [])
        self.assertEqual(self.client._short_sessions, 2)

    def test_v1_and_v2_callback_shapes_count_the_same(self):
        """Durations only: the reason-code object never enters the decision,
        so the (client, userdata, rc) and (client, userdata, flags, reason,
        properties) shapes behave identically."""
        reason = types.SimpleNamespace(is_failure=True, value=7)
        for _ in range(3):
            self.client._on_connect(self.paho, None, {}, 0)
            self.now[0] += 2.0
            self.client._on_disconnect(self.paho, None, {}, reason, None)
        self.assertEqual(self.paho.delay_calls,
                         [(tel.STORM_MIN_DELAY_S, tel.RECONNECT_MAX_DELAY_S)])

    def test_a_disconnect_with_no_session_behind_it_counts_nothing(self):
        """A refused CONNACK reaches on_disconnect without an on_connect; that
        is not an eviction and must not count towards one."""
        for _ in range(3):
            self.client._on_disconnect(self.paho, None, 0)
        self.assertEqual(self.paho.delay_calls, [])
        self.assertEqual(self.client._short_sessions, 0)


# ── Item 3: the first-connect loop ───────────────────────────────────────────
class RecordingStop:
    """A threading.Event stand-in that records wait() and can set itself."""

    def __init__(self, set_on_wait: int | None = None):
        self.waits: list = []
        self._set = False
        self.set_on_wait = set_on_wait

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout=None) -> bool:
        self.waits.append(timeout)
        if self.set_on_wait is not None and len(self.waits) >= self.set_on_wait:
            self._set = True
        return self._set

    def set(self):
        self._set = True

    def clear(self):
        self._set = False


class TestTheFirstConnectLoop(SessionTestCase):
    def test_retries_wait_on_the_stop_event_and_start_the_loop_once(self):
        """T3. RED before 1.0.6.68: the retry slept with time.sleep(5) — a
        SIGTERM while the broker was down waited the sleep out."""
        client = self.make()
        client._client.connect_failures = 2
        stop = RecordingStop()

        with mock.patch.object(tel, "_stop", stop), \
                mock.patch.object(tel.time, "sleep") as sleep:
            with self.assertLogs(tel.log, level="ERROR"):
                client.connect()

        self.assertEqual(stop.waits, [5, 5],
                         "the retry pause must be the stop event's wait(5)")
        self.assertEqual(client._client.connect_calls, 3)
        self.assertEqual(client._client.loop_starts, 1,
                         "loop_start() must run exactly once")
        sleep.assert_not_called()

    def test_a_stop_during_the_wait_returns_without_starting_the_loop(self):
        client = self.make()
        client._client.connect_failures = 5
        stop = RecordingStop(set_on_wait=1)

        with mock.patch.object(tel, "_stop", stop), \
                mock.patch.object(tel.time, "sleep"):
            with self.assertLogs(tel.log, level="ERROR"):
                client.connect()

        self.assertEqual(stop.waits, [5])
        self.assertEqual(client._client.connect_calls, 1)
        self.assertEqual(client._client.loop_starts, 0,
                         "a stopped daemon started paho's loop anyway")


if __name__ == "__main__":
    unittest.main()

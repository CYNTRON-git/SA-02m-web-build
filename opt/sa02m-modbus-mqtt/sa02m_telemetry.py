#!/usr/bin/env python3
"""SA-02m system telemetry → MQTT.

Publishes CPU, RAM, temperature, uptime, RS-485 stats.
Subscribes to /devices/<device-id>/controls/{do,beeper,alarm_led}/on and drives
the PCA9536 I2C expander via i2cset. Which pin each channel occupies, and its
polarity, come from /etc/sa02m_hw.conf — the same file the web CGI reads. They
are never hard-coded here: a second copy of that map is what made a `beeper`
command switch the discrete output until 1.0.6.42. The expander is shared with
the web CGI, the beeper override worker and MPLC4, so every access takes the
same flock and honours the same owner gate they do — see the shared-bus section
below. One channel does not simply refuse when that gate says the bus is held:
since 1.0.6.43 a `beeper` command falls back to the override file the panel
button has always used, so a cloud «beep» sounds while the PLC keeps the bus.

Device ID: the board name itself (the hostname, e.g. ``SA-02m``) — no prefix,
resolved by :func:`_resolve_device_id`. Topic canon: docs/MQTT_TOPICS.md.
"""

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import signal
import socket
import logging
import ipaddress
import subprocess
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:      # non-Linux dev host; see _with_bus_lock's fallback
    fcntl = None

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt not installed: pip3 install paho-mqtt")

# ── Config ────────────────────────────────────────────────────────────────────
MQTT_BROKER = os.environ.get("SA02M_MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.environ.get("SA02M_MQTT_PORT", "1883"))
MQTT_QOS = 1
POLL_INTERVAL_S = 30
DEVICE_BASE = "/devices"
SERIAL_COUNT = int(os.environ.get("SA02M_SERIAL_COUNT", "5"))

# ── Device id ────────────────────────────────────────────────────────────────
# The id is the board name. Resolution order, first VALID wins:
#   $SA02M_TELEMETRY_DEVICE_ID → /etc/sa02m_telemetry.conf → hostname → SA-02m.
# The conf file is created by nothing: it exists only if an integrator pins the
# id so a later `hostnamectl set-hostname` cannot silently orphan every binding.
# It is read here rather than wired as a systemd EnvironmentFile= because
# etc/sa02m-telemetry.service is not OTA-deployable — a unit-file change would
# never reach a field board.
DEVICE_ID_ENV = "SA02M_TELEMETRY_DEVICE_ID"
DEVICE_ID_CONF = os.environ.get("SA02M_TELEMETRY_CONF", "/etc/sa02m_telemetry.conf")
DEVICE_ID_FALLBACK = "SA-02m"
# Allow-list, not a sanity check: the id becomes an MQTT topic segment, so a
# value carrying `/` would re-shape the topic tree and a `+`/`#` would turn our
# own subscribe into a wildcard over every device on the broker. Charset matches
# the Alice _ID_RE (models.py) exactly. Known narrower consumer, recorded rather
# than assumed: mqtt_set.cgi allows [a-zA-Z0-9._-] — no `:` — so an id pinned
# with a colon would be accepted here and refused there as `bad_device`. Not
# reachable today (the panel's DO/beeper buttons go to hw_set.cgi -> i2c and
# never name this id), so the divergence is documented, not designed around.
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
TELEMETRY_DRIVER = "sa02m-telemetry"
MQTT_CLIENT_ID_MAX = 22          # MQTT 3.1 caps at 23; the package slices to 22

# ── Legacy retained clear (1.0.6.22 migration) ───────────────────────────────
# Before 1.0.6.22 the id was the hostname glued behind a fixed prefix, so the
# old subtree has no publisher after the rename and stays in the broker looking
# alive — that is exactly how a stale retained `1` made the app show the
# opposite of the hardware. Ride the producer itself rather than an OTA
# migration: the update runner re-execs the ALREADY-INSTALLED runner, so a
# migration added in this branch would not run during the update that delivers
# it. See docs/MQTT_TOPICS.md for the manual equivalent.
LEGACY_ID_PREFIX = "sa02m-"
LEGACY_HOSTNAMES = ("SA-02", "SA-02m")   # the only values scripts/01-system.sh ever set
LEGACY_CLEAR_COLLECT_S = 3.0
LEGACY_CLEAR_MAX_TOPICS = 500
LEGACY_CLEAR_CONNECT_WAIT_S = 5.0
LOOPBACK_NAMES = ("localhost", "ip6-localhost", "ip6-loopback")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [telemetry] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("telemetry")

_stop = threading.Event()


def sd_notify(msg: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    import socket as _s
    try:
        with _s.socket(_s.AF_UNIX, _s.SOCK_DGRAM) as s:
            s.connect(sock_path)
            s.sendall(msg.encode())
    except Exception:
        pass


def _valid_device_id(value: str) -> bool:
    return bool(DEVICE_ID_RE.match(value))


def _hostname() -> str:
    """The one home for 'how this service reads the board name'."""
    try:
        return (socket.gethostname() or "").strip()
    except Exception:
        return ""


def _read_conf_value(path: str, key: str) -> str:
    """`KEY=VALUE` lookup in a shell-style conf; '' when absent or unreadable.

    Last assignment wins (shell semantics). An inline `#` comment is NOT
    stripped from an unquoted value: a `#` cannot occur in a valid id anyway,
    so the whole line is rejected by the allow-list instead of being guessed at.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    found = ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, raw = line.partition("=")
        if not sep or name.strip() != key:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
            raw = raw[1:-1]
        found = raw.strip()
    return found


def _resolve_device_id() -> tuple[str, str]:
    """Return (device id, the source it came from).

    Fail closed: a present-but-invalid value is rejected with a WARN naming its
    source and resolution falls through to the next one. An absent/empty source
    is simply "not set" and stays silent.
    """
    candidates = (
        ("env " + DEVICE_ID_ENV, os.environ.get(DEVICE_ID_ENV, "")),
        (DEVICE_ID_CONF, _read_conf_value(DEVICE_ID_CONF, DEVICE_ID_ENV)),
        ("hostname", _hostname()),
    )
    for source, raw in candidates:
        value = (raw or "").strip()
        if not value:
            continue
        if not _valid_device_id(value):
            log.warning("device id from %s rejected (invalid): %r", source, value)
            continue
        return value, source
    return DEVICE_ID_FALLBACK, "fallback"


def get_device_id() -> str:
    return _resolve_device_id()[0]


def _legacy_device_ids(current: str) -> list[str]:
    """The deterministic pre-1.0.6.22 id set, minus the current id.

    The id was always `<prefix><hostname>` and scripts/01-system.sh only ever
    sets the hostname to the vendor defaults — a custom operator hostname is
    covered by the first entry. A board whose hostname changed AFTER telemetry
    had already published is the one case no formula recovers; the manual
    command in docs/MQTT_TOPICS.md covers it.
    """
    out: list[str] = []
    for name in (_hostname(),) + LEGACY_HOSTNAMES:
        if not name:
            continue
        legacy = LEGACY_ID_PREFIX + name
        # A legacy id becomes a subscribe FILTER — allow-list it too, or a
        # hostname carrying `+`/`#` would widen the subscribe past our subtree.
        if legacy == current or legacy in out or not _valid_device_id(legacy):
            continue
        out.append(legacy)
    return out


def _broker_is_loopback(broker: str) -> bool:
    """True only when the broker is provably this board's own.

    The ownership floor for the legacy clear: since 1.0.5.69 every board carries
    the same hostname, so the legacy id is the SAME STRING on every board — on a
    shared external broker a clear would wipe a neighbour's still-live subtree.
    No DNS resolution: an unresolved name is "not proven", never "probably ok".
    """
    value = (broker or "").strip().strip("[]").lower()
    if not value:
        return False
    if value in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _payload_text(payload) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace").strip()
    return str(payload or "").strip()


def _clear_one_legacy(client, legacy_id: str, collect_s: float) -> tuple[str, int]:
    """Collect and clear one legacy subtree. Returns (verdict, topic count)."""
    prefix = f"{DEVICE_BASE}/{legacy_id}/"
    topic_filter = prefix + "#"
    lock = threading.Lock()
    seen: dict[str, bytes] = {}
    capped = [False]

    def _collect(_client, _userdata, msg):
        if not getattr(msg, "retain", False):
            return                      # a live message is not ours to erase
        topic = getattr(msg, "topic", "") or ""
        if not topic.startswith(prefix):
            return                      # defence in depth: never outside the subtree
        with lock:
            if len(seen) >= LEGACY_CLEAR_MAX_TOPICS:
                capped[0] = True        # a runaway tree must not stall startup
                return
            seen.setdefault(topic, msg.payload or b"")

    try:
        client.message_callback_add(topic_filter, _collect)
        client.subscribe(topic_filter, qos=MQTT_QOS)
        time.sleep(collect_s)
    finally:
        for undo in (
            lambda: client.unsubscribe(topic_filter),
            lambda: client.message_callback_remove(topic_filter),
        ):
            try:
                undo()
            except Exception:
                pass

    with lock:
        collected = dict(seen)
        was_capped = capped[0]
    if was_capped:
        log.warning(
            "legacy retained %s: collection capped at %d topics",
            legacy_id, LEGACY_CLEAR_MAX_TOPICS,
        )
    if not collected:
        log.info("legacy retained %s — nothing to clear", legacy_id)
        return ("empty", 0)
    # Second, positive proof of ownership: the subtree must carry OUR driver
    # marker. Loopback says the broker is this board's; this says the subtree
    # was published by this board's telemetry. Ambiguous evidence ⇒ leave it.
    if _payload_text(collected.get(prefix + "meta/driver")) != TELEMETRY_DRIVER:
        log.warning(
            "legacy retained NOT cleared: %s — ownership unproven (no retained "
            "meta/driver == %s); %d topic(s) left in place, clear by hand per "
            "docs/MQTT_TOPICS.md",
            legacy_id, TELEMETRY_DRIVER, len(collected),
        )
        return ("unproven", len(collected))
    for topic in sorted(collected):
        client.publish(topic, "", qos=MQTT_QOS, retain=True)
    log.info("legacy retained cleared: %s — %d topics", legacy_id, len(collected))
    return ("cleared", len(collected))


def clear_legacy_retained(
    client, device_id: str, broker: str, collect_s: float | None = None,
) -> dict[str, tuple[str, int]]:
    """Clear this board's own orphaned pre-1.0.6.22 retained subtree.

    Returns {legacy id: (verdict, topic count)}; verdicts are `cleared`,
    `empty`, `unproven`, `not-loopback`. Fail closed on every ambiguity — a
    stale topic is recoverable, a wiped neighbour is not.
    """
    if collect_s is None:
        collect_s = LEGACY_CLEAR_COLLECT_S
    legacy_ids = _legacy_device_ids(device_id)
    result: dict[str, tuple[str, int]] = {}
    if not legacy_ids:
        return result
    if not _broker_is_loopback(broker):
        for legacy_id in legacy_ids:
            result[legacy_id] = ("not-loopback", 0)
        log.warning(
            "legacy retained NOT cleared: broker %s is not loopback, so this "
            "board cannot prove the subtree is its own (%s) — clear by hand per "
            "docs/MQTT_TOPICS.md",
            broker, ", ".join(legacy_ids),
        )
        return result
    for legacy_id in legacy_ids:
        result[legacy_id] = _clear_one_legacy(client, legacy_id, collect_s)
    return result


# ── Hardware control (PCA9536, wired per /etc/sa02m_hw.conf) ─────────────────
def _i2cget(bus: int, addr: int, reg: int) -> int | None:
    try:
        result = subprocess.run(
            ["i2cget", "-y", str(bus), hex(addr), hex(reg)],
            capture_output=True, text=True, timeout=1
        )
        if result.returncode == 0:
            return int(result.stdout.strip(), 16)
    except Exception:
        pass
    return None


def _i2cset(bus: int, addr: int, reg: int, value: int) -> bool:
    try:
        result = subprocess.run(
            ["i2cset", "-y", str(bus), hex(addr), hex(reg), hex(value)],
            capture_output=True, timeout=1
        )
        return result.returncode == 0
    except Exception:
        return False


# ── The shared bus: the flock and the owner gate lib_hw.sh already runs ──────
# 1.0.6.42. The PCA9536's output port is ONE byte with four owners: this
# daemon, the web CGI (www/network_config/cgi-bin/lib_hw.sh), the beeper
# override worker (etc/sa02m-beeper-override.sh) and MPLC4/KLogic. Driving one
# channel is a read-modify-write, so two owners interleaving between the read
# and the write lose one of the two commands outright — and one of the bits is
# the discrete output that commutes real equipment. The CGI closes that with an
# flock on SA02M_I2C_LOCK_FILE plus a refusal while an owner unit or process
# holds the bus. This daemon took neither until now, so the widened window the
# read-modify-write opened was a window nobody was guarding.
#
# Everything below mirrors that policy, function by function, and the values
# come from the same one home, /etc/sa02m_hw.conf. The constants here are the
# fallbacks for a board whose conf is absent or blank; they are lib_hw.sh's own
# `:-` defaults, and tests/test_telemetry_hw_lock.py reads lib_hw.sh and fails
# when they diverge — a copy that cannot drift unnoticed is the closest a
# Python daemon gets to reading a shell file's defaults.
HW_LOCK_FILE_DEFAULT = "/run/lock/sa02m-pca9536.lock"    # SA02M_I2C_LOCK_FILE
HW_LOCK_WAIT_SEC_DEFAULT = 1.0                           # SA02M_I2C_LOCK_WAIT_SEC
HW_OWNER_UNITS_DEFAULT = (                               # SA02M_I2C_OWNER_UNITS
    "mplc.service", "mplc4.service", "klogic.service", "klogicd.service",
)
HW_OWNER_PROCS_DEFAULT = (                               # SA02M_I2C_OWNER_PROCS
    "mplc", "mplc4", "klogic", "klogicd", "klogic-sa02",
)
# lib_hw.sh sa02m_hw_i2c_owner_active()'s literal case list, mirrored rather
# than normalised: a conf reading `False` does not switch the gate off in the
# shell either, and a daemon that lower-cased it would go quiet exactly where
# the CGI still writes.
HW_RESPECT_OWNER_OFF = ("0", "no", "false", "off", "OFF", "N")
HW_OWNER_PROBE_TIMEOUT_S = 2.0
# Python's flock() has no timeout and signal.alarm is main-thread-only, while
# these calls run on paho's network thread — so the bounded wait is a poll.
HW_LOCK_POLL_S = 0.02

# ── The one channel that pre-empts a busy bus (1.0.6.43) ────────────────────
# The Operator's decision, not an agent's: a cloud/Alice «beep» takes the same
# fallback the panel button has always taken. While an owner holds the
# expander, a `beeper` command is written to SA02M_BEEPER_OVERRIDE_FILE and
# SA02M_BEEPER_OVERRIDE_WORKER is started to apply it under the bus lock, so
# the buzzer sounds without this daemon touching the PLC's byte. Mirror of
# lib_hw.sh sa02m_hw_i2c_write_channel_web / _beeper_override_write /
# _beeper_override_start_worker; these defaults are that file's `:-` values and
# the drift alarm in tests/test_telemetry_hw_lock.py fails when they diverge.
# ONLY `beeper`: `do` commutes real equipment and `alarm_led` is not a 7 s
# pulse, and the panel refuses both of them on a held bus too.
HW_BEEPER_CHANNEL = "beeper"
HW_BEEPER_OVERRIDE_SEC_DEFAULT = 7        # SA02M_BEEPER_WEB_OVERRIDE_SEC
HW_BEEPER_OVERRIDE_FILE_DEFAULT = "/run/sa02m-hw-override/beeper.env"
HW_BEEPER_OVERRIDE_WORKER_DEFAULT = "/usr/local/sbin/sa02m-beeper-override.sh"

# Returned in place of a result when the bus lock could not be taken. A
# sentinel rather than None: None is what a failed i2cget returns, and the two
# refusals need different words in the journal.
BUS_BUSY = object()

_flock_missing_warned = False


def _probe_available(tool: str) -> bool:
    """Mirror of lib_hw.sh's `command -v <tool>` guards around the owner gate."""
    return shutil.which(tool) is not None


def _run_probe(argv: list[str]) -> int | None:
    """Exit status of a short owner probe; None when it gave no answer.

    No answer = the command could not be spawned, or it hung past
    HW_OWNER_PROBE_TIMEOUT_S. What that means is the caller's to decide.
    """
    try:
        return subprocess.run(argv, capture_output=True,
                              timeout=HW_OWNER_PROBE_TIMEOUT_S).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _hw_owner_active(profile: "HwProfile") -> str | None:
    """The other owner holding the expander, or None when the bus is free.

    Mirror of lib_hw.sh sa02m_hw_i2c_owner_active(): SA02M_I2C_RESPECT_OWNER
    switches it off, units are matched with `systemctl is-active` and only when
    the name ends in `.service`, processes with `pgrep -x`, first hit wins.

    One deliberate divergence, in the safe direction: where the shell would sit
    on a wedged systemctl, an unanswered probe returns a reason here and the
    caller refuses. These writes run on paho's single network thread, so
    blocking there takes MQTT down with them, and writing on a bus we could not
    prove is free is the one outcome worse than refusing.
    """
    if not profile.respect_owner:
        return None

    if _probe_available("systemctl"):
        for unit in profile.owner_units:
            if not unit.endswith(".service"):
                continue
            rc = _run_probe(["systemctl", "is-active", "--quiet", unit])
            if rc == 0:
                return unit
            if rc is None:
                return f"systemctl is-active {unit} gave no answer"

    if _probe_available("pgrep"):
        for proc in profile.owner_procs:
            if not proc:
                continue
            rc = _run_probe(["pgrep", "-x", proc])
            if rc == 0:
                return proc
            if rc is None:
                return f"pgrep -x {proc} gave no answer"

    return None


def _bus_busy_reason(profile: "HwProfile") -> str:
    if profile.lock_wait_s is None:
        return (f"unparseable SA02M_I2C_LOCK_WAIT_SEC in {profile.source} — "
                "refusing rather than driving a shared bus on a guessed wait")
    return (f"{profile.lock_file} stayed held by another owner for the whole "
            f"{profile.lock_wait_s:g}s wait")


def _beeper_override_write(profile: "HwProfile", on: bool) -> tuple[bool, str]:
    """Stage the buzzer's override file and swap it in. (ok, detail-on-failure).

    Mirror of lib_hw.sh sa02m_hw_beeper_override_write: `value=<0|1>` and
    `expires_at=<now+ttl>`, written under a temp name in the SAME directory and
    renamed over the live path.

    The atomicity is not decoration. Since 1.0.6.43 this file has TWO
    producers — the web CGI and this daemon — and one consumer that SOURCES it
    (etc/sa02m-beeper-override.sh). A reader catching a half-written file would
    take a `value=` with no `expires_at`, which its own validation reads as
    «no override» — a beep that silently never sounds. os.replace is the same
    rename-over-in-place the CGI's `mv -f` performs.

    Last writer wins, carrying its own TTL: a cloud beep landing during a panel
    beep replaces it, and neither corrupts the other. This daemon never reads
    the file back — the value it just wrote proves nothing about the pin — and
    never deletes it; expiry is the worker's business, by the timestamp.
    """
    path = profile.beeper_override_file
    directory = os.path.dirname(path) or "."
    # The directory's real home is the tmpfiles.d entry in
    # scripts/03-webserver.sh (`d /run/sa02m-hw-override 0775 www-data
    # www-data`), which recreates it on every boot of a board that has the
    # feature at all. This makedirs is the CGI's own fallback, mirrored: if it
    # ever fires here the directory ends up root-owned, and www-data's CGI
    # would then be unable to stage its temp file in it.
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return False, f"{path}: {exc}"
    try:
        os.chmod(directory, 0o775)
    except OSError:
        pass
    expires_at = int(time.time()) + profile.beeper_override_sec
    # The CGI's temp name is `${file}.$$` — unique because every request is its
    # own process. In one long-lived daemon the pid is shared, so the thread id
    # joins it: two commands staging the same name would have one of them
    # renaming a file the other had already moved away.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("value=%d\n" % (1 if on else 0))
            fh.write("expires_at=%d\n" % expires_at)
        # Before the rename, not after: os.replace carries the temp file's mode
        # onto the live path, so doing it in this order means the file is never
        # visible with root's umask-narrowed 0644 — which the other producer,
        # www-data, could not overwrite in place.
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"{path}: {exc}"
    return True, ""


def _beeper_override_start_worker(profile: "HwProfile") -> bool:
    """Start the override worker detached. True when it was spawned.

    Mirror of lib_hw.sh sa02m_hw_beeper_override_start_worker, including its
    `[ -x "$worker" ] || return 0`: an absent worker is NOT an error. The file
    is already on disk, so a worker installed later — or the panel's next
    click — applies it; failing the command instead would report a defect the
    operator cannot act on from the cloud side.

    Detached (`start_new_session` = the shell's `nohup … & disown`): these
    callbacks run on paho's network thread, and a child in this process group
    would be killed with the service on the next restart, leaving the buzzer
    latched with nobody left to honour its TTL.
    """
    worker = profile.beeper_override_worker
    if not os.path.isfile(worker) or not os.access(worker, os.X_OK):
        return False
    try:
        subprocess.Popen(
            [worker], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("the beeper override worker %s would not start: %s",
                    worker, exc)
        return False
    return True


def _open_lock_file(path: str) -> int | None:
    """The lock file's fd, created if missing; None when it cannot be opened."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        pass
    if not os.path.exists(path):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o666))
            os.chmod(path, 0o666)   # explicit: 0o666 above is cut by the umask
        except OSError:
            pass
    try:
        # Read-write and NEVER truncating, for the reason lib_hw.sh
        # sa02m_hw_i2c_with_lock spells out at its own `exec 9<>`: /run/lock is
        # a sticky tmpfs, so an O_TRUNC open of a lock file another uid created
        # fails with EACCES — www-data's CGI and this root daemon share it.
        return os.open(path, os.O_RDWR)
    except OSError as exc:
        log.warning("cannot open the I2C lock file %s: %s", path, exc)
        return None


def _flock_wait(fd: int, wait_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(HW_LOCK_POLL_S)


def _with_bus_lock(profile: "HwProfile", fn):
    """Run `fn` holding SA02M_I2C_LOCK_FILE; BUS_BUSY when it cannot be taken.

    Mirror of lib_hw.sh sa02m_hw_i2c_with_lock(): the same file, a BOUNDED wait
    of SA02M_I2C_LOCK_WAIT_SEC and then a refusal — never a longer wait, which
    on this thread would stall the MQTT keepalive as well as the command.

    `fn` runs ONCE with the lock held for the whole of it. That bracket is the
    point: the read and the write of a read-modify-write must not be separable
    by another owner, so the lock covers both or it covers nothing worth having.
    """
    if profile.lock_wait_s is None:
        return BUS_BUSY

    if fcntl is None:
        # Same fail-open as lib_hw.sh when the `flock` binary is missing — but
        # said out loud, once: an unlocked read-modify-write on this byte is a
        # real risk, not a footnote. No board is in this state (the daemon runs
        # on Linux); a dev host running the suite is.
        global _flock_missing_warned
        if not _flock_missing_warned:
            _flock_missing_warned = True
            log.warning("no flock on this host — the PCA9536 is driven UNLOCKED "
                        "(%s); the web CGI, MPLC4 and the beeper worker can "
                        "interleave with us on the same byte", profile.lock_file)
        return fn()

    fd = _open_lock_file(profile.lock_file)
    if fd is None:
        return BUS_BUSY
    try:
        if not _flock_wait(fd, profile.lock_wait_s):
            return BUS_BUSY
        try:
            return fn()
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


# ── The channel map: read from /etc/sa02m_hw.conf, never carried here ────────
# 1.0.6.42. This daemon used to hold `{"do": 0, "beeper": 1, "alarm_led": 2}`
# in code. The board is wired the other way round — bit0 = alarm LED, bit1 =
# DO, bit2 = buzzer — so a `beeper` command switched the DISCRETE OUTPUT, the
# one that commutes real equipment on an installation. It also drove the pin
# HIGH for "on" while every other consumer treats these outputs as ACTIVE-LOW,
# so each command was shifted AND inverted.
#
# The remedy is not a corrected constant — a second copy of the map is what
# drifted in the first place. `/etc/sa02m_hw.conf` is the ONE home and
# www/network_config/cgi-bin/lib_hw.sh is the reference consumer; the parsing
# and validation below mirror it key for key (a Python daemon cannot source a
# shell file, and lib_hw.sh cannot be imported).
# The path override is spelled SA02M_HW_CONF, not lib_hw.sh's bare HW_CONF: a
# long-lived daemon inherits its whole environment from systemd, and a name that
# generic is one collision away from pointing this at the wrong file. Same
# namespaced idiom as SA02M_TELEMETRY_CONF above. The default path is identical.
HW_CONF_ENV = "SA02M_HW_CONF"
HW_CONF_DEFAULT = "/etc/sa02m_hw.conf"
HW_CHANNELS = ("do", "beeper", "alarm_led")
# The polarity mask under `auto` is built from every declared channel, usb_power
# included, exactly as lib_hw.sh sa02m_hw_i2c_output_mask_dec does.
HW_MASK_CHANNELS = HW_CHANNELS + ("usb_power",)
# lib_hw.sh's `SA02M_I2C_EXTRA_OUTPUT_MASK:-0x08` — bit3, KLogic's blue LED.
# Pinned against that line by tests/test_telemetry_hw_lock.py
# TestTheFallbacksMatchTheCgi, like every other default this daemon mirrors.
HW_EXTRA_OUTPUT_MASK_DEFAULT = 0x08


def _hw_conf_path() -> str:
    return os.environ.get(HW_CONF_ENV) or HW_CONF_DEFAULT


def _hw_parse_bit(raw: str) -> int | None:
    """A PCA9536 has four pins. lib_hw.sh sa02m_hw_i2c_channel_mask: `^[0-3]$`.

    Anything else — blank, a typo, an out-of-range index — returns None and the
    channel is refused. There is deliberately no default: guessing a pin is the
    defect this function exists to stop.
    """
    raw = (raw or "").strip()
    if len(raw) == 1 and raw in "0123":
        return int(raw)
    return None


def _hw_parse_mask(raw: str) -> int | None:
    """Mirror of lib_hw.sh sa02m_hw_i2c_extra_output_mask_dec's accepted forms.

    `0x` hex of one or two digits, or plain decimal 0..15; masked to the four
    real pins. Unparseable returns None so the caller can refuse rather than
    silently read it as 0 (which for a polarity mask means "active-high" — the
    wrong direction to fail in).
    """
    raw = (raw or "").strip()
    try:
        if raw[:2].lower() == "0x" and 1 <= len(raw) - 2 <= 2:
            return int(raw, 16) & 0x0F
        if raw.isdigit() and 0 <= int(raw) <= 15:
            return int(raw) & 0x0F
    except ValueError:
        pass
    return None


def _hw_parse_wait(raw: str) -> float | None:
    """Seconds to wait for the bus lock; None when the conf value is unusable.

    Blank — or an absent conf — is the shipped default, not an error. A value
    that is not a non-negative number returns None and every bus operation is
    refused with a line naming the key: `flock -w banana` fails the same way in
    lib_hw.sh, and a wait we cannot read is not one to guess at.
    """
    raw = (raw or "").strip()
    if not raw:
        return HW_LOCK_WAIT_SEC_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _hw_parse_override_sec(raw: str) -> int:
    """The buzzer override's TTL in seconds; never None, exactly as the CGI.

    lib_hw.sh: `ttl=${SA02M_BEEPER_WEB_OVERRIDE_SEC:-7}` and then
    `[[ "$ttl" =~ ^[0-9]+$ ]] || ttl=7`, so blank AND unparseable both resolve
    to 7. Unlike the bus-lock wait there is nothing to refuse here: an override
    written with a garbage expiry is a buzzer the worker either ignores
    outright or never stops, and both are worse than a 7 s pulse.
    """
    raw = (raw or "").strip()
    # isascii() as well as isdigit(): the shell's `^[0-9]+$` does not accept
    # the non-ASCII digits Python's isdigit() would.
    if raw.isascii() and raw.isdigit():
        return int(raw)
    return HW_BEEPER_OVERRIDE_SEC_DEFAULT


def _hw_parse_word_list(raw: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """A whitespace-separated shell list; blank resolves to `default`.

    lib_hw.sh can tell an absent key from an explicitly empty one — it defaults
    BEFORE sourcing the conf — and _read_conf_value cannot. Blank therefore
    means the default here, which is the fail-safe direction: the other reading
    ("no owners") would let this daemon write on a bus MPLC4 holds. An
    integrator who really wants no owner gate has the documented switch,
    SA02M_I2C_RESPECT_OWNER=0, and both consumers honour it identically.
    """
    words = tuple((raw or "").split())
    return words or default


def _hw_extra_output_mask(val) -> int:
    """SA02M_I2C_EXTRA_OUTPUT_MASK: pins the conf declares as outputs that no
    channel names.

    ABSENT is lib_hw.sh's `:-0x08`, NOT 0. A 0 there drops bit3 — KLogic's blue
    LED — out of the direction register, i.e. this daemon would turn a working
    output into an input while the CGI, which does default to 0x08, turns it
    back on the next panel click.

    How a live board reaches that state: NOT through the full installer —
    scripts/03-webserver.sh migrates the key in when it is missing. It is
    scripts/update-www-only.sh, which ships and restarts this daemon while
    carrying only the BACKEND migration, so a board last touched by that path
    runs new code against a conf that predates the key (it first shipped in
    1.0.5.64).

    BLANK is not the same case and the two consumers still disagree on it:
    lib_hw.sh applies its `:-0x08` at load and then SOURCES the conf, so an
    explicitly empty assignment overwrites the default and its reader's
    `${…:-0}` yields 0, while this function returns 0x08. Nothing shipped
    writes an empty value, so no board is in that state; it is recorded in
    .ai-dev/backlog.md rather than silently made to match, because whichever
    way it is resolved it changes which consumer moves.

    An unparseable value still resolves to 0, exactly as
    sa02m_hw_i2c_extra_output_mask_dec's `*)` branch prints 0.
    """
    raw = (val("SA02M_I2C_EXTRA_OUTPUT_MASK") or "").strip()
    if not raw:
        return HW_EXTRA_OUTPUT_MASK_DEFAULT
    return _hw_parse_mask(raw) or 0


class HwProfile:
    """What /etc/sa02m_hw.conf says about the expander, resolved once at start.

    `bits` holds only the channels whose configuration parsed; `refusals` holds
    the rest with the reason, so a command for one of them can be answered with
    a log line naming what is wrong instead of a write to a guessed pin.
    """

    def __init__(self, backend, bus, addr, bits, active_low_mask, refusals,
                 source, extra_output_mask=HW_EXTRA_OUTPUT_MASK_DEFAULT,
                 lock_file=HW_LOCK_FILE_DEFAULT,
                 lock_wait_s=HW_LOCK_WAIT_SEC_DEFAULT,
                 owner_units=HW_OWNER_UNITS_DEFAULT,
                 owner_procs=HW_OWNER_PROCS_DEFAULT, respect_owner=True,
                 beeper_override_sec=HW_BEEPER_OVERRIDE_SEC_DEFAULT,
                 beeper_override_file=HW_BEEPER_OVERRIDE_FILE_DEFAULT,
                 beeper_override_worker=HW_BEEPER_OVERRIDE_WORKER_DEFAULT):
        self.backend = backend
        self.bus = bus
        self.addr = addr
        self.bits = bits
        self.active_low_mask = active_low_mask
        self.refusals = refusals
        self.source = source
        # Who else may be on the byte, and how we take our turn on it.
        self.lock_file = lock_file
        self.lock_wait_s = lock_wait_s
        self.owner_units = owner_units
        self.owner_procs = owner_procs
        self.respect_owner = respect_owner
        # The buzzer's way past a bus we may not take, shared with the CGI.
        self.beeper_override_sec = beeper_override_sec
        self.beeper_override_file = beeper_override_file
        self.beeper_override_worker = beeper_override_worker
        # bit3 = KLogic's blue LED: this daemon never drives it, but the
        # direction register must not turn it back into an input.
        self.extra_output_mask = extra_output_mask

    def is_active_low(self, channel: str) -> bool:
        return bool(self.active_low_mask & (1 << self.bits[channel]))

    def output_mask(self) -> int:
        """Every pin this board drives, for the direction register."""
        mask = 0
        for bit in self.bits.values():
            mask |= 1 << bit
        return mask & 0x0F

    @classmethod
    def load(cls, path: str | None = None) -> "HwProfile":
        path = path or _hw_conf_path()
        present = os.path.isfile(path)

        def val(key: str) -> str:
            # The file's own parser, reused rather than re-implemented: shell
            # assignment semantics, last one wins, quotes stripped.
            return _read_conf_value(path, key) if present else ""

        backend = cls._resolve_backend(val)
        # Bus and address address the chip, they do not select a pin: a wrong
        # value here cannot mis-target a channel on THIS board, so the shipped
        # defaults (bus 2, 0x41) are a safe fallback where a bit index is not.
        bus_raw = (val("SA02M_I2C_EXP_BUS") or "").strip()
        bus = int(bus_raw) if bus_raw.isdigit() else 2
        try:
            addr = int((val("SA02M_I2C_EXP_ADDR") or "0x41").strip(), 0)
        except ValueError:
            addr = 0x41

        bits: dict[str, int] = {}
        refusals: dict[str, str] = {}
        raw_bits: dict[str, str] = {}
        for ch in HW_MASK_CHANNELS:
            raw = val("SA02M_I2C_BIT_" + ch.upper())
            raw_bits[ch] = raw
            bit = _hw_parse_bit(raw)
            if bit is None:
                if ch in HW_CHANNELS:
                    refusals[ch] = (
                        f"no usable bit in {path}: "
                        f"SA02M_I2C_BIT_{ch.upper()}={raw!r}"
                        if present else f"{path} is missing"
                    )
            else:
                bits[ch] = bit

        active_low = cls._resolve_active_low(val, bits, raw_bits)
        if active_low is None:
            for ch in HW_CHANNELS:
                refusals.setdefault(
                    ch, f"unparseable SA02M_I2C_ACTIVE_LOW_MASK in {path} — "
                        "polarity unknown, refusing rather than guessing")
            bits = {}
            active_low = 0

        # usb_power is not a telemetry channel; it contributed to the polarity
        # mask above and is dropped here so it cannot be commanded from MQTT.
        bits = {ch: b for ch, b in bits.items() if ch in HW_CHANNELS}
        extra = _hw_extra_output_mask(val)
        return cls(backend, bus, addr, bits, active_low, refusals,
                   path if present else f"{path} (absent)", extra,
                   lock_file=(val("SA02M_I2C_LOCK_FILE") or "").strip()
                   or HW_LOCK_FILE_DEFAULT,
                   lock_wait_s=_hw_parse_wait(val("SA02M_I2C_LOCK_WAIT_SEC")),
                   owner_units=_hw_parse_word_list(
                       val("SA02M_I2C_OWNER_UNITS"), HW_OWNER_UNITS_DEFAULT),
                   owner_procs=_hw_parse_word_list(
                       val("SA02M_I2C_OWNER_PROCS"), HW_OWNER_PROCS_DEFAULT),
                   respect_owner=(val("SA02M_I2C_RESPECT_OWNER") or "1").strip()
                   not in HW_RESPECT_OWNER_OFF,
                   beeper_override_sec=_hw_parse_override_sec(
                       val("SA02M_BEEPER_WEB_OVERRIDE_SEC")),
                   beeper_override_file=(
                       val("SA02M_BEEPER_OVERRIDE_FILE") or "").strip()
                   or HW_BEEPER_OVERRIDE_FILE_DEFAULT,
                   beeper_override_worker=(
                       val("SA02M_BEEPER_OVERRIDE_WORKER") or "").strip()
                   or HW_BEEPER_OVERRIDE_WORKER_DEFAULT)

    @staticmethod
    def _resolve_backend(val) -> str:
        """Mirror of lib_hw.sh sa02m_hw_backend()."""
        raw = (val("SA02M_HW_BACKEND") or "auto").strip()
        if raw in ("off", "disabled", "none"):
            return "disabled"
        if raw in ("i2c", "i2c_expander"):
            return "i2c_expander"
        if raw in ("gpio", "gpio_sysfs"):
            return "gpio_sysfs"
        for ch in HW_MASK_CHANNELS:
            if (val("SA02M_GPIO_" + ch.upper()) or "").strip().isdigit():
                return "gpio_sysfs"
        return "i2c_expander"

    @staticmethod
    def _resolve_active_low(val, bits, raw_bits) -> int | None:
        """Mirror of lib_hw.sh sa02m_hw_i2c_active_low_mask_dec().

        `auto` (the shipped value) means every declared output is active-low.
        """
        raw = (val("SA02M_I2C_ACTIVE_LOW_MASK") or "auto").strip()
        if raw in ("auto", ""):
            mask = _hw_extra_output_mask(val)
            for bit in bits.values():
                mask |= 1 << bit
            return mask & 0x0F
        return _hw_parse_mask(raw)


class PCA9536Control:
    """The PCA9536 output port, driven per an :class:`HwProfile`.

    Which pin a channel occupies and whether it is active-low are properties of
    the board, so they live in /etc/sa02m_hw.conf and arrive here in `profile`.
    There is deliberately no raw `set_bit` any more: a caller that can name a
    bit can name the wrong one, which is the whole 1.0.6.42 defect.

    Register 1 = output port, register 3 = direction (0 = output).
    """
    REG_OUT = 0x01
    REG_DIR = 0x03

    def __init__(self, profile: HwProfile):
        self._profile = profile
        self._lock = threading.Lock()
        # The direction register is applied under the bus lock too, so a busy
        # bus at startup DEFERS it instead of disabling hardware control for the
        # life of the process — init_hw() runs once, and MPLC4 already running
        # when this service starts is the normal case, not the exception.
        self._direction_applied = False

    @property
    def profile(self) -> HwProfile:
        return self._profile

    def channels(self) -> tuple[str, ...]:
        """The channels this board can actually drive, in a stable order."""
        return tuple(ch for ch in HW_CHANNELS if ch in self._profile.bits)

    def init(self) -> tuple[bool, str]:
        """Apply the direction register once, under the bus lock.

        Returns (usable, detail). `usable` is False only for a real I/O
        failure; a bus another owner holds leaves hardware control ENABLED with
        the direction write deferred to the first command that gets the lock.
        """
        owner = _hw_owner_active(self._profile)
        if owner is not None:
            return True, f"deferred, {owner} holds the expander"
        with self._lock:
            result = _with_bus_lock(self._profile,
                                    self._apply_direction_unlocked)
        if result is BUS_BUSY:
            return True, "deferred, " + _bus_busy_reason(self._profile)
        return (True, "") if result else (False, "the direction register would "
                                                 "not take")

    def _apply_direction_unlocked(self) -> bool:
        if self._direction_applied:
            return True
        # Direction from the config's own output set, as lib_hw.sh
        # sa02m_hw_i2c_config_mask_dec computes it: declared outputs low, the
        # rest left as inputs. Forcing 0x00 here would claim a pin an
        # integrator had deliberately narrowed out of the config.
        outputs = self._profile.output_mask() | self._profile.extra_output_mask
        config_mask = 0xF0 | ((~outputs) & 0x0F)
        if not _i2cset(self._profile.bus, self._profile.addr,
                       self.REG_DIR, config_mask):
            return False
        self._direction_applied = True
        return True

    def _read_out(self) -> int | None:
        return _i2cget(self._profile.bus, self._profile.addr, self.REG_OUT)

    def read_channels(self) -> tuple[dict[str, int], str]:
        """Every configured channel's LOGICAL level, from ONE locked port read.

        One read for all of them, as lib_hw.sh sa02m_hw_i2c_read_output_dec
        does: three separate reads can straddle another owner's write and
        publish a state no single instant of the port ever had. Reading the
        port rather than a cache is the same rule — MPLC4, KLogic and the
        beeper worker move these bits without us.

        The OWNER gate is deliberately not applied here, and this is the one
        place the daemon does not mirror the CGI. lib_hw.sh refuses a read while
        MPLC4 holds the bus so the panel can draw its «занято» badge: that
        gate's product is a UI state, not bus safety. This service has no badge
        — mirroring it would publish nothing for do/beeper/alarm_led on every
        board that runs MPLC4, which is every production board, and the moment
        the PLC is driving those pins is the moment their state is worth
        publishing. A read cannot corrupt the byte, and under the lock it cannot
        even observe a half-finished read-modify-write. What it can be is up to
        one poll stale, which is the milder failure and an honest one: the
        daemon publishes what it measured, it does not claim to have set it.

        Returns (levels, reason). A non-empty reason means nothing was read.
        """
        with self._lock:
            result = _with_bus_lock(self._profile, self._read_out)
        if result is BUS_BUSY:
            return {}, _bus_busy_reason(self._profile)
        if result is None:
            return {}, "the expander did not answer the read"
        levels: dict[str, int] = {}
        for channel in self.channels():
            on = bool(result & (1 << self._profile.bits[channel]))
            if self._profile.is_active_low(channel):
                on = not on
            levels[channel] = int(on)
        return levels, ""

    def _beeper_override(self, on: bool, owner: str) -> tuple[bool, str]:
        """The buzzer's path past an expander we may not take (1.0.6.43).

        No bus, no lock: the file is a request, and the worker applies it under
        the same flock everyone else takes. The worker is started AFTER the
        file lands, never before — the other order gives a worker with nothing
        to read, which it answers by exiting.
        """
        written, detail = _beeper_override_write(self._profile, on)
        if not written:
            return False, f"the beeper override could not be written: {detail}"
        started = _beeper_override_start_worker(self._profile)
        return True, (
            f"via the override file {self._profile.beeper_override_file} "
            f"for {self._profile.beeper_override_sec}s "
            f"({owner} holds the expander"
            + ("" if started else
               f"; {self._profile.beeper_override_worker} is not installed, so "
               f"another owner of the file applies it") + ")")

    def set_channel(self, channel: str, on: bool) -> tuple[bool, str]:
        """Drive one channel to a LOGICAL level. Returns (accepted, detail).

        The whole read-modify-write runs under the bus lock. The byte carries
        three other owners' bits, so a write that reads the port before someone
        else's write and lands after it silently reverts their command — and
        one of those bits is the discrete output.

        A refusal — an owner holding the bus, a lock that did not come free
        within the configured wait, a port that would not answer — comes back
        with a reason for the caller to log. Never a silent no-op, and never a
        write on a bus this daemon could not take.

        `detail` is that reason on a refusal. On an ACCEPTED command it is
        empty for a plain bus write and names the path taken when the command
        went to the beeper override instead (1.0.6.43) — the journal must not
        read «HW beeper = 1» for a pin this daemon did not drive itself.
        """
        if channel not in self._profile.bits:
            return False, f"no bit for it in {self._profile.source}"
        owner = _hw_owner_active(self._profile)
        if owner is not None:
            # The one widening (1.0.6.43), and it is exactly one channel wide.
            # `do` commutes real equipment and `alarm_led` is a steady
            # indicator, not a 7 s pulse; the panel refuses both here too.
            #
            # Deliberately NARROWER than lib_hw.sh in one respect: the CGI
            # takes this branch before it needs a bit at all, so a conf with no
            # SA02M_I2C_BIT_BEEPER still gets an override file — which the
            # worker then applies to its own `:-2` fallback pin. This daemon
            # refuses a channel with no configured bit above, and keeps doing
            # so here: driving a guessed pin is the 1.0.6.42 defect itself.
            if channel == HW_BEEPER_CHANNEL:
                return self._beeper_override(on, owner)
            return False, f"{owner} holds the expander"
        with self._lock:
            result = _with_bus_lock(
                self._profile, lambda: self._set_channel_unlocked(channel, on))
        if result is BUS_BUSY:
            return False, _bus_busy_reason(self._profile)
        return result

    def _set_channel_unlocked(self, channel: str, on: bool) -> tuple[bool, str]:
        if not self._apply_direction_unlocked():
            return False, "the direction register would not take"
        reg = self._read_out()
        if reg is None:
            # Without the current byte we would be guessing the bits we were
            # not asked to touch — and one of them is the discrete output.
            return False, "the expander did not answer the read"
        mask = 1 << self._profile.bits[channel]
        high = on if not self._profile.is_active_low(channel) else not on
        new = (reg | mask) if high else (reg & ~mask)
        if not _i2cset(self._profile.bus, self._profile.addr,
                       self.REG_OUT, new & 0xFF):
            return False, "the expander refused the write"
        return True, ""


# ── System metrics ─────────────────────────────────────────────────────────────
def cpu_usage_pct() -> int:
    try:
        with open("/proc/stat") as f:
            c1 = f.readline()
        time.sleep(0.1)
        with open("/proc/stat") as f:
            c2 = f.readline()
        a1 = list(map(int, c1.split()[1:]))
        a2 = list(map(int, c2.split()[1:]))
        total1 = sum(a1)
        total2 = sum(a2)
        idle1 = a1[3]
        idle2 = a2[3]
        dt = total2 - total1
        di = idle2 - idle1
        return (dt - di) * 100 // dt if dt > 0 else 0
    except Exception:
        return 0


def cpu_temp_c() -> float:
    best = 0
    try:
        for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            try:
                raw = int(p.read_text().strip())
                best = max(best, raw)
            except Exception:
                pass
    except Exception:
        pass
    return round(best / 1000, 1)


def ram_pct() -> int:
    try:
        info: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        if total > 0:
            return (total - avail) * 100 // total
    except Exception:
        pass
    return 0


def uptime_s() -> int:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except Exception:
        return 0


def rs485_stats(port_idx: int) -> dict:
    """Return tx/rx/errors for /dev/RS-485-{port_idx}."""
    dev = Path(f"/dev/RS-485-{port_idx}")
    if not dev.exists():
        return {}
    try:
        real = dev.resolve()
        ttyname = real.name
        portidx = re.sub(r"[^0-9]", "", ttyname)
        if not portidx:
            return {}
        for driver_file in Path("/proc/tty/driver").iterdir():
            try:
                text = driver_file.read_text()
                for line in text.splitlines():
                    if line.startswith(portidx + ":"):
                        m_tx = re.search(r"tx:(\d+)", line)
                        m_rx = re.search(r"rx:(\d+)", line)
                        m_fe = re.search(r"fe:(\d+)", line)
                        m_pe = re.search(r"pe:(\d+)", line)
                        m_oe = re.search(r"oe:(\d+)", line)
                        return {
                            "tx": int(m_tx.group(1)) if m_tx else 0,
                            "rx": int(m_rx.group(1)) if m_rx else 0,
                            "errors": (
                                (int(m_fe.group(1)) if m_fe else 0) +
                                (int(m_pe.group(1)) if m_pe else 0) +
                                (int(m_oe.group(1)) if m_oe else 0)
                            ),
                        }
            except Exception:
                pass
    except Exception:
        pass
    return {}


# ── MQTT client ───────────────────────────────────────────────────────────────
class TelemetryClient:
    def __init__(self):
        self._device_id, self._device_id_source = _resolve_device_id()
        # MQTT 3.1 caps the client id at 23 bytes and the id above may now be
        # pinned up to 64 chars, so truncate — the package idiom
        # (mqtt_live_snapshot.py, mqtt_monitor_stream.py).
        client_id = f"{self._device_id}-telemetry"[:MQTT_CLIENT_ID_MAX]
        # Pin paho to the v1 callback API so the (client, userdata, flags, rc)
        # signatures below stay valid on paho-mqtt 2.x (default there is v2).
        try:
            # paho-mqtt >= 2.0: use VERSION2 to avoid deprecation warning
            self._client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        except (AttributeError, TypeError):
            # paho-mqtt < 2.0
            self._client = mqtt.Client(client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # Last Will (single per connection): device-level error marks the
        # controller offline if telemetry crashes. ``connection`` is published
        # actively (1 on connect, 0 on graceful stop).
        self._client.will_set(
            f"{DEVICE_BASE}/{self._device_id}/meta/error", "r",
            qos=MQTT_QOS, retain=True,
        )
        self._connected = False
        self._hw: PCA9536Control | None = None
        self._meta_done = False
        self._legacy_cleared = False

    # paho-mqtt v1: (client, userdata, flags, rc)
    # paho-mqtt v2: (client, userdata, connect_flags, reason_code, properties)
    def _on_connect(self, client, userdata, flags, rc, *_):
        failed = rc.is_failure if hasattr(rc, "is_failure") else bool(rc)
        if failed:
            log.warning("MQTT connect failed: %s", rc)
            return
        self._connected = True
        log.info("MQTT connected")
        self._subscribe_writeback()
        self._pub("controls/connection", "1")
        self._pub("meta/error", "")

    # paho-mqtt v1: (client, userdata, rc)
    # paho-mqtt v2: (client, userdata, disconnect_flags, reason_code, properties)
    def _on_disconnect(self, client, userdata, flags_or_rc, *extra):
        self._connected = False
        rc = extra[0] if extra else flags_or_rc
        log.warning("MQTT disconnected: %s", rc)

    def _subscribe_writeback(self) -> None:
        dev = self._device_id
        for ctrl in ["do", "beeper", "alarm_led"]:
            topic = f"{DEVICE_BASE}/{dev}/controls/{ctrl}/on"
            self._client.subscribe(topic, qos=MQTT_QOS)
            self._client.message_callback_add(topic, self._make_hw_cb(ctrl))

    def _make_hw_cb(self, ctrl: str):
        def cb(client, userdata, msg):
            if self._hw is None:
                # Never silent: a dropped command that leaves no trace is the
                # exact defect class this release exists to close.
                log.warning("HW not ready — %s command dropped", ctrl)
                return
            if ctrl not in self._hw.channels():
                # No bit for this channel in /etc/sa02m_hw.conf. Refusing is
                # the point: driving a guessed pin is how `beeper` came to
                # switch the discrete output (1.0.6.42).
                log.warning(
                    "HW %s has no configured bit — command dropped (%s)",
                    ctrl, self._hw.profile.refusals.get(ctrl, "not configured"),
                )
                return
            val = msg.payload.decode().strip()
            on = val not in ("0", "false", "False", "")
            accepted, why = self._hw.set_channel(ctrl, on)
            if accepted:
                self._pub(f"controls/{ctrl}", "1" if on else "0")
                # `why` is empty for a plain bus write and names the override
                # path when the command went there instead (1.0.6.43): the
                # journal never claims a byte this daemon did not put out.
                log.info("HW %s = %d%s", ctrl, on, f" {why}" if why else "")
            else:
                # A refusal names who we yielded the shared bus to, or what
                # would not answer. Publishing nothing here is the other half:
                # a state we did not write must not be reported as written.
                log.warning("HW %s not driven: %s", ctrl, why)
        return cb

    def _pub(self, suffix: str, value: str, retain: bool = True) -> None:
        topic = f"{DEVICE_BASE}/{self._device_id}/{suffix}"
        self._client.publish(topic, value, qos=MQTT_QOS, retain=retain)

    def _publish_meta(self) -> None:
        if self._meta_done:
            return
        self._pub("meta/name", f"СА-02м ({_hostname() or self._device_id})")
        # The constant, never a second literal: the legacy clear's ownership
        # proof compares against exactly what this line publishes.
        self._pub("meta/driver", TELEMETRY_DRIVER)
        # Availability control (paired with the Last Will above)
        self._pub("controls/connection/meta/type", "switch")
        self._pub("controls/connection/meta/readonly", "1")
        # Control meta
        for ctrl, ctype in [
            ("cpu_pct", "value"), ("temp_c", "temperature"),
            ("ram_pct", "value"), ("uptime_s", "value"),
        ]:
            self._pub(f"controls/{ctrl}/meta/type", ctype)
            self._pub(f"controls/{ctrl}/meta/readonly", "1")
        for ctrl in ["do", "beeper", "alarm_led"]:
            self._pub(f"controls/{ctrl}/meta/type", "switch")
        for i in range(SERIAL_COUNT):
            port = f"com{i+1}"
            for key in ["tx", "rx", "errors"]:
                self._pub(f"controls/rs485_{port}_{key}/meta/type", "value")
                self._pub(f"controls/rs485_{port}_{key}/meta/readonly", "1")
        self._meta_done = True

    def _publish_metrics(self) -> None:
        self._pub("controls/cpu_pct", str(cpu_usage_pct()))
        self._pub("controls/temp_c", str(cpu_temp_c()))
        self._pub("controls/ram_pct", str(ram_pct()))
        self._pub("controls/uptime_s", str(uptime_s()))

        # HW state. Only the channels the config actually maps, and as the
        # LOGICAL level — publishing the raw bit on an active-low output is how
        # a retained `1` came to mean "off" and the app showed the opposite of
        # the hardware. A channel we cannot read is left unpublished rather
        # than reported as 0.
        if self._hw:
            levels, why = self._hw.read_channels()
            if why:
                log.warning("HW state not published this cycle: %s", why)
            for ctrl, level in levels.items():
                self._pub(f"controls/{ctrl}", str(level))

        # RS-485 stats
        for i in range(SERIAL_COUNT):
            stats = rs485_stats(i)
            port = f"com{i+1}"
            if stats:
                self._pub(f"controls/rs485_{port}_tx", str(stats.get("tx", 0)))
                self._pub(f"controls/rs485_{port}_rx", str(stats.get("rx", 0)))
                self._pub(f"controls/rs485_{port}_errors", str(stats.get("errors", 0)))

    def connect(self) -> None:
        while not _stop.is_set():
            try:
                self._client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
                self._client.loop_start()
                return
            except Exception as e:
                log.error("MQTT connect error: %s — retry in 5s", e)
                time.sleep(5)

    def init_hw(self) -> None:
        profile = HwProfile.load()
        # Backend is honoured, not assumed (1.0.6.42). `disabled` is what an
        # integrator sets to keep software off this expander; `gpio_sysfs`
        # routes these channels to sysfs pins, which this daemon does not
        # implement. Driving the PCA9536 in either case writes to hardware the
        # operator explicitly took us off — so refuse, loudly, and leave the
        # web UI (which does implement both) as the way to drive them.
        if profile.backend != "i2c_expander":
            log.warning(
                "HW backend is %s (%s) — telemetry drives no outputs; "
                "hardware control stays with the web UI",
                profile.backend, profile.source,
            )
            self._hw = None
            return
        for ctrl, reason in sorted(profile.refusals.items()):
            log.warning("HW %s unavailable: %s", ctrl, reason)
        if not profile.bits:
            log.warning("no usable hardware channel in %s — HW control disabled",
                        profile.source)
            self._hw = None
            return
        hw = PCA9536Control(profile)
        usable, detail = hw.init()
        if not usable:
            log.warning("PCA9536 init failed — HW control disabled")
            self._hw = None
            return
        self._hw = hw
        if detail:
            log.warning("PCA9536 direction register %s — it is applied by the "
                        "first command that gets the bus", detail)
        log.info(
            "HW channels from %s: %s (active-low mask 0x%X, bus lock %s)",
            profile.source,
            ", ".join(f"{c}=bit{profile.bits[c]}" for c in hw.channels()),
            profile.active_low_mask, profile.lock_file,
        )

    def _clear_legacy_retained(self) -> None:
        """Once per process, off the _on_connect callback thread."""
        if self._legacy_cleared:
            return
        self._legacy_cleared = True     # whatever the outcome — never on reconnect
        deadline = time.monotonic() + LEGACY_CLEAR_CONNECT_WAIT_S
        while not self._connected and time.monotonic() < deadline:
            if _stop.is_set():
                return
            time.sleep(0.1)
        if not self._connected:
            log.warning("legacy retained clear skipped: no MQTT connection")
            return
        try:
            clear_legacy_retained(self._client, self._device_id, MQTT_BROKER)
        except Exception as e:
            log.warning("legacy retained clear failed: %s", e)

    def run(self) -> None:
        # The always-on "which id is this board really serving" probe — what the
        # next person needs the moment a binding looks dead.
        log.info(
            "telemetry device id: %s (source: %s)",
            self._device_id, self._device_id_source,
        )
        self.connect()
        # HW FIRST, then the clear. _on_connect subscribes to controls/*/on the
        # moment the broker answers, so anything between connect() and a ready
        # self._hw is a window where a beeper/DO command is accepted and
        # dropped. The clear does not touch _hw, so ordering it after costs
        # nothing and keeps that window at its pre-1.0.6.22 length.
        self.init_hw()
        self._clear_legacy_retained()
        time.sleep(1)
        sd_notify("READY=1")

        while not _stop.is_set():
            try:
                self._publish_meta()
                self._publish_metrics()
            except Exception as e:
                log.error("publish error: %s", e)
            _stop.wait(POLL_INTERVAL_S)

        # Graceful offline before exit (avoid leaving stale "online" retained).
        try:
            self._pub("controls/connection", "0")
            self._pub("meta/error", "r")
            time.sleep(0.2)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass
        log.info("Telemetry stopped")


def main() -> None:
    signal.signal(signal.SIGTERM, lambda s, f: _stop.set())
    signal.signal(signal.SIGINT, lambda s, f: _stop.set())
    client = TelemetryClient()
    client.run()


if __name__ == "__main__":
    main()

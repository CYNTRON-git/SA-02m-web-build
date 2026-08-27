#!/usr/bin/env python3
"""SA-02m system telemetry → MQTT.

Publishes CPU, RAM, temperature, uptime, RS-485 stats.
Subscribes to /devices/<device-id>/controls/{do,beeper,alarm_led}/on
and controls the PCA9536 I2C expander via i2cset (or hw_set.cgi).

Device ID: the board name itself (the hostname, e.g. ``SA-02m``) — no prefix,
resolved by :func:`_resolve_device_id`. Topic canon: docs/MQTT_TOPICS.md.
"""

from __future__ import annotations

import os
import re
import sys
import time
import signal
import socket
import logging
import ipaddress
import subprocess
import threading
from pathlib import Path

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

# ── Legacy retained clear (1.0.6.21 migration) ───────────────────────────────
# Before 1.0.6.21 the id was the hostname glued behind a fixed prefix, so the
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
    """The deterministic pre-1.0.6.21 id set, minus the current id.

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
    """Clear this board's own orphaned pre-1.0.6.21 retained subtree.

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


# ── Hardware control (PCA9536 via i2cget/i2cset or /etc/sa02m_hw.conf) ───────
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


class PCA9536Control:
    """PCA9536 I2C expander: DO=bit0, Beeper=bit1, AlarmLED=bit2.

    SA-02m uses I2C bus 2, address 0x41.
    Register 1 = output port, register 3 = direction (0=output).
    """
    BUS = 2
    ADDR = 0x41
    REG_OUT = 0x01
    REG_DIR = 0x03

    def __init__(self):
        self._lock = threading.Lock()
        self._state = 0x00  # all low

    def init(self) -> bool:
        # Set all pins as output
        ok = _i2cset(self.BUS, self.ADDR, self.REG_DIR, 0x00)
        if ok:
            cur = _i2cget(self.BUS, self.ADDR, self.REG_OUT)
            if cur is not None:
                self._state = cur & 0x07
        return ok

    def get_bit(self, bit: int) -> int:
        return (self._state >> bit) & 1

    def set_bit(self, bit: int, value: bool) -> bool:
        with self._lock:
            if value:
                new_state = self._state | (1 << bit)
            else:
                new_state = self._state & ~(1 << bit)
            if _i2cset(self.BUS, self.ADDR, self.REG_OUT, new_state & 0xFF):
                self._state = new_state & 0xFF
                return True
            return False


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
        bit_map = {"do": 0, "beeper": 1, "alarm_led": 2}

        def cb(client, userdata, msg):
            if self._hw is None:
                # Never silent: a dropped command that leaves no trace is the
                # exact defect class this release exists to close.
                log.warning("HW not ready — %s command dropped", ctrl)
                return
            val = msg.payload.decode().strip()
            on = val not in ("0", "false", "False", "")
            bit = bit_map.get(ctrl, 0)
            if self._hw.set_bit(bit, on):
                self._pub(f"controls/{ctrl}", "1" if on else "0")
                log.info("HW %s = %d", ctrl, on)
            else:
                log.warning("HW %s write failed", ctrl)
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

        # HW state
        if self._hw:
            for ctrl, bit in [("do", 0), ("beeper", 1), ("alarm_led", 2)]:
                self._pub(f"controls/{ctrl}", str(self._hw.get_bit(bit)))

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
        self._hw = PCA9536Control()
        if not self._hw.init():
            log.warning("PCA9536 init failed — HW control disabled")
            self._hw = None

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
        # nothing and keeps that window at its pre-1.0.6.21 length.
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

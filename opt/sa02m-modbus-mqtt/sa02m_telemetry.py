#!/usr/bin/env python3
"""SA-02m system telemetry → MQTT.

Publishes CPU, RAM, temperature, uptime, RS-485 stats.
Subscribes to /devices/sa02m-<hostname>/controls/{do,beeper,alarm_led}/on
and controls the PCA9536 I2C expander via i2cset (or hw_set.cgi).

Device ID: sa02m-<hostname>
"""

import os
import re
import sys
import time
import signal
import socket
import logging
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


def get_device_id() -> str:
    try:
        h = socket.gethostname()
    except Exception:
        h = "SA-02"
    return f"sa02m-{h}"


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
        self._device_id = get_device_id()
        # Pin paho to the v1 callback API so the (client, userdata, flags, rc)
        # signatures below stay valid on paho-mqtt 2.x (default there is v2).
        try:
            self._client = mqtt.Client(
                client_id=f"{self._device_id}-telemetry",
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            )
        except (AttributeError, TypeError):
            # paho-mqtt 1.x has no callback_api_version argument.
            self._client = mqtt.Client(client_id=f"{self._device_id}-telemetry")
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

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            log.info("MQTT connected")
            self._subscribe_writeback()
            self._pub("controls/connection", "1")
            self._pub("meta/error", "")
        else:
            log.warning("MQTT connect rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        log.warning("MQTT disconnected rc=%d", rc)

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
        self._pub("meta/name", f"СА-02м ({socket.gethostname()})")
        self._pub("meta/driver", "sa02m-telemetry")
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

    def run(self) -> None:
        self.connect()
        self.init_hw()
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

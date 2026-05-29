#!/usr/bin/env python3
"""SA-02m Modbus→MQTT bridge v2.

Devices:  mr02m (all 13 types), dtv (RTU-Sensor), ce02m3
Protocol: standard Modbus RTU (FC01-06) + Wiren Board Fast Modbus
          (FC 0x46: scanner + event polling).
Topics:   Wiren Board MQTT convention (/devices/…/controls/…)
Config:   /etc/sa02m-modbus-mqtt.yaml  (env SA02M_MQTT_CONFIG to override)
Systemd:  sd_notify READY=1 / WATCHDOG=1
"""

import os
import sys
import time
import signal
import struct
import logging
import threading
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pyyaml not installed: pip3 install pyyaml")

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt not installed: pip3 install paho-mqtt")

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed: pip3 install pyserial")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge")

# ── Constants ──────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(os.environ.get("SA02M_MQTT_CONFIG", "/etc/sa02m-modbus-mqtt.yaml"))
DEVICE_BASE = "/devices"
FMB_ADDR = 0xFD          # Fast Modbus broadcast address

# MR-02m module type → (do_count, di_count, ao_count, ai_count)
# Source: Core/Inc/main.h, MODBUS_VARIABLES.txt, subagent exploration
MR02M_MODULE_TYPES: dict[int, tuple[int, int, int, int]] = {
    1:  (6,  8,  0,  0),   # DO6DI8
    2:  (16, 0,  0,  0),   # DO16
    3:  (0,  0,  12, 0),   # AO12
    4:  (6,  0,  0,  0),   # DO6
    5:  (0,  14, 0,  0),   # DI14
    6:  (0,  0,  6,  6),   # AO6AI6
    7:  (0,  0,  0,  12),  # AI12
    8:  (4,  6,  0,  0),   # DO4DI6
    9:  (0,  0,  0,  0),   # TENZO2 (strain gauges, special)
    10: (0,  10, 0,  0),   # 10DIcon (DI1-10 = Input 18-27)
    11: (6,  5,  2,  0),   # 6DO5DI2AO
    12: (0,  0,  2,  6),   # AI6AO2
    15: (4,  6,  4,  0),   # 4TO6DI (triac dimmers)
}
MR02M_TYPE_NAMES: dict[int, str] = {
    1: "DO6DI8", 2: "DO16", 3: "AO12", 4: "DO6", 5: "DI14",
    6: "AO6AI6", 7: "AI12", 8: "DO4DI6", 9: "TENZO2",
    10: "10DIcon", 11: "6DO5DI2AO", 12: "AI6AO2", 15: "4TO6DI",
}

# AI sensor type codes → (mqtt_type, units, scale)
# Scaled register units per MODBUS_VARIABLES.txt (ai_sensor_t, codes 0..38):
#   Temperature sensors:     0.1 °C  (reg × 0.1 = °C)
#   VOLTAGE_10V  (code 4):   mV  (0..10000),  reg × 0.001 = V
#   CURRENT_4_20 (code 5):   0.01 mA (0..2000), reg × 0.01 = mA
#   CURRENT_0_5  (code 21):  0.01 mA (0..500),  reg × 0.01 = mA
#   CURRENT_0_20 (code 22):  0.01 mA (0..2000), reg × 0.01 = mA
#   DRY_CONTACT  (code 7):   0 or 1
#   DIFF_50MV    (code 23):  raw mV (user-calibrated limits), reg × 1.0 = mV
#   DIFF_2V      (code 24):  raw 0.001 V (user-calibrated limits), reg × 0.001 = V
#   VOLTAGE_30V  (code 38):  0.01 V (0..3000), reg × 0.01 = V
_TEMP = ("temperature", "°C", 0.1)
AI_SENSOR_TYPES: dict[int, tuple[str, str, float]] = {
    0:  ("value",       "",    1.0),    # Disabled
    4:  ("voltage",     "V",   0.001), # 0–10 V  (raw 0..10000 mV)
    5:  ("current",     "mA",  0.01),  # 4–20 mA (raw 0..2000 × 0.01 mA)
    7:  ("switch",      "",    1.0),   # Dry contact (0/1)
    21: ("current",     "mA",  0.01),  # 0–5 mA  (raw 0..500 × 0.01 mA)
    22: ("current",     "mA",  0.01),  # 0–20 mA (raw 0..2000 × 0.01 mA)
    23: ("voltage",     "mV",  1.0),   # ±50 mV differential (raw in mV)
    24: ("voltage",     "V",   0.001), # ±2 V differential (raw × 0.001 V)
    38: ("voltage",     "V",   0.01),  # 0–30 V  (raw 0..3000 × 0.01 V)
}
# All temperature-type codes (RTD/NTC/thermocouple, codes 1-3, 6, 8-20, 25-37):
for _code in (
    list(range(1, 4)) + [6] + list(range(8, 21)) + list(range(25, 38))
):
    AI_SENSOR_TYPES.setdefault(_code, _TEMP)

# Fast Modbus event type codes (WB standard, from fast_mb_events.h)
FMB_EVT_COIL     = 0x00   # DO coil,  1 byte payload
FMB_EVT_DISCRETE = 0x01   # DI discrete, 1 byte payload
FMB_EVT_HOLDING  = 0x02   # AO holding, 2 bytes payload (BE)
FMB_EVT_INPUT    = 0x03   # DI/AI input, 2 bytes payload (BE)
FMB_EVT_REBOOT   = 0x0F   # device rebooted, 0 bytes payload


# ── Systemd watchdog ───────────────────────────────────────────────────────────
def sd_notify(msg: str) -> None:
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    import socket
    try:
        addr = sock_path.lstrip("@")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            if sock_path.startswith("@"):
                s.connect("\0" + addr)
            else:
                s.connect(addr)
            s.sendall(msg.encode())
    except Exception:
        pass


# ── CRC16 & Modbus frame builders ──────────────────────────────────────────────
def crc16(data: bytes) -> int:
    """Standard Modbus CRC-16/IBM (poly 0xA001). Returns uint16."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else crc >> 1
    return crc


def _append_crc(data: bytes) -> bytes:
    """Append CRC16 in standard Modbus wire order [LO, HI]."""
    c = crc16(data)
    return data + bytes([c & 0xFF, c >> 8])


def build_request(addr: int, fc: int, reg: int, count: int) -> bytes:
    return _append_crc(bytes([addr, fc, reg >> 8, reg & 0xFF, count >> 8, count & 0xFF]))


def build_write_coil(addr: int, coil: int, value: bool) -> bytes:
    v = 0xFF00 if value else 0x0000
    return _append_crc(bytes([addr, 0x05, coil >> 8, coil & 0xFF, v >> 8, v & 0xFF]))


def build_write_register(addr: int, reg: int, value: int) -> bytes:
    return _append_crc(bytes([addr, 0x06, reg >> 8, reg & 0xFF, value >> 8, value & 0xFF]))


def build_fmb5(sub: int) -> bytes:
    """5-byte Fast Modbus broadcast command (begin/next/end scan)."""
    return _append_crc(bytes([FMB_ADDR, 0x46, sub]))


def build_fmb_poll_events(min_slave: int, max_data: int,
                          ack_slave: int, ack_flag: int) -> bytes:
    """9-byte poll_events frame (0x10)."""
    return _append_crc(bytes([FMB_ADDR, 0x46, 0x10,
                               min_slave, max_data, ack_slave, ack_flag]))


def build_fmb_configure_events(addr: int, evt_type: int,
                                start_reg: int, count: int, priority: int) -> bytes:
    """configure_events (0x18) unicast frame."""
    data = bytes([addr, 0x46, 0x18, 5,
                  evt_type, start_reg >> 8, start_reg & 0xFF, count, priority])
    return _append_crc(data)


# ── ModbusSerial ───────────────────────────────────────────────────────────────
class ModbusSerial:
    """Thread-safe Modbus RTU over serial, with Fast Modbus support."""

    def __init__(self, port: str, baudrate: int, timeout: float = 0.3):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    def _ensure_open(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            self._ser = serial.Serial(
                self._port, baudrate=self._baudrate,
                bytesize=8, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=self._timeout,
            )
            time.sleep(0.05)
        return self._ser

    def close(self) -> None:
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self._ser = None

    def _transact(self, request: bytes, expected: int) -> bytes:
        ser = self._ensure_open()
        ser.reset_input_buffer()
        ser.write(request)
        resp = ser.read(expected)
        if len(resp) < expected:
            raise IOError(f"Short response: {len(resp)}/{expected} bytes")
        recv_crc = resp[-2] | (resp[-1] << 8)
        if crc16(resp[:-2]) != recv_crc:
            raise IOError(f"CRC mismatch on FC{request[1]:02X}")
        if resp[1] & 0x80:
            raise IOError(f"Modbus exception {resp[2]} on FC{request[1] & 0x7F:02X}")
        return resp

    # --- Standard Modbus reads ------------------------------------------------

    def read_coils(self, addr: int, start: int, count: int) -> list[int]:
        with self._lock:
            resp = self._transact(build_request(addr, 0x01, start, count),
                                  5 + (count + 7) // 8)
            return [(resp[3 + i // 8] >> (i % 8)) & 1 for i in range(count)]

    def read_discrete_inputs(self, addr: int, start: int, count: int) -> list[int]:
        with self._lock:
            resp = self._transact(build_request(addr, 0x02, start, count),
                                  5 + (count + 7) // 8)
            return [(resp[3 + i // 8] >> (i % 8)) & 1 for i in range(count)]

    def read_holding_registers(self, addr: int, start: int, count: int) -> list[int]:
        with self._lock:
            resp = self._transact(build_request(addr, 0x03, start, count),
                                  5 + count * 2)
            return [(resp[3 + i * 2] << 8) | resp[4 + i * 2] for i in range(count)]

    def read_input_registers(self, addr: int, start: int, count: int) -> list[int]:
        with self._lock:
            resp = self._transact(build_request(addr, 0x04, start, count),
                                  5 + count * 2)
            return [(resp[3 + i * 2] << 8) | resp[4 + i * 2] for i in range(count)]

    def write_coil(self, addr: int, coil: int, value: bool) -> None:
        with self._lock:
            self._transact(build_write_coil(addr, coil, value), 8)

    def write_register(self, addr: int, reg: int, value: int) -> None:
        with self._lock:
            self._transact(build_write_register(addr, reg, value), 8)

    # --- Fast Modbus ----------------------------------------------------------

    def fmb_send_recv(self, frame: bytes, min_resp: int, max_resp: int,
                      timeout: float) -> bytes:
        """
        Send a Fast Modbus frame and read variable-length response.
        Temporarily overrides serial timeout for faster event polling.
        """
        with self._lock:
            ser = self._ensure_open()
            old_t = ser.timeout
            try:
                ser.timeout = timeout
                ser.reset_input_buffer()
                ser.write(frame)
                buf = b""
                deadline = time.monotonic() + timeout
                while len(buf) < max_resp and time.monotonic() < deadline:
                    chunk = ser.read(max_resp - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                return buf if len(buf) >= min_resp else b""
            finally:
                ser.timeout = old_t


# ── Port pool (shared serial per port:baud) ────────────────────────────────────
_port_pool: dict[str, ModbusSerial] = {}
_port_pool_lock = threading.Lock()


def get_port(port_path: str, baudrate: int) -> ModbusSerial:
    key = f"{port_path}:{baudrate}"
    with _port_pool_lock:
        if key not in _port_pool:
            _port_pool[key] = ModbusSerial(port_path, baudrate)
        return _port_pool[key]


# ── FastModbusScanner ──────────────────────────────────────────────────────────
class FastModbusScanner:
    """
    Wiren Board Fast Modbus bus scanner.

    Protocol (from fast_mb.c, modbus_rtu_hw.c):
      begin_scan:  [FD 46 01 CRC_L CRC_H]          → 5 bytes
      next_scan:   [FD 46 02 CRC_L CRC_H]           → 5 bytes
      end_scan:    [FD 46 04 CRC_L CRC_H]            → 5 bytes
      answer_scan: [FD 46 03 SN3 SN2 SN1 SN0 ADDR CRC_L CRC_H] → 10 bytes
        SN bytes: big-endian serial number (MSB first).
        ADDR: Modbus slave address (1-247).
        CRC: standard CRC16 [LO, HI] over bytes 0-7.
    """
    MAX_DEVICES = 32
    SCAN_TIMEOUT = 0.5  # per device, covers arbitration (~8ms@115200) + response

    def scan(self, port_path: str, baudrate: int) -> list[dict]:
        """Return [{serial, address}] for each found device."""
        devices: list[dict] = []
        try:
            ser = serial.Serial(
                port_path, baudrate, bytesize=8,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=self.SCAN_TIMEOUT,
            )
            time.sleep(0.05)
        except Exception as e:
            log.error("FMB scan: cannot open %s: %s", port_path, e)
            return devices

        try:
            ser.reset_input_buffer()
            ser.write(build_fmb5(0x01))  # begin_scan

            for _ in range(self.MAX_DEVICES):
                resp = ser.read(10)
                if len(resp) < 10:
                    break
                if resp[0] != FMB_ADDR or resp[1] != 0x46 or resp[2] != 0x03:
                    break
                # Verify CRC (standard Modbus [LO, HI])
                calc = crc16(resp[:8])
                recv = resp[8] | (resp[9] << 8)
                if calc != recv:
                    log.warning("FMB scan: CRC error in answer_scan")
                    break
                # Serial: big-endian bytes 3-6
                serial_no = struct.unpack(">I", resp[3:7])[0]
                addr = resp[7]
                devices.append({"serial": serial_no, "address": addr})
                log.debug("FMB scan: found addr=%d serial=0x%08X", addr, serial_no)

                # next_scan to wake next device
                ser.reset_input_buffer()
                ser.write(build_fmb5(0x02))

            ser.write(build_fmb5(0x04))   # end_scan
            time.sleep(0.05)
        except Exception as e:
            log.error("FMB scan error on %s: %s", port_path, e)
        finally:
            ser.close()

        return devices


# ── FastModbusEventPortManager ─────────────────────────────────────────────────
class FastModbusEventPortManager:
    """
    Per-port Fast Modbus event polling for real-time DO/DI/AO/AI notifications.

    Poll cycle:
      1. Send poll_events (0x10) broadcast.
      2. One device wins arbitration and responds 0x11 (events) or 0x12 (no events).
      3. Publish changed values to MQTT immediately.
      4. Repeat; on 0x12 wait 50ms, on 0x11 poll again immediately.
    """
    POLL_TIMEOUT = 0.25   # 250ms covers 12-bit event arbitration at any baud
    MAX_DATA_LEN = 128

    def __init__(self, port_path: str, baudrate: int, pub: "MQTTPublisher"):
        self._port_path = port_path
        self._baudrate = baudrate
        self._pub = pub
        self._devices: dict[int, dict] = {}   # addr → info
        self._ack_slave: int = 0
        self._ack_flag:  int = 0
        self._stop = threading.Event()
        self._log = logging.getLogger(f"fmb.{port_path.replace('/dev/', '')}")

    def register_device(self, addr: int, device_id: str, device_type: str,
                        do_count: int, di_count: int,
                        ao_count: int, ai_count: int) -> None:
        self._devices[addr] = {
            "id": device_id, "type": device_type,
            "do": do_count, "di": di_count,
            "ao": ao_count, "ai": ai_count,
            "configured": False,
        }

    # --- configure_events (0x18) for one device ------------------------------

    def _configure_device(self, ser: ModbusSerial, addr: int, dev: dict) -> bool:
        configs: list[tuple[int, int, int]] = []  # (evt_type, start_reg, count)
        if dev["do"] > 0:
            configs.append((FMB_EVT_COIL,    1,  dev["do"]))   # DO: COIL type=0x00, addr 1+
        if dev["di"] > 0:
            configs.append((FMB_EVT_INPUT,   18, dev["di"]))   # DI: INPUT type=0x03, addr 18+
            # NOTE: MR-02m DI events arrive as FMB_EVT_INPUT (0x03) at Input Reg address 18+
            # NOT as FMB_EVT_DISCRETE (0x01) — per MODBUS_VARIABLES.txt line 21 + configure_events type 03
        if dev["ao"] > 0:
            configs.append((FMB_EVT_HOLDING, 33, dev["ao"]))   # AO: HOLDING type=0x02, addr 33+
        # AI events also arrive as FMB_EVT_INPUT at 403,410,... — handled in _dispatch if needed.

        for (evt_type, start_reg, count) in configs:
            frame = build_fmb_configure_events(addr, evt_type, start_reg, count, 1)
            try:
                # ACK: [addr][0x46][0x18][1][0x00][CRC_L][CRC_H] = 7 bytes
                resp = ser.fmb_send_recv(frame, 7, 7, 0.3)
                if len(resp) < 7 or resp[0] != addr or resp[2] != 0x18:
                    self._log.warning("configure_events addr=%d type=%d failed", addr, evt_type)
                    return False
            except Exception as e:
                self._log.warning("configure_events addr=%d: %s", addr, e)
                return False

        return True

    # --- poll_events (0x10) loop ---------------------------------------------

    def _poll_once(self, ser: ModbusSerial) -> tuple[bool, list[tuple]]:
        """
        One poll_events cycle.  Returns (had_events, [(slave, type, reg, val)]).
        """
        frame = build_fmb_poll_events(1, self.MAX_DATA_LEN,
                                      self._ack_slave, self._ack_flag)
        buf = ser.fmb_send_recv(frame, 4, 256, self.POLL_TIMEOUT)
        if len(buf) < 4 or buf[1] != 0x46:
            return False, []

        slave_id = buf[0]
        subcode  = buf[2]

        if subcode == 0x12:
            # No events: [slave][0x46][0x12][flag][CRC_L][CRC_H] = 6 bytes
            if len(buf) >= 6:
                flag = buf[3]
                calc = crc16(buf[:4])
                recv = buf[4] | (buf[5] << 8)
                if calc == recv:
                    self._ack_slave = slave_id
                    self._ack_flag  = flag
            return False, []

        if subcode != 0x11:
            return False, []

        # Events: [slave][0x46][0x11][FLAG][N][DATA_LEN]{events}[CRC_L][CRC_H]
        if len(buf) < 8:
            return False, []

        flag     = buf[3]
        n_events = buf[4]
        data_len = buf[5]
        total    = 6 + data_len + 2

        if len(buf) < total:
            return False, []

        # Verify CRC
        calc = crc16(buf[:6 + data_len])
        recv = buf[6 + data_len] | (buf[7 + data_len] << 8)
        if calc != recv:
            self._log.warning("poll_events: CRC error (slave=%d)", slave_id)
            return False, []

        # Parse events from buf[6 .. 6+data_len-1]
        events: list[tuple] = []
        pos = 6
        end = 6 + data_len
        for _ in range(n_events):
            if pos + 3 > end:
                break
            evt_type = buf[pos]
            reg      = (buf[pos + 1] << 8) | buf[pos + 2]
            pos += 3
            if evt_type in (FMB_EVT_COIL, FMB_EVT_DISCRETE):
                if pos >= end:
                    break
                val = buf[pos]; pos += 1
            elif evt_type in (FMB_EVT_HOLDING, FMB_EVT_INPUT):
                if pos + 1 >= end:
                    break
                val = (buf[pos] << 8) | buf[pos + 1]; pos += 2
            elif evt_type == FMB_EVT_REBOOT:
                val = -1
            else:
                break
            events.append((slave_id, evt_type, reg, val))

        self._ack_slave = slave_id
        self._ack_flag  = flag
        return True, events

    # --- MQTT dispatch -------------------------------------------------------

    def _dispatch(self, slave_id: int, evt_type: int, reg: int, val: int) -> None:
        dev = self._devices.get(slave_id)
        if not dev:
            return
        did = dev["id"]

        if evt_type == FMB_EVT_COIL and 1 <= reg <= dev["do"]:
            self._pub.pub_control(did, f"do_{reg}", str(val))
            self._pub.pub_error(did, f"do_{reg}", "")

        elif evt_type == FMB_EVT_INPUT:
            # DI states: Input Reg 18..(17+di_count) — arrive as FMB_EVT_INPUT per MODBUS_VARIABLES
            # (NOT as FMB_EVT_DISCRETE; configure_events type 0x03=INPUT, same address as FC04 reg 18+)
            if dev["di"] > 0 and 18 <= reg < 18 + dev["di"]:
                di_n = reg - 17
                self._pub.pub_control(did, f"di_{di_n}", str(val & 1))
                self._pub.pub_error(did, f"di_{di_n}", "")
            # AI events (403,410,...) are not configured via configure_events, so they don't
            # arrive at HIGH priority. AI values are published by periodic polling every 5s.

        elif evt_type == FMB_EVT_HOLDING and 33 <= reg < 33 + dev["ao"]:
            ao_n = reg - 32
            self._pub.pub_control(did, f"ao_{ao_n}", str(val))
            self._pub.pub_error(did, f"ao_{ao_n}", "")

        elif evt_type == FMB_EVT_REBOOT:
            self._log.info("Device addr=%d rebooted (event 0x0F)", slave_id)

    # --- Run loop ------------------------------------------------------------

    def run(self) -> None:
        ser = get_port(self._port_path, self._baudrate)
        time.sleep(2)

        # Configure events for all registered devices
        for addr, dev in self._devices.items():
            for attempt in range(3):
                if self._configure_device(ser, addr, dev):
                    dev["configured"] = True
                    self._log.info("FMB events configured addr=%d (%s)", addr, dev["id"])
                    break
                time.sleep(0.5)
            if not dev["configured"]:
                self._log.warning("FMB events config failed addr=%d — polling only", addr)

        while not self._stop.is_set():
            try:
                had_events, events = self._poll_once(ser)
                for (slave_id, evt_type, reg, val) in events:
                    self._dispatch(slave_id, evt_type, reg, val)
                # If had events, poll again immediately; otherwise rest 50ms
                if not had_events:
                    time.sleep(0.05)
            except Exception as e:
                self._log.debug("event loop error: %s", e)
                time.sleep(0.1)

    def stop(self) -> None:
        self._stop.set()


# ── MQTTPublisher ──────────────────────────────────────────────────────────────
class MQTTPublisher:
    """Wiren Board MQTT publisher with availability tracking (wb-mqtt-serial style).

    Reliability features modelled on wb-mqtt-serial:
      * Last Will Testament — broker marks the bridge device offline if the
        process crashes or loses its connection, so consumers never trust
        stale retained data.
      * Per-device availability — a whole device is flagged offline via
        ``/devices/<id>/meta/error = "r"`` when it stops answering, and cleared
        on recovery (driven by the pollers' error back-off state machine).
      * Bridge status device — ``/devices/<bridge_id>/...`` exposes connection
        state and online/total device counters for monitoring.
    """

    def __init__(self, cfg: dict):
        self._broker = cfg.get("broker", "127.0.0.1")
        self._port   = int(cfg.get("port", 1883))
        self._client_id = cfg.get("client_id", "sa02m-modbus-bridge")
        self._qos    = int(cfg.get("qos", 1))
        self._retain = bool(cfg.get("retain", True))
        self._reconnect_delay = int(cfg.get("reconnect_delay_s", 5))
        self._availability = bool(cfg.get("availability", True))
        self._bridge_id = cfg.get("bridge_device_id", "sa02m-bridge")
        self._username = cfg.get("username") or None
        self._password = cfg.get("password") or None
        self._lock   = threading.Lock()

        # Availability bookkeeping
        self._device_online: dict[str, bool] = {}
        self._poll_errors = 0
        self._bridge_meta_done = False

        self._client = mqtt.Client(
            client_id=self._client_id,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
        )
        if self._username:
            self._client.username_pw_set(self._username, self._password)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        # Last Will: MQTT allows exactly ONE will per connection, so use the
        # bridge device-level error as the unified offline signal — a monitor
        # watching /devices/+/meta/error catches a bridge crash the same way it
        # catches a single device going offline. ``connection`` is published
        # actively (1 while running, 0 on graceful stop).
        if self._availability:
            self._client.will_set(
                f"{DEVICE_BASE}/{self._bridge_id}/meta/error", "r",
                qos=1, retain=True,
            )
        self._connected = False
        # Track subscriptions for re-subscribe on reconnect
        self._subscriptions: dict[str, callable] = {}

    @property
    def bridge_id(self) -> str:
        return self._bridge_id

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            log.info("MQTT connected to %s:%d", self._broker, self._port)
            # Re-subscribe
            for topic, cb in self._subscriptions.items():
                client.subscribe(topic, qos=1)
                client.message_callback_add(topic, cb)
            # (Re)announce bridge availability after every (re)connect.
            if self._availability:
                self._publish_bridge_status(online=True)
        else:
            log.warning("MQTT connect failed rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            log.warning("MQTT unexpected disconnect rc=%d — reconnecting", rc)

    def connect(self) -> None:
        while True:
            try:
                self._client.connect(self._broker, self._port, keepalive=60)
                self._client.loop_start()
                # Wait up to 5s for connection
                for _ in range(50):
                    if self._connected:
                        return
                    time.sleep(0.1)
                return
            except Exception as e:
                log.error("MQTT connect error: %s — retry in %ds", e, self._reconnect_delay)
                time.sleep(self._reconnect_delay)

    def pub(self, topic: str, payload: str, retain: bool | None = None) -> None:
        r = self._retain if retain is None else retain
        try:
            self._client.publish(topic, payload, qos=self._qos, retain=r)
        except Exception as e:
            log.debug("MQTT publish %s: %s", topic, e)

    def pub_meta(self, device_id: str, key: str, value: str) -> None:
        self.pub(f"{DEVICE_BASE}/{device_id}/meta/{key}", value, retain=True)

    def pub_control(self, device_id: str, name: str, value: str) -> None:
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}", value)

    def pub_control_meta(self, device_id: str, name: str,
                         key: str, value: str) -> None:
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}/meta/{key}",
                 value, retain=True)

    def pub_error(self, device_id: str, name: str, error: str) -> None:
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}/meta/error",
                 error, retain=True)

    def pub_device_error(self, device_id: str, error: str) -> None:
        """Device-level error flag (wb-mqtt-serial: whole device offline = "r")."""
        self.pub(f"{DEVICE_BASE}/{device_id}/meta/error", error, retain=True)

    # --- Availability registry -------------------------------------------------

    def register_device(self, device_id: str) -> None:
        with self._lock:
            self._device_online.setdefault(device_id, True)

    def device_online(self, device_id: str, online: bool) -> None:
        """Update one device's online state; refresh bridge counters on change."""
        with self._lock:
            changed = self._device_online.get(device_id) != online
            self._device_online[device_id] = online
        if not online:
            with self._lock:
                self._poll_errors += 1
        if changed and self._availability:
            self.pub_device_error(device_id, "" if online else "r")
            self._publish_bridge_status(online=True)

    def _publish_bridge_status(self, online: bool) -> None:
        if not self._availability:
            return
        if not self._bridge_meta_done:
            self.pub_meta(self._bridge_id, "name", "SA-02m Modbus→MQTT bridge")
            self.pub_meta(self._bridge_id, "driver", "sa02m-modbus-mqtt")
            for ctrl, ctype in (("connection", "switch"),
                                ("devices_total", "value"),
                                ("devices_online", "value"),
                                ("poll_errors", "value")):
                self.pub_control_meta(self._bridge_id, ctrl, "type", ctype)
                self.pub_control_meta(self._bridge_id, ctrl, "readonly", "1")
            self._bridge_meta_done = True
        with self._lock:
            total = len(self._device_online)
            up = sum(1 for v in self._device_online.values() if v)
            errors = self._poll_errors
        self.pub(f"{DEVICE_BASE}/{self._bridge_id}/controls/connection",
                 "1" if online else "0", retain=True)
        self.pub_control(self._bridge_id, "devices_total", str(total))
        self.pub_control(self._bridge_id, "devices_online", str(up))
        self.pub_control(self._bridge_id, "poll_errors", str(errors))
        self.pub_device_error(self._bridge_id, "" if online else "r")

    def announce_bridge(self) -> None:
        self._publish_bridge_status(online=True)

    def shutdown(self, device_ids: list[str]) -> None:
        """Graceful offline: mark bridge + all devices offline, then disconnect."""
        if self._availability:
            for did in device_ids:
                self.pub_device_error(did, "r")
            self._publish_bridge_status(online=False)
        time.sleep(0.2)   # let final publishes flush
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def subscribe_writeback(self, device_id: str, name: str, callback) -> None:
        topic = f"{DEVICE_BASE}/{device_id}/controls/{name}/on"
        self._subscriptions[topic] = callback
        self._client.subscribe(topic, qos=1)
        self._client.message_callback_add(topic, callback)


# ── Base device poller ─────────────────────────────────────────────────────────
class DevicePoller:
    def __init__(self, cfg: dict, pub: MQTTPublisher):
        self.cfg       = cfg
        self.pub       = pub
        self.device_id = cfg["id"]
        self.port_path = cfg.get("port", "/dev/COM1")
        self.baudrate  = int(cfg.get("baudrate", 115200))
        self.address   = int(cfg.get("address", 1))
        self._stop     = threading.Event()
        self._meta_ok  = False
        self.log       = logging.getLogger(f"dev.{self.device_id}")
        # Availability / error back-off (wb-mqtt-serial style). A device that
        # stops answering must not keep hammering the shared half-duplex RS-485
        # bus and starving healthy devices, so failed reads back off
        # exponentially up to ``backoff_max_s`` and the device is flagged
        # offline (device-level meta/error="r") after ``offline_after_fails``.
        self._fail_threshold = max(1, int(cfg.get("offline_after_fails", 3)))
        self._backoff_base_s = float(cfg.get("backoff_base_s", 2.0))
        self._backoff_max_s  = float(cfg.get("backoff_max_s", 30.0))
        self._fail_count     = 0
        self._online         = True
        self._backoff_until  = 0.0

    def get_port(self) -> ModbusSerial:
        return get_port(self.port_path, self.baudrate)

    def publish_device_meta(self, name: str, driver: str = "modbus-rtu") -> None:
        if self._meta_ok:
            return
        self.pub.pub_meta(self.device_id, "name", name)
        self.pub.pub_meta(self.device_id, "driver", driver)
        self._meta_ok = True

    # --- Availability state machine ------------------------------------------

    def mark_ok(self) -> None:
        """A read succeeded — device is alive again."""
        self._fail_count = 0
        self._backoff_until = 0.0
        if not self._online:
            self._online = True
            self.log.info("device back online")
            self.pub.device_online(self.device_id, True)

    def mark_fail(self) -> None:
        """A read failed — count it and (once past threshold) go offline + back off."""
        self._fail_count += 1
        if self._fail_count >= self._fail_threshold:
            over = self._fail_count - self._fail_threshold
            delay = min(self._backoff_base_s * (2 ** over), self._backoff_max_s)
            self._backoff_until = time.monotonic() + delay
            if self._online:
                self._online = False
                self.log.warning("device offline after %d failed reads — "
                                  "backing off polling", self._fail_count)
                self.pub.device_online(self.device_id, False)

    def in_backoff(self) -> bool:
        return time.monotonic() < self._backoff_until

    def _io(self, fn, *args):
        """Run a Modbus read and feed the availability state machine."""
        try:
            res = fn(*args)
            self.mark_ok()
            return res
        except Exception:
            self.mark_fail()
            raise

    # Read wrappers that drive availability (writes do not affect device online state).
    def read_coils(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_coils, addr, start, count)

    def read_discrete_inputs(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_discrete_inputs, addr, start, count)

    def read_holding_registers(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_holding_registers, addr, start, count)

    def read_input_registers(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_input_registers, addr, start, count)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        raise NotImplementedError


# ── MR-02m poller ─────────────────────────────────────────────────────────────
class MR02mPoller(DevicePoller):

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._mod_type: int | None = None
        self._do = self._di = self._ao = self._ai = 0
        self._poll_do_di_s  = float(cfg.get("poll_do_di_s",  1))
        self._poll_ai_ao_s  = float(cfg.get("poll_ai_ao_s",  5))
        self._poll_diag_s   = float(cfg.get("poll_diag_s",  60))
        self._channels      = cfg.get("channels", {})
        self._ai_types: dict[int, int] = {}

    def _ch_cfg(self, kind: str, ch: int) -> dict:
        for e in self._channels.get(kind, []):
            if isinstance(e, dict) and e.get("ch") == ch:
                return e
        return {}

    def _ch_enabled(self, kind: str, ch: int) -> bool:
        return self._ch_cfg(kind, ch).get("enabled", True)

    def _ch_label(self, kind: str, ch: int) -> str:
        return self._ch_cfg(kind, ch).get("label", f"{kind.upper()}{ch}")

    def _init_module(self) -> bool:
        try:
            regs = self.read_input_registers(self.address, 0, 1)
            mt = regs[0]
            if mt not in MR02M_MODULE_TYPES:
                self.log.error("Unknown module_type=%d", mt)
                return False
            self._mod_type = mt
            self._do, self._di, self._ao, self._ai = MR02M_MODULE_TYPES[mt]
            type_name = MR02M_TYPE_NAMES.get(mt, str(mt))
            name = self.cfg.get(
                "name",
                f"MR-02m {type_name} ({self.port_path.replace('/dev/','')} addr={self.address})"
            )
            self.publish_device_meta(name)
            self.pub.pub_control(self.device_id, "module_type", type_name)
            self.pub.pub_control_meta(self.device_id, "module_type", "type", "text")
            self.log.info("type=%d(%s) do=%d di=%d ao=%d ai=%d",
                          mt, type_name, self._do, self._di, self._ao, self._ai)
            self._publish_channel_meta()
            return True
        except Exception as e:
            self.log.error("init_module: %s", e)
            return False

    def _publish_channel_meta(self) -> None:
        for i in range(1, self._do + 1):
            n = f"do_{i}"
            self.pub.pub_control_meta(self.device_id, n, "type", "switch")
            self.pub.pub_control_meta(self.device_id, n, "order", str(i))
            lbl = self._ch_label("do", i)
            if lbl != f"DO{i}":
                self.pub.pub_control_meta(self.device_id, n, "title", lbl)

        for i in range(1, self._di + 1):
            n = f"di_{i}"
            self.pub.pub_control_meta(self.device_id, n, "type", "switch")
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            lbl = self._ch_label("di", i)
            if lbl != f"DI{i}":
                self.pub.pub_control_meta(self.device_id, n, "title", lbl)

        for i in range(1, self._ao + 1):
            n = f"ao_{i}"
            self.pub.pub_control_meta(self.device_id, n, "type", "range")
            self.pub.pub_control_meta(self.device_id, n, "min", "0")
            self.pub.pub_control_meta(self.device_id, n, "max", "1000")
            lbl = self._ch_label("ao", i)
            if lbl != f"AO{i}":
                self.pub.pub_control_meta(self.device_id, n, "title", lbl)

        for i in range(1, self._ai + 1):
            n = f"ai_{i}"
            ch = self._ch_cfg("ai", i)
            st = int(ch.get("sensor_type", 2))  # default Pt100 2-wire
            self._ai_types[i] = st
            mqtt_type, units, _ = AI_SENSOR_TYPES.get(st, _TEMP)
            self.pub.pub_control_meta(self.device_id, n, "type", mqtt_type)
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            if units:
                self.pub.pub_control_meta(self.device_id, n, "units", units)
            lbl = self._ch_label("ai", i)
            if lbl != f"AI{i}":
                self.pub.pub_control_meta(self.device_id, n, "title", lbl)

    def _poll_do_di(self) -> None:
        if self._do > 0:
            try:
                coils = self.read_coils(self.address, 1, self._do)
                for i, v in enumerate(coils, 1):
                    if self._ch_enabled("do", i):
                        self.pub.pub_control(self.device_id, f"do_{i}", str(v))
                        self.pub.pub_error(self.device_id, f"do_{i}", "")
            except Exception as e:
                self.log.warning("DO read: %s", e)
                for i in range(1, self._do + 1):
                    self.pub.pub_error(self.device_id, f"do_{i}", "r")

        if self._di > 0:
            try:
                inputs = self.read_discrete_inputs(self.address, 18, self._di)
                for i, v in enumerate(inputs, 1):
                    if self._ch_enabled("di", i):
                        self.pub.pub_control(self.device_id, f"di_{i}", str(v))
                        self.pub.pub_error(self.device_id, f"di_{i}", "")
            except Exception as e:
                self.log.warning("DI read: %s", e)
                for i in range(1, self._di + 1):
                    self.pub.pub_error(self.device_id, f"di_{i}", "r")

    def _poll_ai_ao(self) -> None:
        if self._ao > 0:
            try:
                regs = self.read_holding_registers(self.address, 33, self._ao)
                for i, v in enumerate(regs, 1):
                    if self._ch_enabled("ao", i):
                        self.pub.pub_control(self.device_id, f"ao_{i}", str(v))
                        self.pub.pub_error(self.device_id, f"ao_{i}", "")
            except Exception as e:
                self.log.warning("AO read: %s", e)
                for i in range(1, self._ao + 1):
                    self.pub.pub_error(self.device_id, f"ao_{i}", "r")

        for i in range(1, self._ai + 1):
            if not self._ch_enabled("ai", i):
                continue
            base = 400 + (i - 1) * 7
            try:
                regs = self.read_holding_registers(self.address, base, 7)
                # regs[0]=type, [1/2]=base int32, [3]=scaled, [4]=cal, [5]=hi, [6]=lo
                raw = regs[3]
                if raw >= 0x8000:
                    raw -= 0x10000
                _, _, scale = AI_SENSOR_TYPES.get(self._ai_types.get(i, 2), _TEMP)
                self.pub.pub_control(self.device_id, f"ai_{i}", str(round(raw * scale, 3)))
                self.pub.pub_error(self.device_id, f"ai_{i}", "")
            except Exception as e:
                self.log.debug("AI ch%d: %s", i, e)
                self.pub.pub_error(self.device_id, f"ai_{i}", "r")

    def _poll_diag(self) -> None:
        try:
            # Uptime: reg 105 = LSW, reg 106 = MSW
            r = self.read_input_registers(self.address, 105, 2)
            self.pub.pub_control(self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass
        try:
            # Serial: reg 270 = LSW, reg 271 = MSW
            r = self.read_input_registers(self.address, 270, 2)
            self.pub.pub_control(self.device_id, "serial", str(r[0] | (r[1] << 16)))
        except Exception:
            pass
        # DI pulse counters (if enabled in channel config)
        if self._di > 0:
            for i in range(1, min(self._di + 1, 15)):
                ch = self._ch_cfg("di", i)
                if not ch.get("counter", False):
                    continue
                try:
                    # DI1 counter: reg 77 (LSW), 78 (MSW)
                    base = 77 + (i - 1) * 2
                    r = self.read_input_registers(self.address, base, 2)
                    self.pub.pub_control(self.device_id, f"di_{i}_count",
                                         str(r[0] | (r[1] << 16)))
                except Exception:
                    pass

    def _setup_writeback(self) -> None:
        for i in range(1, self._do + 1):
            def make_cb(ch: int):
                def cb(client, userdata, msg):
                    try:
                        v = msg.payload.decode().strip()
                        on = v not in ("0", "false", "False", "")
                        self.get_port().write_coil(self.address, ch, on)
                        self.pub.pub_control(self.device_id, f"do_{ch}", "1" if on else "0")
                        self.log.info("writeback DO%d=%d", ch, on)
                    except Exception as e:
                        self.log.warning("writeback DO%d: %s", ch, e)
                        self.pub.pub_error(self.device_id, f"do_{ch}", "w")
                return cb
            self.pub.subscribe_writeback(self.device_id, f"do_{i}", make_cb(i))

        for i in range(1, self._ao + 1):
            def make_ao_cb(ch: int):
                def cb(client, userdata, msg):
                    try:
                        v = int(float(msg.payload.decode().strip()))
                        v = max(0, min(1000, v))
                        self.get_port().write_register(self.address, 32 + ch, v)
                        self.pub.pub_control(self.device_id, f"ao_{ch}", str(v))
                        self.log.info("writeback AO%d=%d", ch, v)
                    except Exception as e:
                        self.log.warning("writeback AO%d: %s", ch, e)
                        self.pub.pub_error(self.device_id, f"ao_{ch}", "w")
                return cb
            self.pub.subscribe_writeback(self.device_id, f"ao_{i}", make_ao_cb(i))

    def run(self) -> None:
        time.sleep(1)
        while not self._stop.is_set():
            if self._init_module():
                break
            time.sleep(5)
        if self._stop.is_set():
            return

        self._setup_writeback()
        t_do_di = t_ai_ao = t_diag = 0.0

        while not self._stop.is_set():
            if self.in_backoff():
                time.sleep(0.1)
                continue
            now = time.monotonic()
            if now - t_do_di >= self._poll_do_di_s:
                self._poll_do_di()
                t_do_di = now
            if now - t_ai_ao >= self._poll_ai_ao_s:
                self._poll_ai_ao()
                t_ai_ao = now
            if now - t_diag >= self._poll_diag_s:
                self._poll_diag()
                t_diag = now
            time.sleep(0.05)


# ── cyntron-dtv (RTU-Sensor) poller ───────────────────────────────────────────
class DTVPoller(DevicePoller):
    # All input registers 1-30 plus MCU diagnostics 123-124
    DTV_REGS: dict[int, tuple[str, float, str, str]] = {
        1:   ("temp_ds18b20",        0.1,   "temperature",  "°C"),
        2:   ("temp_mcp9808",        0.1,   "temperature",  "°C"),
        3:   ("temp_hdc1080",        0.1,   "temperature",  "°C"),
        4:   ("temp_bme280",         0.1,   "temperature",  "°C"),
        5:   ("temp_bme680",         0.1,   "temperature",  "°C"),
        6:   ("temp_ext",            0.1,   "temperature",  "°C"),
        7:   ("humidity_hdc1080",    0.1,   "rel_humidity", "%"),
        8:   ("humidity_bme280",     0.1,   "rel_humidity", "%"),
        9:   ("humidity_bme680",     0.1,   "rel_humidity", "%"),
        10:  ("pressure_bme280_mmhg",1.0,   "pressure",     "mmHg"),
        11:  ("pressure_bme680_mmhg",1.0,   "pressure",     "mmHg"),
        12:  ("pressure_bme280_kpa", 0.01,  "pressure",     "kPa"),
        13:  ("pressure_bme680_kpa", 0.01,  "pressure",     "kPa"),
        14:  ("altitude_bme280",     1.0,   "value",        "m"),
        15:  ("altitude_bme680",     1.0,   "value",        "m"),
        16:  ("gas_resist_bme680",   1.0,   "value",        "kΩ"),
        17:  ("iaq_bme680",          1.0,   "value",        "IAQ"),
        18:  ("eco2_bme680",         1.0,   "value",        "ppm"),
        19:  ("tvoc_zmod",           0.01,  "value",        "mg/m³"),
        20:  ("iaq_zmod",            1.0,   "value",        "IAQ"),
        21:  ("eco2_zmod",           1.0,   "value",        "ppm"),
        22:  ("etoh_zmod",           0.01,  "value",        "ppm"),
        25:  ("light_pct",           1.0,   "value",        "%"),
        26:  ("input_pb2",           1.0,   "switch",       ""),
        27:  ("presence",            1.0,   "switch",       ""),
        28:  ("moving_distance",     1.0,   "value",        "cm"),
        29:  ("still_distance",      1.0,   "value",        "cm"),
        30:  ("detect_distance",     1.0,   "value",        "cm"),
        123: ("mcu_vdd",             0.01,  "voltage",      "V"),
        124: ("mcu_temp",            1.0,   "temperature",  "°C"),
    }
    DTV_COILS = {1: ("buzzer", True), 2: ("leds", True)}

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._sensors_present: set[str] = set()
        self._poll_sensors_s  = float(cfg.get("poll_sensors_s",  10))
        self._poll_presence_s = float(cfg.get("poll_presence_s",  2))
        self._poll_diag_s     = float(cfg.get("poll_diag_s",     60))
        # Optional explicit list of sensor names to poll
        explicit = cfg.get("sensors_present")
        if isinstance(explicit, list):
            self._sensors_present = set(explicit)
            self._autodetect = False
        else:
            self._autodetect = True

    def _autodetect_sensors(self) -> None:
        """Read regs 1-30 once; mark those returning non-0x8000 as present."""
        self._sensors_present = set()
        try:
            regs = self.read_input_registers(self.address, 1, 30)
            for idx in range(30):
                reg = idx + 1
                if reg not in self.DTV_REGS:
                    continue
                if regs[idx] != 0x8000:
                    self._sensors_present.add(self.DTV_REGS[reg][0])
            self.log.info("Detected sensors: %s", sorted(self._sensors_present))
        except Exception as e:
            self.log.warning("autodetect failed: %s — assuming all present", e)
            self._sensors_present = {v[0] for v in self.DTV_REGS.values()}

    def _publish_meta(self) -> None:
        name = self.cfg.get(
            "name",
            f"DTV-RS-485 ({self.port_path.replace('/dev/','')} addr={self.address})"
        )
        self.publish_device_meta(name)
        for ch_name, _, mqtt_type, units in self.DTV_REGS.values():
            if ch_name not in self._sensors_present:
                continue
            self.pub.pub_control_meta(self.device_id, ch_name, "type", mqtt_type)
            self.pub.pub_control_meta(self.device_id, ch_name, "readonly", "1")
            if units:
                self.pub.pub_control_meta(self.device_id, ch_name, "units", units)
        for coil, (ch_name, _) in self.DTV_COILS.items():
            self.pub.pub_control_meta(self.device_id, ch_name, "type", "switch")

    def _poll_sensors(self) -> None:
        # Bulk read regs 1-30
        try:
            regs = self.read_input_registers(self.address, 1, 30)
            for idx in range(30):
                reg = idx + 1
                if reg not in self.DTV_REGS:
                    continue
                ch_name, scale, _, _ = self.DTV_REGS[reg]
                if ch_name not in self._sensors_present:
                    continue
                raw = regs[idx]
                if raw == 0x8000:   # sensor absent / error
                    self.pub.pub_error(self.device_id, ch_name, "r")
                    continue
                if raw > 0x8000:    # signed: e.g. negative temperature
                    raw -= 0x10000
                self.pub.pub_control(self.device_id, ch_name, str(round(raw * scale, 3)))
                self.pub.pub_error(self.device_id, ch_name, "")
        except Exception as e:
            self.log.warning("sensor poll: %s", e)

    def _poll_coils(self) -> None:
        try:
            coils = self.read_coils(self.address, 1, 2)
            for coil_num, (ch_name, _) in self.DTV_COILS.items():
                self.pub.pub_control(self.device_id, ch_name, str(coils[coil_num - 1]))
        except Exception:
            pass

    def _poll_diag(self) -> None:
        try:
            # Uptime: reg 105 = LSW, 106 = MSW
            r = self.read_input_registers(self.address, 105, 2)
            self.pub.pub_control(self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass
        for reg, (ch_name, scale, _, _) in [(k, v) for k, v in self.DTV_REGS.items()
                                             if k in (123, 124)]:
            try:
                r = self.read_input_registers(self.address, reg, 1)
                raw = r[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                self.pub.pub_control(self.device_id, ch_name, str(round(raw * scale, 2)))
            except Exception:
                pass

    def _setup_writeback(self) -> None:
        for coil_num, (ch_name, writable) in self.DTV_COILS.items():
            if not writable:
                continue
            def make_cb(coil: int, name: str):
                def cb(client, userdata, msg):
                    try:
                        on = msg.payload.decode().strip() not in ("0", "false", "False", "")
                        self.get_port().write_coil(self.address, coil, on)
                        self.pub.pub_control(self.device_id, name, "1" if on else "0")
                    except Exception as e:
                        self.log.warning("writeback %s: %s", name, e)
                return cb
            self.pub.subscribe_writeback(self.device_id, ch_name, make_cb(coil_num, ch_name))

    def run(self) -> None:
        time.sleep(1)
        if self._autodetect:
            for _ in range(5):
                self._autodetect_sensors()
                if self._sensors_present:
                    break
                time.sleep(3)
        if not self._sensors_present:
            self._sensors_present = {v[0] for v in self.DTV_REGS.values()}

        self._publish_meta()
        self._setup_writeback()

        t_sensors = t_presence = t_diag = 0.0
        while not self._stop.is_set():
            if self.in_backoff():
                time.sleep(0.1)
                continue
            now = time.monotonic()
            if now - t_sensors >= self._poll_sensors_s:
                self._poll_sensors()
                t_sensors = now
            if now - t_presence >= self._poll_presence_s:
                self._poll_coils()
                t_presence = now
            if now - t_diag >= self._poll_diag_s:
                self._poll_diag()
                t_diag = now
            time.sleep(0.05)


# ── CE-02m-3 (3-phase energy meter) poller ────────────────────────────────────
class CE02M3Poller(DevicePoller):

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._ct_ratio      = float(cfg.get("ct_ratio", 4000)) / 1000.0  # K/1000 → multiplier
        self._phases        = cfg.get("phases", ["A", "B", "C"])
        self._per_phase_energy = bool(cfg.get("publish_per_phase_energy", False))
        self._poll_power_s  = float(cfg.get("poll_power_s",   5))
        self._poll_energy_s = float(cfg.get("poll_energy_s", 60))
        self._poll_diag_s   = float(cfg.get("poll_diag_s",  120))
        ch = cfg.get("channels_enabled", {})
        self._en_volt  = ch.get("voltages", True)
        self._en_lvolt = ch.get("line_voltages", True)
        self._en_curr  = ch.get("currents", True)
        self._en_pact  = ch.get("power_active", True)
        self._en_preac = ch.get("power_reactive", True)
        self._en_papp  = ch.get("power_apparent", False)
        self._en_pf    = ch.get("power_factor", True)
        self._en_freq  = ch.get("frequency", True)
        self._en_ener  = ch.get("energy", True)

    @staticmethod
    def _s16(v: int) -> int:
        return v - 0x10000 if v >= 0x8000 else v

    @staticmethod
    def _int32(lsw: int, msw: int) -> int:
        """Assemble signed int32 from LSW (lower address) and MSW (higher address)."""
        v = lsw | (msw << 16)
        return v - 0x100000000 if v >= 0x80000000 else v

    @staticmethod
    def _uint64(r0: int, r1: int, r2: int, r3: int) -> int:
        """Assemble uint64 from 4 regs: r0=word0 (LSW), r3=word3 (MSB)."""
        return r0 | (r1 << 16) | (r2 << 32) | (r3 << 48)

    def _publish_meta(self) -> None:
        name = self.cfg.get(
            "name",
            f"CE-02m-3 ({self.port_path.replace('/dev/','')} addr={self.address})"
        )
        self.publish_device_meta(name)
        for ph in ["a", "b", "c"]:
            for pfx, unit in [("voltage", "V"), ("current", "A"),
                               ("power", "W"), ("reactive", "var"),
                               ("apparent", "VA"), ("pf", "")]:
                n = f"{pfx}_{ph}"
                self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
                if unit:
                    self.pub.pub_control_meta(self.device_id, n, "units", unit)
        for sfx, unit in [("total", "W"), ("reactive_total", "var"),
                          ("apparent_total", "VA"), ("pf_total", "")]:
            self.pub.pub_control_meta(self.device_id, sfx, "readonly", "1")
        self.pub.pub_control_meta(self.device_id, "frequency", "units", "Hz")
        self.pub.pub_control_meta(self.device_id, "asic_temp", "units", "°C")

    def _poll_power(self) -> None:
        # Regs 500-547: 48 registers
        try:
            regs = self.read_input_registers(self.address, 500, 48)
        except Exception as e:
            self.log.warning("power poll: %s", e)
            return

        ph3 = ["a", "b", "c"]

        if self._en_volt:
            # 500-502: Uph ×0.1 V
            for i, ph in enumerate(ph3):
                if ph.upper() in self._phases:
                    self.pub.pub_control(self.device_id, f"voltage_{ph}",
                                         str(round(regs[i] * 0.1, 1)))
        if self._en_lvolt:
            # 506-508: Uline ×0.1 V (ab, bc, ca)
            for i, ln in enumerate(["ab", "bc", "ca"]):
                self.pub.pub_control(self.device_id, f"voltage_{ln}",
                                     str(round(regs[6 + i] * 0.1, 1)))

        if self._en_curr:
            # 510-512: I A,B,C ×0.001 A, apply CT ratio
            for i, ph in enumerate(ph3):
                raw = self._s16(regs[10 + i])
                self.pub.pub_control(self.device_id, f"current_{ph}",
                                     str(round(raw * 0.001 * self._ct_ratio, 3)))
            # 513: I neutral ×0.001 A
            raw_n = self._s16(regs[13])
            self.pub.pub_control(self.device_id, "current_n",
                                 str(round(raw_n * 0.001 * self._ct_ratio, 3)))

        if self._en_pact:
            # 518-525: P A,B,C,total — int32 (LSW,MSW), W
            for i, ph in enumerate(ph3):
                if ph.upper() in self._phases:
                    w = self._int32(regs[18 + i * 2], regs[19 + i * 2])
                    self.pub.pub_control(self.device_id, f"power_{ph}", str(w))
            total = self._int32(regs[24], regs[25])
            self.pub.pub_control(self.device_id, "power_total", str(total))

        if self._en_preac:
            # 526-533: Q A,B,C,total — int32, var
            for i, ph in enumerate(ph3):
                q = self._int32(regs[26 + i * 2], regs[27 + i * 2])
                self.pub.pub_control(self.device_id, f"reactive_{ph}", str(q))
            total_q = self._int32(regs[32], regs[33])
            self.pub.pub_control(self.device_id, "reactive_total", str(total_q))

        if self._en_papp:
            # 534-541: S A,B,C,total — int32, VA
            for i, ph in enumerate(ph3):
                s = self._int32(regs[34 + i * 2], regs[35 + i * 2])
                self.pub.pub_control(self.device_id, f"apparent_{ph}", str(s))
            total_s = self._int32(regs[40], regs[41])
            self.pub.pub_control(self.device_id, "apparent_total", str(total_s))

        if self._en_freq:
            # 542: freq ×0.01 Hz
            self.pub.pub_control(self.device_id, "frequency",
                                 str(round(regs[42] * 0.01, 2)))

        if self._en_pf:
            # 543-546: PF A,B,C,total — ×0.001 signed
            for i, ph in enumerate(ph3 + ["total"]):
                pf = round(self._s16(regs[43 + i]) * 0.001, 3)
                self.pub.pub_control(self.device_id, f"pf_{ph}", str(pf))

        # 547: ASIC temperature (°C, signed)
        if len(regs) > 47:
            self.pub.pub_control(self.device_id, "asic_temp",
                                 str(self._s16(regs[47])))

    def _poll_energy(self) -> None:
        if not self._en_ener:
            return
        try:
            # 580-599: 5 × uint64 (total AP, AN, RP, RN, S)
            regs = self.read_input_registers(self.address, 580, 20)
            names = ["energy_active_import", "energy_active_export",
                     "energy_reactive_import", "energy_reactive_export",
                     "energy_apparent"]
            for i, name in enumerate(names):
                val = self._uint64(regs[i*4], regs[i*4+1], regs[i*4+2], regs[i*4+3])
                self.pub.pub_control(self.device_id, name, str(val))
        except Exception as e:
            self.log.debug("energy poll: %s", e)

        if self._per_phase_energy:
            try:
                # 600-611: per-phase active import A,B,C
                regs = self.read_input_registers(self.address, 600, 12)
                for i, ph in enumerate(["a", "b", "c"]):
                    val = self._uint64(regs[i*4], regs[i*4+1], regs[i*4+2], regs[i*4+3])
                    self.pub.pub_control(self.device_id, f"energy_active_import_{ph}", str(val))
            except Exception:
                pass

    def _poll_diag(self) -> None:
        try:
            # Uptime: reg 105 = LSW, 106 = MSW
            r = self.read_input_registers(self.address, 105, 2)
            self.pub.pub_control(self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass
        try:
            r = self.read_input_registers(self.address, 123, 2)
            self.pub.pub_control(self.device_id, "mcu_vdd", str(round(r[0] * 0.01, 2)))
            self.pub.pub_control(self.device_id, "mcu_temp", str(self._s16(r[1])))
        except Exception:
            pass

    def run(self) -> None:
        time.sleep(1)
        self._publish_meta()
        t_power = t_energy = t_diag = 0.0
        while not self._stop.is_set():
            if self.in_backoff():
                time.sleep(0.1)
                continue
            now = time.monotonic()
            if now - t_power >= self._poll_power_s:
                self._poll_power()
                t_power = now
            if now - t_energy >= self._poll_energy_s:
                self._poll_energy()
                t_energy = now
            if now - t_diag >= self._poll_diag_s:
                self._poll_diag()
                t_diag = now
            time.sleep(0.05)


# ── Global state ───────────────────────────────────────────────────────────────
POLLER_CLASSES: dict[str, type] = {
    "mr02m":  MR02mPoller,
    "dtv":    DTVPoller,
    "ce02m3": CE02M3Poller,
}
_pollers:  list[DevicePoller] = []
_fmb_mgrs: list[FastModbusEventPortManager] = []
_threads:  list[threading.Thread] = []
_stop_ev   = threading.Event()


# ── Config & helpers ───────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning("Config not found: %s — bridge idle", CONFIG_PATH)
        return {"mqtt": {}, "devices": []}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {"mqtt": {}, "devices": []}


def watchdog_thread(interval_s: float) -> None:
    while not _stop_ev.is_set():
        sd_notify("WATCHDOG=1")
        time.sleep(interval_s)


def signal_handler(sig, frame) -> None:
    log.info("Signal %d received — shutting down", sig)
    _stop_ev.set()
    for p in _pollers:
        p.stop()
    for m in _fmb_mgrs:
        m.stop()


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    cfg         = load_config()
    mqtt_cfg    = cfg.get("mqtt", {})
    devices_cfg = cfg.get("devices") or []

    pub = MQTTPublisher(mqtt_cfg)
    pub.connect()
    time.sleep(0.5)
    sd_notify("READY=1")

    # Systemd watchdog
    wdg_usec = float(os.environ.get("WATCHDOG_USEC", "0"))
    if wdg_usec > 0:
        t = threading.Thread(target=watchdog_thread,
                             args=((wdg_usec / 1_000_000) / 2,), daemon=True)
        t.start()

    # Build per-port Fast Modbus managers for devices with fast_modbus=true
    fmb_ports: dict[str, FastModbusEventPortManager] = {}
    for dev_cfg in devices_cfg:
        if not dev_cfg.get("fast_modbus", False):
            continue
        dev_type = dev_cfg.get("type", "").lower()
        if dev_type != "mr02m":
            continue   # Fast Modbus events only implemented for MR-02m
        port_key = f"{dev_cfg.get('port','')}:{dev_cfg.get('baudrate', 115200)}"
        if port_key not in fmb_ports:
            fmb_ports[port_key] = FastModbusEventPortManager(
                dev_cfg["port"], int(dev_cfg.get("baudrate", 115200)), pub
            )

    # Start device pollers
    for dev_cfg in devices_cfg:
        dev_type = dev_cfg.get("type", "").lower()
        cls = POLLER_CLASSES.get(dev_type)
        if cls is None:
            log.error("Unknown device type '%s' id=%s — skipping",
                      dev_type, dev_cfg.get("id", "?"))
            continue
        poller = cls(dev_cfg, pub)
        _pollers.append(poller)
        pub.register_device(dev_cfg["id"])
        t = threading.Thread(target=poller.run,
                             name=f"poll-{dev_cfg['id']}", daemon=True)
        _threads.append(t)
        t.start()
        log.info("Started %s poller for %s", dev_type, dev_cfg["id"])

        # Register in FMB manager (counts filled after init, default to max for type)
        if dev_cfg.get("fast_modbus", False) and dev_type == "mr02m":
            port_key = f"{dev_cfg.get('port','')}:{dev_cfg.get('baudrate', 115200)}"
            mgr = fmb_ports.get(port_key)
            if mgr:
                # Use explicit do/di counts or default to largest possible
                mt  = dev_cfg.get("module_type", 1)
                do, di, ao, ai = MR02M_MODULE_TYPES.get(mt, (6, 8, 0, 0))
                mgr.register_device(
                    int(dev_cfg.get("address", 1)), dev_cfg["id"],
                    dev_type, do, di, ao, ai
                )

    # Start Fast Modbus event managers
    for mgr in fmb_ports.values():
        _fmb_mgrs.append(mgr)
        t = threading.Thread(target=mgr.run, name=f"fmb-{mgr._port_path}",
                             daemon=True)
        _threads.append(t)
        t.start()
        log.info("Started Fast Modbus event manager for %s", mgr._port_path)

    if not _pollers:
        log.warning("No devices configured — bridge idle")

    # Announce bridge availability now that the device registry is populated.
    pub.announce_bridge()

    while not _stop_ev.is_set():
        time.sleep(1)

    # Graceful offline: tell consumers the bridge and its devices went down
    # cleanly (instead of leaving stale retained "online" data behind).
    pub.shutdown([p.device_id for p in _pollers])
    log.info("Bridge stopped")


if __name__ == "__main__":
    main()

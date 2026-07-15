#!/usr/bin/env python3
"""SA-02m Modbus→MQTT bridge v2.

Devices:  mr02m (all 13 types), dtv (RTU-Sensor), ce02m3
Protocol: standard Modbus RTU (FC01-06) + Wiren Board Fast Modbus
          (FC 0x46: scanner + event polling).
Topics:   Wiren Board MQTT convention (/devices/…/controls/…)
Config:   /etc/sa02m-modbus-mqtt.yaml  (env SA02M_MQTT_CONFIG to override)
Systemd:  sd_notify READY=1 / WATCHDOG=1
"""

from __future__ import annotations

import json as _json
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
LIVE_CACHE_DIR = Path(os.environ.get("SA02M_MQTT_LIVE_CACHE", "/run/sa02m-modbus-mqtt"))


class DeviceLiveCache:
    """Снимок последних значений controls для быстрого mqtt_live.cgi (<10 ms)."""

    _lock = threading.Lock()
    _controls: dict[str, dict[str, str]] = {}
    _units: dict[str, dict[str, str]] = {}
    _errors: dict[str, dict[str, str]] = {}
    _sensor_types: dict[str, dict[str, str]] = {}

    @classmethod
    def set_control(cls, device_id: str, name: str, value: str) -> None:
        with cls._lock:
            cls._controls.setdefault(device_id, {})[name] = value

    @classmethod
    def set_unit(cls, device_id: str, name: str, units: str) -> None:
        if not units:
            return
        with cls._lock:
            cls._units.setdefault(device_id, {})[name] = units

    @classmethod
    def set_error(cls, device_id: str, name: str, err: str) -> None:
        with cls._lock:
            bucket = cls._errors.setdefault(device_id, {})
            if err:
                bucket[name] = err
            else:
                bucket.pop(name, None)

    @classmethod
    def set_sensor_type(cls, device_id: str, ai_index: int, code: int) -> None:
        with cls._lock:
            cls._sensor_types.setdefault(device_id, {})[f"ai_{ai_index}"] = str(code)

    @classmethod
    def flush_file(cls, device_id: str) -> None:
        with cls._lock:
            controls = dict(cls._controls.get(device_id, {}))
            units = dict(cls._units.get(device_id, {}))
            errors = dict(cls._errors.get(device_id, {}))
            sensor_types = dict(cls._sensor_types.get(device_id, {}))
        if not controls and not units and not sensor_types:
            return
        try:
            LIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = LIVE_CACHE_DIR / f"{device_id}.json"
            tmp = path.with_suffix(".json.tmp")
            payload = {
                "ok": True,
                "device": device_id,
                "source": "cache",
                "controls": controls,
                "units": units,
                "errors": errors,
                "sensor_types": sensor_types,
                "ts": time.time(),
            }
            tmp.write_text(
                _json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.debug("live cache %s: %s", device_id, e)

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
# Holding 400+7*(ch-1): reg0 = ai_sensor_t (MODBUS_VARIABLES / module_profiles)
MR02M_AI_HOLDING_BASE = 400
MR02M_AI_CHANNEL_STRIDE = 7
# Пауза на RS-485 между кадрами (Modbus T3.5 + время обработки slave), как в flasher send_receive.
MODBUS_INTER_FRAME_DELAY_S = 0.05
# Доп. пауза перед AO после крупного FC03 AI (6AO6AI6: 42 рег.) — время обработки slave.
MODBUS_POST_AI_BLOCK_GAP_S = 0.05
MR02M_TYPE_NAMES: dict[int, str] = {
    1: "DO6DI8", 2: "DO16", 3: "AO12", 4: "DO6", 5: "DI14",
    6: "AO6AI6", 7: "AI12", 8: "DO4DI6", 9: "TENZO2",
    10: "10DIcon", 11: "6DO5DI2AO", 12: "AI6AO2", 15: "4TO6DI",
}
# MR/MP-02m MCU diagnostics (как sa02m_flasher device_config / module_config_window)
MR_MCU_HOLD_OP_DAYS = 114
MR_MCU_HOLD_POWER_TEMP = 123
MR_INP_MCU_UPTIME_LO = 105
MR_INP_DI_CNT_BASE = 77
MR_INP_MCU_DIAG_START = 65505
# Коды как в MR-02m decode_reset_csr / Input 65508 (MODBUS_VARIABLES.txt, приоритет LPWR→…→V18PWR).
MR_RESET_REASON_LABELS: dict[int, str] = {
    0: "неизвестно",
    1: "LPWR",
    2: "WWDG",
    3: "IWDG",
    4: "SW-сброс",
    5: "POR/PDR",
    6: "NRST",
    7: "OBL",
    8: "V18PWR",
}
# (control_name, mqtt_type, units, title_ru)
MR02M_SYS_CONTROLS: tuple[tuple[str, str, str, str], ...] = (
    ("uptime_s", "value", "", "Время работы"),
    ("serial", "text", "", "Серийный номер"),
    ("mcu_temp", "temperature", "°C", "Температура МК"),
    ("mcu_vdd", "voltage", "V", "Питание МК"),
    ("op_days", "value", "дн", "Наработка"),
    ("mcu_ram_free", "value", "B", "ОЗУ свободно"),
    ("mcu_ram_used", "value", "B", "ОЗУ занято"),
    ("reset_reason", "text", "", "Причина перезагрузки"),
    ("fw_updates", "value", "", "Счётчик обновлений FW"),
)
# 6AO6AI6: N-нога берёт тип с P только для ТХА и 3-проводного RTD (как прошивальщик).
AI_RTD_CODES_3_WIRE = frozenset(range(21, 34))
AI_TC_K_CODE = 41

# Legacy ai_sensor_t enum (0x00..0x26) → Modbus selection codes 0..42 (MR-02m ≥1.0.9.1).
# Source: opt/sa02m-flasher/sa02m_flasher/module_profiles.py AI_SENSOR_LEGACY_ENUM_MIGRATION
AI_SENSOR_LEGACY_ENUM_MIGRATION: dict[int, int] = {
    0x00: 0, 0x01: 3, 0x02: 11, 0x03: 9, 0x04: 34, 0x05: 40, 0x06: 41, 0x07: 42,
    0x08: 8, 0x09: 10, 0x0A: 7, 0x0B: 4, 0x0C: 5, 0x0D: 6, 0x0E: 13, 0x0F: 14,
    0x10: 16, 0x11: 17, 0x12: 18, 0x13: 19, 0x14: 20, 0x15: 38, 0x16: 39, 0x17: 36,
    0x18: 37, 0x19: 2, 0x1A: 1, 0x1B: 21, 0x1C: 22, 0x1D: 23, 0x1E: 24, 0x1F: 26,
    0x20: 27, 0x21: 29, 0x22: 30, 0x23: 31, 0x24: 32, 0x25: 33, 0x26: 35,
}
AI_SENSOR_SCHEMA_MODBUS = 2


def _migrate_legacy_ai_sensor_code(code: int) -> int:
    c = int(code) & 0xFFFF
    if c in AI_SENSOR_LEGACY_ENUM_MIGRATION:
        return AI_SENSOR_LEGACY_ENUM_MIGRATION[c]
    if 0 <= c <= 42:
        return c
    return 0


def _ai_register_is_legacy_enum(code: int) -> bool:
    c = int(code) & 0xFFFF
    mapped = AI_SENSOR_LEGACY_ENUM_MIGRATION.get(c)
    return mapped is not None and mapped != c


def _resolve_ai_sensor_type(bus_st: int, yaml_st: int | None) -> int:
    """Pick effective type: YAML wins over legacy garbage in holding reg0."""
    bus = int(bus_st) & 0xFFFF
    yaml = int(yaml_st) if yaml_st is not None else None
    if _ai_register_is_legacy_enum(bus):
        if yaml is not None:
            return yaml & 0xFFFF
        return _migrate_legacy_ai_sensor_code(bus)
    if bus != 0:
        return bus
    if yaml is not None:
        return yaml & 0xFFFF
    return 0


def _migrate_config_ai_sensor_types(cfg: dict) -> None:
    """One-time migration: legacy enum in YAML → Modbus codes 0..42."""
    for dev in cfg.get("devices") or []:
        if not isinstance(dev, dict):
            continue
        if str(dev.get("type", "")).lower() != "mr02m":
            continue
        schema = int(dev.get("ai_sensor_schema") or 1)
        if schema >= AI_SENSOR_SCHEMA_MODBUS:
            continue
        channels = dev.get("channels")
        if not isinstance(channels, dict):
            continue
        ai_list = channels.get("ai")
        if not isinstance(ai_list, list):
            continue
        for entry in ai_list:
            if not isinstance(entry, dict) or "sensor_type" not in entry:
                continue
            old = int(entry["sensor_type"]) & 0xFFFF
            entry["sensor_type"] = _migrate_legacy_ai_sensor_code(old)
        dev["ai_sensor_schema"] = AI_SENSOR_SCHEMA_MODBUS
        log.info("Migrated legacy AI sensor_type codes for device %s",
                 dev.get("id", "?"))


# AI sensor Modbus selection codes 0..42 → (mqtt_type, units, scale)
# Scaled register units per MODBUS_VARIABLES.txt / MR-02m README (коды 0..42):
#   Temperature sensors:     0.1 °C  (reg × 0.1 = °C)
#   VOLTAGE_10V  (code 34):  mV  (0..10000),  reg × 0.001 = V
#   VOLTAGE_30V  (code 35):  0.01 V (0..3000), reg × 0.01 = V
#   CURRENT_4_20 (code 40):  0.01 mA (0..2000), reg × 0.01 = mA
#   CURRENT_0_5  (code 38):  0.01 mA (0..500),  reg × 0.01 = mA
#   CURRENT_0_20 (code 39):  0.01 mA (0..2000), reg × 0.01 = mA
#   DRY_CONTACT  (code 42):  0 or 1
#   DIFF_50MV    (code 36):  raw mV (user-calibrated limits), reg × 1.0 = mV
#   DIFF_2V      (code 37):  raw 0.001 V (user-calibrated limits), reg × 0.001 = V
_TEMP = ("temperature", "°C", 0.1)
AI_SENSOR_TYPES: dict[int, tuple[str, str, float]] = {
    0:  ("value",       "",    1.0),    # Disabled
    34: ("voltage",     "V",   0.001), # 0–10 V  (raw 0..10000 mV)
    35: ("voltage",     "V",   0.01),  # 0–30 V  (raw 0..3000 × 0.01 V)
    36: ("voltage",     "mV",  1.0),   # ±50 mV differential (raw in mV)
    37: ("voltage",     "V",   0.001), # ±2 V differential (raw × 0.001 V)
    38: ("current",     "mA",  0.01),  # 0–5 mA  (raw 0..500 × 0.01 mA)
    39: ("current",     "mA",  0.01),  # 0–20 mA (raw 0..2000 × 0.01 mA)
    40: ("current",     "mA",  0.01),  # 4–20 mA (raw 0..2000 × 0.01 mA)
    42: ("switch",      "",    1.0),   # Dry contact (0/1)
}
# Temperature-type codes (NTC/RTD/thermocouple, codes 1-33, 41):
for _code in list(range(1, 34)) + [41]:
    AI_SENSOR_TYPES.setdefault(_code, _TEMP)

# ── WB conventions: precision per units ───────────────────────────────────────
# /devices/.../controls/.../meta/precision (number of decimal places to display)
# Source: https://github.com/wirenboard/conventions
_PRECISION_BY_UNITS: dict[str, str] = {
    "°C":    "1",  "°F":    "1",
    "V":     "3",  "kV":    "3",  "mV": "1",
    "A":     "3",  "mA":    "2",
    "W":     "1",  "kW":    "3",
    "kWh":   "3",  "Wh":    "1",
    "var":   "1",  "kvar":  "3",
    "VA":    "1",  "kVA":   "3",
    "Hz":    "2",
    "%":     "1",  "%, RH": "1",
    "kPa":   "2",  "Pa":    "0",  "mbar": "1", "bar": "3", "mmHg": "0",
    "kΩ":    "2",  "Ω":     "1",
    "ppm":   "1",  "ppb":   "2",
    "mg/m³": "2",
    "IAQ":   "1",
    "cm":    "0",  "m":     "2",
}

def _ctrl_precision(units: str) -> str | None:
    """Return WB precision meta value for given units string, or None."""
    return _PRECISION_BY_UNITS.get(units)


# ── WB conventions: bilingual title helper ────────────────────────────────────
def _make_title(label_ru: str, label_en: str = "") -> str:
    """Return JSON bilingual title if both provided, else plain string."""
    if label_en:
        return _json.dumps({"ru": label_ru, "en": label_en}, ensure_ascii=False)
    return label_ru


# ── Fast Modbus event type codes (WB standard, from fast_mb_events.h) ─────────
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


def _modbus_read_frame_len(data: bytes) -> int:
    """Длина RTU-ответа FC01–04: [addr, func, byte_count, data…, crc]."""
    if len(data) < 3:
        return 0
    func = data[1]
    if func not in (0x01, 0x02, 0x03, 0x04):
        return 0
    return 3 + int(data[2]) + 2


def _rtu_char_time_s(baudrate: int) -> float:
    return 10.0 / max(baudrate, 300)


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

    def __init__(
        self,
        port: str,
        baudrate: int,
        timeout: float = 0.3,
        inter_frame_delay_s: float = MODBUS_INTER_FRAME_DELAY_S,
    ):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._inter_frame_delay_s = max(0.0, float(inter_frame_delay_s))
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()

    def _bus_gap(self) -> None:
        if self._inter_frame_delay_s > 0:
            time.sleep(self._inter_frame_delay_s)

    def _read_rtu_response(self, ser: serial.Serial, request: bytes,
                           timeout: float | None = None) -> bytes:
        """Чтение полного RTU-кадра (как sa02m-flasher send_receive), не один read(N)."""
        tlim = timeout if timeout is not None else self._timeout
        char_time = _rtu_char_time_s(self._baudrate)
        post_send = max(0.001, min(0.02, char_time * 3.5 + 0.002))
        time.sleep(post_send)
        deadline = time.monotonic() + tlim
        buf = b""
        last_recv = time.monotonic()
        silence = max(0.02, char_time * 3.5)
        while time.monotonic() < deadline:
            if ser.in_waiting:
                buf += ser.read(ser.in_waiting)
                last_recv = time.monotonic()
                if (len(request) > 0 and len(buf) > len(request)
                        and buf[:len(request)] == request):
                    buf = buf[len(request):]
                flen = _modbus_read_frame_len(buf)
                if flen and len(buf) >= flen:
                    return buf[:flen]
            elif buf and (time.monotonic() - last_recv) >= silence:
                if (len(request) > 0 and len(buf) > len(request)
                        and buf[:len(request)] == request):
                    buf = buf[len(request):]
                flen = _modbus_read_frame_len(buf)
                if flen and len(buf) >= flen:
                    return buf[:flen]
            time.sleep(0.001)
        if (len(request) > 0 and len(buf) > len(request)
                and buf[:len(request)] == request):
            buf = buf[len(request):]
        return buf

    def _ensure_open(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            open_kwargs: dict = dict(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self._timeout,
            )
            try:
                self._ser = serial.Serial(**open_kwargs, exclusive=True)
            except TypeError:
                self._ser = serial.Serial(**open_kwargs)
            time.sleep(0.05)
            try:
                self._ser.reset_input_buffer()
                self._ser.reset_output_buffer()
            except Exception:
                pass
        return self._ser

    def close(self) -> None:
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self._ser = None

    def _transact(self, request: bytes, expected: int) -> bytes:
        ser = self._ensure_open()
        try:
            self._bus_gap()
            ser.reset_input_buffer()
            ser.write(request)
            ser.flush()
            resp = self._read_rtu_response(ser, request)
            if len(resp) < expected:
                raise IOError(f"Short response: {len(resp)}/{expected} bytes")
            recv_crc = resp[-2] | (resp[-1] << 8)
            if crc16(resp[:-2]) != recv_crc:
                raise IOError(f"CRC mismatch on FC{request[1]:02X}")
            if resp[1] & 0x80:
                raise IOError(
                    f"Modbus exception {resp[2]} on FC{request[1] & 0x7F:02X}")
            return resp
        finally:
            self._bus_gap()

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
                self._bus_gap()


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
        # Последнее meta/error по каналу — не дублировать пустое «сброс ошибки» в MQTT.
        self._ctrl_errors: dict[tuple[str, str], str] = {}

        try:
            # paho-mqtt >= 2.0: use VERSION2 to avoid deprecation warning
            self._client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=self._client_id,
            )
        except (AttributeError, TypeError):
            # paho-mqtt < 2.0
            self._client = mqtt.Client(client_id=self._client_id)
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

    # paho-mqtt v1: (client, userdata, flags, rc)
    # paho-mqtt v2: (client, userdata, connect_flags, reason_code, properties)
    # Using *_ absorbs the extra `properties` arg in v2.
    def _on_connect(self, client, userdata, flags, rc, *_):
        failed = rc.is_failure if hasattr(rc, "is_failure") else bool(rc)
        if failed:
            log.warning("MQTT connect failed: %s", rc)
            return
        self._connected = True
        log.info("MQTT connected to %s:%d", self._broker, self._port)
        for topic, cb in self._subscriptions.items():
            client.subscribe(topic, qos=1)
            client.message_callback_add(topic, cb)
        if self._availability:
            self._publish_bridge_status(online=True)

    # paho-mqtt v1: (client, userdata, rc)
    # paho-mqtt v2: (client, userdata, disconnect_flags, reason_code, properties)
    def _on_disconnect(self, client, userdata, flags_or_rc, *extra):
        self._connected = False
        rc = extra[0] if extra else flags_or_rc
        unexpected = rc.is_failure if hasattr(rc, "is_failure") else bool(rc)
        if unexpected:
            log.warning("MQTT unexpected disconnect: %s — reconnecting", rc)

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
        DeviceLiveCache.set_control(device_id, name, value)

    def pub_control_meta(self, device_id: str, name: str,
                         key: str, value: str) -> None:
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}/meta/{key}",
                 value, retain=True)

    def pub_control_units(self, device_id: str, name: str, units: str) -> None:
        """Publish units + auto precision (WB conventions)."""
        if units:
            DeviceLiveCache.set_unit(device_id, name, units)
            self.pub_control_meta(device_id, name, "units", units)
            prec = _ctrl_precision(units)
            if prec is not None:
                self.pub_control_meta(device_id, name, "precision", prec)

    def pub_error(self, device_id: str, name: str, error: str) -> None:
        err = error if error else ""
        key = (device_id, name)
        with self._lock:
            prev = self._ctrl_errors.get(key)
            if prev == err:
                return
            if err == "" and prev is None:
                return
            self._ctrl_errors[key] = err
        self.pub(f"{DEVICE_BASE}/{device_id}/controls/{name}/meta/error",
                 err, retain=True)
        DeviceLiveCache.set_error(device_id, name, err)

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

    def device_online_snapshot(self) -> dict:
        """Copy of the per-device online map (device_id → bool) for the roster writer."""
        with self._lock:
            return dict(self._device_online)

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

    def write_register(self, addr: int, reg: int, value: int) -> None:
        self.get_port().write_register(addr, reg, value)

    def stop(self) -> None:
        self._stop.set()

    def setup(self) -> None:
        """Однократная инициализация (вызывается из потока порта)."""

    def poll_io(self) -> None:
        """Один проход основных измерений (в общем цикле порта)."""

    def poll_slow_if_due(self, now: float) -> None:
        """Редкий опрос (диагностика, счётчики энергии) по своим интервалам."""

    def run(self) -> None:
        raise NotImplementedError


# ── Port poll scheduler (one RS-485 line → continuous round-robin) ───────────
class PortPollScheduler:
    """
    Один поток на port:baud: без пауз между циклами — после обхода всех addr
    сразу следующий круг (скорость ограничена только Modbus/RS-485).
    """

    def __init__(self, port_path: str, baudrate: int, pollers: list[DevicePoller]):
        self._port_path = port_path
        self._baudrate = baudrate
        self._pollers = pollers
        self._stop = threading.Event()
        tag = port_path.replace("/dev/", "")
        self._log = logging.getLogger(f"port.{tag}")

    def stop(self) -> None:
        self._stop.set()
        for p in self._pollers:
            p.stop()

    def run(self) -> None:
        time.sleep(0.5)
        for p in self._pollers:
            if self._stop.is_set():
                return
            try:
                p.setup()
            except Exception as e:
                self._log.error("setup %s: %s", p.device_id, e)

        self._log.info("continuous poll on %s, %d device(s)",
                        self._port_path, len(self._pollers))

        while not self._stop.is_set():
            t0 = time.monotonic()
            polled = False
            for p in self._pollers:
                if self._stop.is_set():
                    break
                if not p.in_backoff():
                    polled = True
                    try:
                        p.poll_io()
                    except Exception as e:
                        self._log.debug("poll_io %s: %s", p.device_id, e)
                try:
                    p.poll_slow_if_due(t0)
                except Exception as e:
                    self._log.debug("poll_slow %s: %s", p.device_id, e)

            if not polled:
                # Все устройства в backoff — не крутить CPU впустую.
                if self._stop.wait(0.1):
                    break


# ── MR-02m poller ─────────────────────────────────────────────────────────────
class MR02mPoller(DevicePoller):

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._mod_type: int | None = None
        self._do = self._di = self._ao = self._ai = 0
        self._poll_diag_s   = float(cfg.get("poll_diag_s",  60))
        self._poll_uptime_s = float(cfg.get("poll_uptime_s", 5))
        self._channels      = cfg.get("channels", {})
        self._ai_types: dict[int, int] = {}
        self._t_diag        = 0.0
        self._t_uptime      = 0.0

    def _ch_cfg(self, kind: str, ch: int) -> dict:
        for e in self._channels.get(kind, []):
            if isinstance(e, dict) and e.get("ch") == ch:
                return e
        return {}

    @staticmethod
    def _ai_n_parent_ch(ch: int) -> int | None:
        """6AO6AI6: N-нога (AI2, AI4, AI6) → номер P-канала (AI1, AI3, AI5)."""
        return {2: 1, 4: 3, 6: 5}.get(ch)

    @staticmethod
    def _ai_mirror_type_from_parent(sensor_code: int) -> bool:
        c = int(sensor_code) & 0xFFFF
        return c == AI_TC_K_CODE or c in AI_RTD_CODES_3_WIRE

    def _ai_effective_sensor_type(self, ch: int) -> int | None:
        """6AO6AI6: N наследует тип P только для ТХА и 3-проводного RTD."""
        parent = self._ai_n_parent_ch(ch) if self._mod_type == 6 else None
        if parent:
            p_st = self._ch_cfg("ai", parent).get("sensor_type")
            if p_st is not None and self._ai_mirror_type_from_parent(int(p_st)):
                return int(p_st) & 0xFFFF
        st = self._ch_cfg("ai", ch).get("sensor_type")
        if st is not None:
            return int(st) & 0xFFFF
        return None

    def _ch_enabled(self, kind: str, ch: int) -> bool:
        return self._ch_cfg(kind, ch).get("enabled", True)

    def _sys_ch_cfg(self, key: str) -> dict:
        for e in self._channels.get("sys", []):
            if isinstance(e, dict) and str(e.get("key", "")) == str(key):
                return e
        return {}

    def _sys_enabled(self, key: str) -> bool:
        return self._sys_ch_cfg(key).get("enabled", True)

    def _ch_label(self, kind: str, ch: int) -> str:
        return self._ch_cfg(kind, ch).get("label", f"{kind.upper()}{ch}")

    def _ch_title(self, kind: str, ch: int, default: str) -> str | None:
        """Return bilingual JSON title or plain label, None if default."""
        cfg = self._ch_cfg(kind, ch)
        lbl = cfg.get("label", default)
        lbl_en = cfg.get("label_en", "")
        if lbl == default and not lbl_en:
            return None
        return _make_title(lbl, lbl_en)

    def _ch_enum(self, kind: str, ch: int) -> str | None:
        """Return JSON enum string if configured, e.g. {"0":"Выкл","1":"Вкл"}."""
        e = self._ch_cfg(kind, ch).get("enum")
        if e and isinstance(e, dict):
            return _json.dumps({str(k): str(v) for k, v in e.items()},
                               ensure_ascii=False)
        return None

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
            self._apply_configured_ai_sensor_types()
            return True
        except Exception as e:
            self.log.error("init_module: %s", e)
            return False

    def _apply_configured_ai_sensor_types(self) -> None:
        """Записать sensor_type из YAML в holding 400+7*(ch-1), reg0."""
        if self._ai <= 0:
            return
        for i in range(1, self._ai + 1):
            if not self._ch_enabled("ai", i):
                continue
            st = self._ai_effective_sensor_type(i)
            if st is None:
                continue
            reg = MR02M_AI_HOLDING_BASE + (i - 1) * MR02M_AI_CHANNEL_STRIDE
            for attempt in range(4):
                try:
                    cur = self.read_holding_registers(self.address, reg, 1)[0] & 0xFFFF
                    if cur != 0 and cur != st and not _ai_register_is_legacy_enum(cur):
                        self._ai_types[i] = cur
                        self.log.info(
                            "AI%d keeping device sensor_type %d (yaml %d)",
                            i, cur, st)
                        break
                    if cur == st:
                        self._ai_types[i] = st
                        break
                    self.write_register(self.address, reg, st)
                    time.sleep(0.12)
                    verify = self.read_holding_registers(self.address, reg, 1)[0] & 0xFFFF
                    if verify == st:
                        self._ai_types[i] = st
                        mqtt_type, units, _ = AI_SENSOR_TYPES.get(st, _TEMP)
                        self.pub.pub_control_meta(self.device_id, f"ai_{i}", "type", mqtt_type)
                        if units:
                            self.pub.pub_control_units(self.device_id, f"ai_{i}", units)
                        self.log.info("AI%d sensor_type %d -> %d", i, cur, st)
                        time.sleep(0.25)
                        break
                    self.log.warning("AI%d sensor_type verify %d != %d (try %d)",
                                     i, verify, st, attempt + 1)
                except Exception as e:
                    if attempt >= 3:
                        self.log.warning("AI%d sensor_type write %d: %s", i, st, e)
                    time.sleep(0.15 * (attempt + 1))
            else:
                continue
            time.sleep(0.08)

    def _publish_channel_meta(self) -> None:
        for i in range(1, self._do + 1):
            n = f"do_{i}"
            enum = self._ch_enum("do", i)
            ctrl_type = "enum" if enum else "switch"
            self.pub.pub_control_meta(self.device_id, n, "type", ctrl_type)
            self.pub.pub_control_meta(self.device_id, n, "order", str(i))
            if enum:
                self.pub.pub_control_meta(self.device_id, n, "enum", enum)
            title = self._ch_title("do", i, f"DO{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)

        for i in range(1, self._di + 1):
            n = f"di_{i}"
            enum = self._ch_enum("di", i)
            ctrl_type = "enum" if enum else "switch"
            self.pub.pub_control_meta(self.device_id, n, "type", ctrl_type)
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            if enum:
                self.pub.pub_control_meta(self.device_id, n, "enum", enum)
            title = self._ch_title("di", i, f"DI{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)
            cn = f"di_{i}_count"
            self.pub.pub_control_meta(self.device_id, cn, "type", "value")
            self.pub.pub_control_meta(self.device_id, cn, "readonly", "1")
            ct = self._ch_title("di", i, f"DI{i} счётчик")
            if ct:
                self.pub.pub_control_meta(self.device_id, cn, "title", ct)

        for i in range(1, self._ao + 1):
            n = f"ao_{i}"
            self.pub.pub_control_meta(self.device_id, n, "type", "range")
            self.pub.pub_control_meta(self.device_id, n, "min", "0")
            self.pub.pub_control_meta(self.device_id, n, "max", "1000")
            # Текущее AO в прошивке — целое ×0,01 В (как MR-02m-flasher).
            self.pub.pub_control_units(self.device_id, n, "V")
            title = self._ch_title("ao", i, f"AO{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)

        for i in range(1, self._ai + 1):
            n = f"ai_{i}"
            ch = self._ch_cfg("ai", i)
            # sensor_type in config is a hint; actual type is read from device on first poll.
            # Use -1 as sentinel meaning "not yet read from device".
            eff = self._ai_effective_sensor_type(i)
            st = int(eff) if eff is not None else -1
            self._ai_types[i] = st
            if st >= 0:
                mqtt_type, units, _ = AI_SENSOR_TYPES.get(st, _TEMP)
            else:
                mqtt_type, units = "value", ""
            self.pub.pub_control_meta(self.device_id, n, "type", mqtt_type)
            self.pub.pub_control_meta(self.device_id, n, "readonly", "1")
            if units:
                self.pub.pub_control_units(self.device_id, n, units)
            title = self._ch_title("ai", i, f"AI{i}")
            if title:
                self.pub.pub_control_meta(self.device_id, n, "title", title)

        self._publish_mr_sys_meta()

    def _publish_mr_sys_meta(self) -> None:
        for name, ctype, units, title in MR02M_SYS_CONTROLS:
            if not self._sys_enabled(name):
                continue
            self.pub.pub_control_meta(self.device_id, name, "type", ctype)
            self.pub.pub_control_meta(self.device_id, name, "readonly", "1")
            if units:
                self.pub.pub_control_units(self.device_id, name, units)
            self.pub.pub_control_meta(
                self.device_id, name, "title",
                _make_title(title, ""),
            )

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
                # DI — Input Reg 18..(17+N), FC04 (как FMB_EVT_INPUT и device_config).
                regs = self.read_input_registers(self.address, 18, self._di)
                for i, raw in enumerate(regs, 1):
                    if self._ch_enabled("di", i):
                        self.pub.pub_control(self.device_id, f"di_{i}", str(raw & 1))
                        self.pub.pub_error(self.device_id, f"di_{i}", "")
            except Exception as e:
                self.log.warning("DI read: %s", e)
                for i in range(1, self._di + 1):
                    self.pub.pub_error(self.device_id, f"di_{i}", "r")
        self._poll_di_counters()

    def _poll_di_counters(self) -> None:
        """DI pulse counters: Input Reg 77+2*(ch-1).. (uint32 lo-hi), FC04."""
        if self._di <= 0:
            return
        chs = [i for i in range(1, self._di + 1) if self._ch_enabled("di", i)]
        if not chs:
            return
        max_ch = max(chs)
        try:
            regs = self.read_input_registers(
                self.address, MR_INP_DI_CNT_BASE, max_ch * 2)
            for i in chs:
                off = (i - 1) * 2
                if off + 1 >= len(regs):
                    continue
                cnt = ((int(regs[off + 1]) & 0xFFFF) << 16
                       | (int(regs[off]) & 0xFFFF))
                self.pub.pub_control(self.device_id, f"di_{i}_count", str(cnt))
                self.pub.pub_error(self.device_id, f"di_{i}_count", "")
        except Exception as e:
            self.log.warning("DI counters: %s", e)
            for i in chs:
                self.pub.pub_error(self.device_id, f"di_{i}_count", "r")

    def _poll_ai_ao(self) -> None:
        # Сначала AI (крупный FC03), затем AO — на 6AO6AI6 первый кадр AO часто срывался без паузы.
        if self._ai > 0:
            try:
                # Один FC03 на все AI (меньше гонок на half-duplex RS-485 с несколькими addr).
                total = self._ai * MR02M_AI_CHANNEL_STRIDE
                block = self.read_holding_registers(
                    self.address, MR02M_AI_HOLDING_BASE, total)
            except Exception as e:
                self.log.warning("AI block read: %s", e)
                for i in range(1, self._ai + 1):
                    if self._ch_enabled("ai", i):
                        self.pub.pub_error(self.device_id, f"ai_{i}", "r")
            else:
                for i in range(1, self._ai + 1):
                    if not self._ch_enabled("ai", i):
                        continue
                    off = (i - 1) * MR02M_AI_CHANNEL_STRIDE
                    regs = block[off:off + MR02M_AI_CHANNEL_STRIDE]
                    bus_st = regs[0] & 0xFFFF
                    eff_st = self._ai_effective_sensor_type(i)
                    dev_st = _resolve_ai_sensor_type(bus_st, eff_st)
                    if dev_st == 0:
                        parent = self._ai_n_parent_ch(i) if self._mod_type == 6 else None
                        if parent:
                            p_off = (parent - 1) * MR02M_AI_CHANNEL_STRIDE
                            p_bus = block[p_off] & 0xFFFF
                            p_yaml = self._ai_effective_sensor_type(parent)
                            p_st = _resolve_ai_sensor_type(p_bus, p_yaml)
                            if p_st and self._ai_mirror_type_from_parent(p_st):
                                dev_st = p_st
                    DeviceLiveCache.set_sensor_type(self.device_id, i, dev_st)
                    prev_st = self._ai_types.get(i, -1)
                    if dev_st != prev_st:
                        self._ai_types[i] = dev_st
                        mqtt_type, units, _ = AI_SENSOR_TYPES.get(dev_st, _TEMP)
                        self.pub.pub_control_meta(
                            self.device_id, f"ai_{i}", "type", mqtt_type)
                        if units:
                            self.pub.pub_control_units(
                                self.device_id, f"ai_{i}", units)
                    value_ch = i
                    parent = self._ai_n_parent_ch(i) if self._mod_type == 6 else None
                    if parent and self._ai_mirror_type_from_parent(dev_st):
                        value_ch = parent
                    v_off = (value_ch - 1) * MR02M_AI_CHANNEL_STRIDE
                    raw = block[v_off + 3]
                    if raw >= 0x8000:
                        raw -= 0x10000
                    _, _, scale = AI_SENSOR_TYPES.get(
                        self._ai_types.get(i, dev_st), _TEMP)
                    self.pub.pub_control(
                        self.device_id, f"ai_{i}", str(round(raw * scale, 3)))
                    self.pub.pub_error(self.device_id, f"ai_{i}", "")

        if self._ao > 0:
            if self._ai > 0 and MODBUS_POST_AI_BLOCK_GAP_S > 0:
                time.sleep(MODBUS_POST_AI_BLOCK_GAP_S)
            regs = None
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    regs = self.read_holding_registers(self.address, 33, self._ao)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        time.sleep(MODBUS_INTER_FRAME_DELAY_S)
            if regs is not None:
                for i, v in enumerate(regs, 1):
                    if self._ch_enabled("ao", i):
                        self.pub.pub_control(self.device_id, f"ao_{i}", str(v))
                        self.pub.pub_error(self.device_id, f"ao_{i}", "")
            else:
                self.log.warning("AO read: %s", last_err)
                for i in range(1, self._ao + 1):
                    self.pub.pub_error(self.device_id, f"ao_{i}", "r")

    @staticmethod
    def _s16_word(raw: int) -> int:
        v = int(raw) & 0xFFFF
        return v - 0x10000 if v >= 0x8000 else v

    def _poll_uptime(self) -> None:
        if not self._sys_enabled("uptime_s"):
            return
        try:
            r = self.read_input_registers(self.address, MR_INP_MCU_UPTIME_LO, 2)
            self.pub.pub_control(
                self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass

    def _poll_diag(self) -> None:
        if self._sys_enabled("serial"):
            try:
                r = self.read_input_registers(self.address, 270, 2)
                self.pub.pub_control(
                    self.device_id, "serial", str(r[0] | (r[1] << 16)))
            except Exception:
                pass
        if self._sys_enabled("mcu_vdd") or self._sys_enabled("mcu_temp"):
            try:
                pt = self.read_holding_registers(
                    self.address, MR_MCU_HOLD_POWER_TEMP, 2)
                if self._sys_enabled("mcu_vdd"):
                    self.pub.pub_control(
                        self.device_id, "mcu_vdd",
                        str(round(int(pt[0]) / 100.0, 2)))
                if self._sys_enabled("mcu_temp"):
                    self.pub.pub_control(
                        self.device_id, "mcu_temp",
                        str(round(self._s16_word(pt[1]) / 10.0, 1)))
            except Exception:
                pass
        if self._sys_enabled("op_days"):
            try:
                days = self.read_holding_registers(
                    self.address, MR_MCU_HOLD_OP_DAYS, 1)[0]
                self.pub.pub_control(
                    self.device_id, "op_days", str(int(days) & 0xFFFF))
            except Exception:
                pass
        if any(self._sys_enabled(k) for k in (
                "mcu_ram_free", "mcu_ram_used", "reset_reason", "fw_updates")):
            try:
                diag = self.read_input_registers(
                    self.address, MR_INP_MCU_DIAG_START, 6)
                if len(diag) >= 2:
                    if self._sys_enabled("mcu_ram_free"):
                        self.pub.pub_control(
                            self.device_id, "mcu_ram_free",
                            str(int(diag[0]) & 0xFFFF))
                    if self._sys_enabled("mcu_ram_used"):
                        self.pub.pub_control(
                            self.device_id, "mcu_ram_used",
                            str(int(diag[1]) & 0xFFFF))
                if len(diag) >= 4 and self._sys_enabled("reset_reason"):
                    # Младший байт — reset reason; старший может содержать SPI fault (modbus_rtu_hw.c).
                    code = int(diag[3]) & 0xFF
                    self.pub.pub_control(
                        self.device_id, "reset_reason",
                        MR_RESET_REASON_LABELS.get(code, f"код {code}"))
                if len(diag) >= 6 and self._sys_enabled("fw_updates"):
                    fw = (int(diag[5]) & 0xFFFF) << 16 | (int(diag[4]) & 0xFFFF)
                    self.pub.pub_control(self.device_id, "fw_updates", str(fw))
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

    def setup(self) -> None:
        for _ in range(60):
            if self._stop.is_set():
                return
            if self._init_module():
                break
            time.sleep(5)
        self._setup_writeback()

    def poll_io(self) -> None:
        self._poll_do_di()
        self._poll_ai_ao()
        DeviceLiveCache.flush_file(self.device_id)

    def poll_slow_if_due(self, now: float) -> None:
        flushed = False
        if now - self._t_uptime >= self._poll_uptime_s:
            self._poll_uptime()
            self._t_uptime = now
            flushed = True
        if now - self._t_diag >= self._poll_diag_s:
            self._poll_diag()
            self._t_diag = now
            flushed = True
        if flushed:
            DeviceLiveCache.flush_file(self.device_id)


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
        self._poll_presence_s = float(cfg.get("poll_presence_s", 2))
        self._poll_diag_s     = float(cfg.get("poll_diag_s",     60))
        self._poll_uptime_s   = float(cfg.get("poll_uptime_s",   5))
        self._t_diag          = 0.0
        self._t_uptime        = 0.0
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
            self.pub.pub_control_units(self.device_id, ch_name, units)
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

    def _poll_uptime(self) -> None:
        try:
            r = self.read_input_registers(self.address, 105, 2)
            self.pub.pub_control(self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass

    def _poll_diag(self) -> None:
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

    def setup(self) -> None:
        if self._autodetect:
            for _ in range(5):
                if self._stop.is_set():
                    return
                self._autodetect_sensors()
                if self._sensors_present:
                    break
                time.sleep(3)
        if not self._sensors_present:
            self._sensors_present = {v[0] for v in self.DTV_REGS.values()}
        self._publish_meta()
        self._setup_writeback()

    def poll_io(self) -> None:
        self._poll_sensors()
        self._poll_coils()
        DeviceLiveCache.flush_file(self.device_id)

    def poll_slow_if_due(self, now: float) -> None:
        flushed = False
        if now - self._t_uptime >= self._poll_uptime_s:
            self._poll_uptime()
            self._t_uptime = now
            flushed = True
        if now - self._t_diag >= self._poll_diag_s:
            self._poll_diag()
            self._t_diag = now
            flushed = True
        if flushed:
            DeviceLiveCache.flush_file(self.device_id)


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
        self._poll_uptime_s = float(cfg.get("poll_uptime_s", 5))
        self._t_energy       = 0.0
        self._t_diag         = 0.0
        self._t_uptime       = 0.0
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
                self.pub.pub_control_units(self.device_id, n, unit)
        for sfx, unit in [("total", "W"), ("reactive_total", "var"),
                          ("apparent_total", "VA"), ("pf_total", "")]:
            self.pub.pub_control_meta(self.device_id, sfx, "readonly", "1")
            self.pub.pub_control_units(self.device_id, sfx, unit)
        self.pub.pub_control_units(self.device_id, "frequency", "Hz")
        self.pub.pub_control_units(self.device_id, "asic_temp", "°C")

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

    def _poll_uptime(self) -> None:
        try:
            r = self.read_input_registers(self.address, 105, 2)
            self.pub.pub_control(self.device_id, "uptime_s", str(r[0] | (r[1] << 16)))
        except Exception:
            pass

    def _poll_diag(self) -> None:
        try:
            r = self.read_input_registers(self.address, 123, 2)
            self.pub.pub_control(self.device_id, "mcu_vdd", str(round(r[0] * 0.01, 2)))
            self.pub.pub_control(self.device_id, "mcu_temp", str(self._s16(r[1])))
        except Exception:
            pass

    def setup(self) -> None:
        self._publish_meta()

    def poll_io(self) -> None:
        self._poll_power()
        DeviceLiveCache.flush_file(self.device_id)

    def poll_slow_if_due(self, now: float) -> None:
        flushed = False
        if now - self._t_uptime >= self._poll_uptime_s:
            self._poll_uptime()
            self._t_uptime = now
            flushed = True
        if now - self._t_energy >= self._poll_energy_s:
            self._poll_energy()
            self._t_energy = now
            flushed = True
        if now - self._t_diag >= self._poll_diag_s:
            self._poll_diag()
            self._t_diag = now
            flushed = True
        if flushed:
            DeviceLiveCache.flush_file(self.device_id)


# ── Global state ───────────────────────────────────────────────────────────────
POLLER_CLASSES: dict[str, type] = {
    "mr02m":  MR02mPoller,
    "dtv":    DTVPoller,
    "ce02m3": CE02M3Poller,
}
_pollers:  list[DevicePoller] = []
_port_schedulers: list[PortPollScheduler] = []
_fmb_mgrs: list[FastModbusEventPortManager] = []
_threads:  list[threading.Thread] = []
_stop_ev   = threading.Event()


# ── Config & helpers ───────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.warning("Config not found: %s — bridge idle", CONFIG_PATH)
        return {"mqtt": {}, "devices": []}
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {"mqtt": {}, "devices": []}
    _migrate_config_ai_sensor_types(cfg)
    return cfg


def watchdog_thread(interval_s: float) -> None:
    while not _stop_ev.is_set():
        sd_notify("WATCHDOG=1")
        time.sleep(interval_s)


def signal_handler(sig, frame) -> None:
    log.info("Signal %d received — shutting down", sig)
    _stop_ev.set()
    for s in _port_schedulers:
        s.stop()
    for p in _pollers:
        p.stop()
    for m in _fmb_mgrs:
        m.stop()


# ── Main ───────────────────────────────────────────────────────────────────────
# ── RS-485 roster export (Provider A source for the bus-free aggregator) ────────
ROSTER_PATH = LIVE_CACHE_DIR / "_roster.json"
_OUR_DEVICE_TYPES = ("mr02m", "dtv", "ce02m3")


def _roster_model_name(dev_type: str, module_type: int) -> str:
    """Display model for a configured bridge device, reusing the bridge's own tables."""
    if dev_type == "mr02m":
        return MR02M_TYPE_NAMES.get(int(module_type), "")
    if dev_type == "dtv":
        return "DTV-RS-45"
    if dev_type == "ce02m3":
        return "CE-02m-3"
    return ""


def _com_key_from_port(port_path: str) -> str:
    """/dev/COM4 → COM4 (the aggregator keys ports by COM label)."""
    base = os.path.basename(str(port_path or "").rstrip("/"))
    return base or str(port_path)


def write_bridge_roster(devices_cfg: list, pub: MQTTPublisher,
                        path: Path = ROSTER_PATH) -> None:
    """Emit /run/sa02m-modbus-mqtt/_roster.json — a normalized per-device roster with
    a REAL per-device online derived from the bridge's availability state machine
    (not the hardcoded controls "ok":true). Atomic tmp+replace, no bus access."""
    online = pub.device_online_snapshot()
    rows = []
    for dev_cfg in devices_cfg or []:
        dev_type = str(dev_cfg.get("type", "")).lower()
        module_type = int(dev_cfg.get("module_type", 0) or 0)
        rows.append({
            "port": _com_key_from_port(dev_cfg.get("port", "")),
            "addr": int(dev_cfg.get("address", 0) or 0),
            "type": dev_type,
            "module_type": module_type,
            "model": _roster_model_name(dev_type, module_type),
            "ours": dev_type in _OUR_DEVICE_TYPES,
            "online": bool(online.get(dev_cfg.get("id"), False)),
        })
    payload = {"ts": time.time(), "devices": rows}
    try:
        LIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.debug("bridge roster write: %s", e)


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

    # Build device pollers and group by RS-485 port (one scheduler thread per line)
    by_port: dict[str, list[DevicePoller]] = {}
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
        port_key = f"{dev_cfg.get('port', '/dev/COM1')}:{int(dev_cfg.get('baudrate', 115200))}"
        by_port.setdefault(port_key, []).append(poller)
        log.info("Registered %s poller %s on %s", dev_type, dev_cfg["id"], port_key)

        if dev_cfg.get("fast_modbus", False) and dev_type == "mr02m":
            mgr = fmb_ports.get(port_key)
            if mgr:
                mt = dev_cfg.get("module_type", 1)
                do, di, ao, ai = MR02M_MODULE_TYPES.get(mt, (6, 8, 0, 0))
                mgr.register_device(
                    int(dev_cfg.get("address", 1)), dev_cfg["id"],
                    dev_type, do, di, ao, ai
                )

    for port_key, pollers in by_port.items():
        port_path, baud_s = port_key.rsplit(":", 1)
        sched = PortPollScheduler(port_path, int(baud_s), pollers)
        _port_schedulers.append(sched)
        t = threading.Thread(target=sched.run, name=f"port-{port_path}",
                             daemon=True)
        _threads.append(t)
        t.start()
        addrs = ", ".join(str(p.address) for p in pollers)
        log.info("Started continuous port poll %s — addr [%s]", port_key, addrs)

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

    # Export the RS-485 roster (Provider A) once now, then on a periodic tick so
    # the bus-free aggregator sees a fresh real per-device online. Cheap file write.
    write_bridge_roster(devices_cfg, pub)
    _roster_interval_s = 5
    _next_roster = time.monotonic() + _roster_interval_s
    while not _stop_ev.is_set():
        time.sleep(1)
        now = time.monotonic()
        if now >= _next_roster:
            write_bridge_roster(devices_cfg, pub)
            _next_roster = now + _roster_interval_s

    # Graceful offline: tell consumers the bridge and its devices went down
    # cleanly (instead of leaving stale retained "online" data behind).
    pub.shutdown([p.device_id for p in _pollers])
    log.info("Bridge stopped")


if __name__ == "__main__":
    main()

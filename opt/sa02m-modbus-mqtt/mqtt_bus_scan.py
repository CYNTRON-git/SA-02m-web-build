#!/usr/bin/env python3
"""Modbus bus scanner for MQTT device discovery (runs as root via sudo from CGI)."""
import json
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    print(json.dumps({"ok": False, "error": "pyserial not installed", "devices": []}))
    sys.exit(0)


MR02M_MODULE_TYPES = {
    1: "DO6DI8", 2: "DO16", 3: "AO12", 4: "DO6", 5: "DI14",
    6: "AO6AI6", 7: "AI12", 8: "DO4DI6", 9: "TENZO2", 10: "10DIcon",
    11: "6DO5DI2AO", 12: "AI6AO2", 15: "4TO6DI",
}


def crc16(data):
    crc = 0xFFFF
    for b in (data if isinstance(data, (bytes, bytearray)) else bytes(data)):
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def make_pdu(addr, func, *payload):
    data = bytes([addr, func]) + bytes(payload)
    c = crc16(data)
    return data + bytes([c & 0xFF, c >> 8])


def valid_crc(pkt):
    if len(pkt) < 4:
        return False
    c = crc16(pkt[:-2])
    return (c & 0xFF) == pkt[-2] and (c >> 8) == pkt[-1]


def read_resp(ser, timeout=0.07):
    ser.timeout = timeout
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(64)
        if chunk:
            buf += chunk
    return buf


def fast_scan(ser):
    found = set()
    try:
        pdu = make_pdu(0xFD, 0x46, 0x04)
        ser.reset_input_buffer()
        ser.write(pdu)
        time.sleep(0.35)
        resp = ser.read(256)
        i = 0
        while i < len(resp) - 3:
            if resp[i] == 0xFD and resp[i + 1] == 0x46 and i + 8 <= len(resp):
                pkt = resp[i:i + 8]
                if valid_crc(pkt) and 1 <= pkt[2] <= 247:
                    found.add(pkt[2])
                i += 8
            else:
                i += 1
    except Exception:
        pass
    return found


def std_scan(ser, max_a):
    found = {}
    for addr in range(1, max_a + 1):
        pdu = make_pdu(addr, 0x03, 0x00, 0x00, 0x00, 0x01)
        try:
            ser.reset_input_buffer()
            ser.write(pdu)
            resp = read_resp(ser, timeout=0.06)
            if (len(resp) >= 7 and resp[0] == addr and resp[1] == 0x03
                    and valid_crc(resp[:7])):
                found[addr] = (resp[3] << 8) | resp[4]
        except Exception:
            continue
    return found


def read_reg(ser, addr, reg):
    pdu = make_pdu(addr, 0x03, reg >> 8, reg & 0xFF, 0x00, 0x01)
    ser.reset_input_buffer()
    ser.write(pdu)
    resp = read_resp(ser, timeout=0.08)
    if len(resp) >= 7 and resp[0] == addr and resp[1] == 0x03:
        return (resp[3] << 8) | resp[4]
    return None


def detect_type(ser, addr):
    mt = read_reg(ser, addr, 0x0100)
    if mt is not None and 1 <= mt <= 15:
        return "mr02m", mt, MR02M_MODULE_TYPES.get(mt, f"type{mt}")
    r1 = read_reg(ser, addr, 0x0001)
    if r1 == 0xCE02:
        return "ce02m3", 0, "СЭ-02м-3"
    if r1 == 0xD712:
        return "dtv", 0, "ДТВ-RS-485"
    return "mr02m", mt or 0, MR02M_MODULE_TYPES.get(mt, "unknown") if mt else "unknown"


def load_params(path: Path) -> dict:
    if path.exists() and path.stat().st_size > 0:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main() -> None:
    params_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    params = load_params(params_path) if params_path else {}
    port = str(params.get("port", "/dev/COM1"))
    baud = int(params.get("baudrate", 115200))
    max_addr = min(int(params.get("max_addr", 32)), 247)

    try:
        if not Path(port).exists():
            raise FileNotFoundError(f"Порт {port} не найден")

        with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.1) as ser:
            fast_addrs = fast_scan(ser)
            std_found = std_scan(ser, max_addr)
            for addr in fast_addrs:
                if addr not in std_found:
                    pdu = make_pdu(addr, 0x03, 0x00, 0x00, 0x00, 0x01)
                    ser.reset_input_buffer()
                    ser.write(pdu)
                    resp = read_resp(ser, timeout=0.08)
                    if len(resp) >= 7 and resp[0] == addr and resp[1] == 0x03:
                        std_found[addr] = (resp[3] << 8) | resp[4]

            devices = []
            for addr in sorted(std_found.keys()):
                dev_type, module_type, type_name = detect_type(ser, addr)
                devices.append({
                    "addr": addr,
                    "type": dev_type,
                    "module_type": module_type,
                    "type_name": type_name,
                    "name": f"Устройство {addr}",
                })

        print(json.dumps({"ok": True, "devices": devices, "port": port, "baudrate": baud}))
    except FileNotFoundError as e:
        print(json.dumps({"ok": False, "error": str(e), "devices": []}))
    except serial.SerialException as e:
        print(json.dumps({"ok": False, "error": f"Ошибка порта: {e}", "devices": []}))
    except PermissionError as e:
        print(json.dumps({"ok": False, "error": f"Нет доступа к порту: {e}", "devices": []}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e), "devices": []}))


if __name__ == "__main__":
    main()

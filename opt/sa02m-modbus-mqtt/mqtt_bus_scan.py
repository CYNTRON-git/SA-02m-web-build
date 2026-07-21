#!/usr/bin/env python3
"""Modbus bus scanner for MQTT device discovery (runs as root via sudo from CGI)."""
import json
import struct
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    print(json.dumps({"ok": False, "error": "pyserial not installed", "devices": []}))
    sys.exit(0)

FMB_ADDR = 0xFD

# Count-first display names — keep in sync with modbus_mqtt_bridge.MR02M_TYPE_NAMES
# and MR-02m Core/Inc/main.h product signatures (not letter-first C enum tags).
MR02M_MODULE_TYPES = {
    1: "6DO8DI", 2: "16DO", 3: "12AO", 4: "6DO", 5: "14DI",
    6: "6AI6AO", 7: "12AI", 8: "4DO6DI", 9: "TENZO2", 10: "10DIcon",
    11: "6DO5DI2AO", 12: "6AI2AO", 15: "4TO6DI",
}

# Подписи как MR02M_TYPE_LABELS_RU в mqtt.js
MR02M_TYPE_LABELS_RU = {
    1: "6ДО 8ДИ", 2: "16ДО", 3: "12АО", 4: "6ДО", 5: "14ДИ",
    6: "6АИ 6АО", 7: "12АИ", 8: "4ДО 6ДИ", 9: "Тензо 2", 10: "10ДИ",
    11: "6ДО 5ДИ 2АО", 12: "6АИ 2АО", 15: "4ТО 6ДИ",
}

_SIG_LATIN = str.maketrans("АВОИДТ", "AVOIDT")


def _latinize_sig(s: str) -> str:
    return (s or "").upper().translate(_SIG_LATIN).replace(" ", "").replace("_", "").replace("-", "")


def _sig_implies_module_type(signature: str, module_type: int) -> bool:
    if module_type not in MR02M_MODULE_TYPES:
        return False
    sig = (signature or "").strip()
    if not sig:
        return True
    sk = _latinize_sig(sig)
    label_k = _latinize_sig(MR02M_TYPE_LABELS_RU.get(module_type, ""))
    code_k = _latinize_sig(MR02M_MODULE_TYPES.get(module_type, ""))
    if sk == label_k or sk == code_k:
        return True
    aliases = {
        6: ("AO6AI6", "6AO6AI", "6AI6AO", "AI6AO6"),
        12: ("AI6AO2", "6AI2AO", "6AO2AI", "AO6AI2"),
        1: ("DO6DI8", "6DO8DI", "8DI6DO"),
        8: ("DO4DI6", "4DO6DI", "6DI4DO"),
        11: ("6DO5DI2AO",),
        15: ("4TO6DI", "TO4DI6"),
        10: ("10DICON", "10DI"),
        2: ("DO16", "16DO"),
        3: ("AO12", "12AO"),
        7: ("AI12", "12AI"),
        5: ("DI14", "14DI"),
        4: ("DO6", "6DO"),
        9: ("TENZO2",),
    }
    return any(tok in sk for tok in aliases.get(module_type, ()))

_TO4DI6_AO_EEPROM_MAGIC = 0xA8


def crc16(data):
    crc = 0xFFFF
    for b in (data if isinstance(data, (bytes, bytearray)) else bytes(data)):
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def make_pdu(addr, func, reg, count):
    data = bytes([addr, func, reg >> 8, reg & 0xFF, count >> 8, count & 0xFF])
    c = crc16(data)
    return data + bytes([c & 0xFF, c >> 8])


def make_fmb5(sub):
    data = bytes([FMB_ADDR, 0x46, sub])
    c = crc16(data)
    return data + bytes([c & 0xFF, c >> 8])


def valid_crc(pkt):
    if len(pkt) < 4:
        return False
    c = crc16(pkt[:-2])
    return (c & 0xFF) == pkt[-2] and (c >> 8) == pkt[-1]


def read_resp(ser, timeout=0.08, max_len=64):
    ser.timeout = timeout
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(max_len - len(buf))
        if chunk:
            buf += chunk
        elif buf:
            break
        else:
            time.sleep(0.002)
    return buf


def read_fmb_frame(ser, timeout=0.2):
    """10-byte answer_scan frame, tolerating leading 0xFF arbitration
    padding (Wiren FMB devices, incl. DTV, prefix answers with 0xFF)."""
    buf = b""
    ser.timeout = 0.05
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(32)
        if chunk:
            buf += chunk
            frame = buf.lstrip(b"\xff")
            if len(frame) >= 10:
                return frame[:10]
        elif buf.lstrip(b"\xff"):
            break
        else:
            time.sleep(0.002)
    frame = buf.lstrip(b"\xff")
    return frame[:10] if len(frame) >= 10 else b""


def fast_scan_fmb(ser):
    """Fast Modbus: begin_scan → answer_scan (0x03) × N → end_scan."""
    found = []
    try:
        ser.reset_input_buffer()
        ser.write(make_fmb5(0x01))
        time.sleep(0.02)
        for _ in range(32):
            resp = read_fmb_frame(ser, timeout=0.25)
            if len(resp) < 10:
                break
            if resp[0] != FMB_ADDR or resp[1] != 0x46 or resp[2] != 0x03:
                break
            if not valid_crc(resp[:10]):
                break
            addr = resp[7]
            if 1 <= addr <= 247:
                serial = struct.unpack(">I", resp[3:7])[0]
                found.append({"addr": addr, "serial": serial})
            ser.reset_input_buffer()
            ser.write(make_fmb5(0x02))
            time.sleep(0.01)
        ser.write(make_fmb5(0x04))
        time.sleep(0.05)
    except Exception:
        pass
    return found


def std_scan(ser, max_a):
    """Классический опрос FC03 reg0 по адресам 1..max_a."""
    found = {}
    for addr in range(1, max_a + 1):
        pdu = make_pdu(addr, 0x03, 0, 1)
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


def read_holding(ser, addr, reg, count=1, timeout=0.08):
    pdu = make_pdu(addr, 0x03, int(reg), int(count))
    ser.reset_input_buffer()
    ser.write(pdu)
    need = 5 + count * 2
    resp = read_resp(ser, timeout=timeout, max_len=need + 8)
    if len(resp) < need or resp[0] != addr or resp[1] != 0x03:
        return None
    if not valid_crc(resp[:need]):
        return None
    bc = resp[2]
    if bc != count * 2:
        return None
    return [(resp[3 + i * 2] << 8) | resp[4 + i * 2] for i in range(count)]


def read_input(ser, addr, reg, count=1, timeout=0.08):
    pdu = make_pdu(addr, 0x04, int(reg), int(count))
    ser.reset_input_buffer()
    ser.write(pdu)
    need = 5 + count * 2
    resp = read_resp(ser, timeout=timeout, max_len=need + 8)
    if len(resp) < need or resp[0] != addr or resp[1] != 0x04:
        return None
    if not valid_crc(resp[:need]):
        return None
    bc = resp[2]
    if bc != count * 2:
        return None
    return [(resp[3 + i * 2] << 8) | resp[4 + i * 2] for i in range(count)]


def decode_signature(regs):
    """Holding 290..301 — ASCII сигнатура (как sa02m-flasher modbus_io)."""
    if not regs or len(regs) < 12:
        return ""
    raw = bytes((r & 0xFF) for r in regs[:12])
    if len(raw) >= 2 and raw[0] == _TO4DI6_AO_EEPROM_MAGIC and raw[1] in (1, 2):
        return "4TO6DI"
    disp = "".join(chr(b) if 32 <= b <= 126 else "." for b in raw).rstrip(". ")
    if len(disp) > 12:
        disp = disp[:12]
    if disp and any(c.isalnum() for c in disp):
        return disp
    return ""


def detect_type(ser, addr, *, read_signature: bool = False):
    """Тип устройства для скана. reg-фингерпринты, затем фолбэк по
    EEPROM-сигнатуре (reg 290); read_signature=True добавляет чтение
    сигнатуры и для опознанных МР-02м."""
    r1 = read_holding(ser, addr, 1, timeout=0.05)
    if r1 is not None and len(r1) >= 1:
        if r1[0] == 0xCE02:
            return "ce02m3", 0, "СЭ-02м-3", ""
        if r1[0] == 0xD712:
            return "dtv", 0, "ДТВ-RS-485", ""

    inp = read_input(ser, addr, 0, 1, timeout=0.05)
    if inp and inp[0] in MR02M_MODULE_TYPES:
        mt = inp[0]
        signature = ""
        if read_signature:
            sig_regs = read_holding(ser, addr, 290, 12, timeout=0.07)
            if sig_regs:
                signature = decode_signature(sig_regs)
        return "mr02m", mt, MR02M_MODULE_TYPES[mt], signature

    # Reg-based fingerprints failed — fall back to the EEPROM signature
    # (reg 290), like hardpy resolve_product: DTV firmwares with Input0=0
    # identify only by "Sens." (holding 1 mirrors sensor data there).
    signature = ""
    sig_regs = read_holding(ser, addr, 290, 12, timeout=0.1)
    if sig_regs:
        signature = decode_signature(sig_regs)
    sk = _latinize_sig(signature).rstrip(".")
    if sk.startswith("SENS") or "DTV" in sk or "RTU" in sk:
        return "dtv", 0, "ДТВ-RS-485", signature
    if sk.startswith("CE02") or sk.startswith("SE02"):
        return "ce02m3", 0, "СЭ-02м-3", signature
    return "unknown", 0, "unknown", signature


def scan_short_name(dev_type, module_type, _type_name, addr, signature=""):
    """Короткое имя для поля в UI; суффикс (COMx addr=n) добавляет веб при сохранении."""
    if dev_type == "mr02m" and module_type in MR02M_MODULE_TYPES:
        sig = (signature or "").strip()
        if sig and not _sig_implies_module_type(sig, module_type):
            return sig
        return f"МР-02м {MR02M_TYPE_LABELS_RU[module_type]}"
    if dev_type == "dtv":
        return "ДТВ-RS-485"
    if dev_type == "ce02m3":
        return "СЭ-02м-3"
    if signature and signature not in ("unknown", ""):
        return signature
    return f"Устройство {addr}"


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

        with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                           timeout=0.1) as ser:
            time.sleep(0.05)
            fast_list = fast_scan_fmb(ser)
            fast_addrs = {d["addr"] for d in fast_list}

            if fast_addrs:
                scan_method = "fast"
                addrs = sorted(fast_addrs)
            else:
                scan_method = "standard"
                std_found = std_scan(ser, max_addr)
                addrs = sorted(std_found.keys())

            devices = []
            for addr in addrs:
                dev_type, module_type, type_name, signature = detect_type(
                    ser, addr, read_signature=False)
                devices.append({
                    "addr": addr,
                    "type": dev_type,
                    "module_type": module_type,
                    "type_name": type_name,
                    "signature": signature,
                    "name": scan_short_name(
                        dev_type, module_type, type_name, addr, signature),
                })

        print(json.dumps({
            "ok": True,
            "devices": devices,
            "port": port,
            "baudrate": baud,
            "scan_method": scan_method,
        }, ensure_ascii=False))
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

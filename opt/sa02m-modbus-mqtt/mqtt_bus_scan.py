#!/usr/bin/env python3
"""Modbus bus scanner for MQTT device discovery (runs as root via sudo from CGI)."""
import json
import os
import re
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

# Count-first display names — keep in sync with bridge_mr02m_map.MR02M_TYPE_NAMES
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

# Saifuli MTDX62-MB (MTD262-MB 24 GHz, MTD062-MB 5.8 GHz): the same public map.
# Holding 7 is the slave id, holding 8 a baud from this set, holding 9 parity
# 0/1/2. Input 0 is presence (0/1), input 3 is the self-test code (0..6).
_MTD_BAUDS = frozenset((1200, 2400, 4800, 9600, 19200, 38400, 57600))
MTD_TEMPLATE = "mtdx62-mb"


def mtdx62_match(ident, addr) -> bool:
    """Holding 7..9 of an MTDX62-MB: own slave id, a listed baud, parity 0..2."""
    if not ident or len(ident) < 3:
        return False
    slave, baud, parity = int(ident[0]), int(ident[1]), int(ident[2])
    return slave == int(addr) and baud in _MTD_BAUDS and parity in (0, 1, 2)


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
LED_TYPE_CODE = 120
_LED_SIG_EXACT = frozenset(("RGBW_WS2812", "RGBWWS2812", "RGBW", "LED"))
_LED_SIG_PREFIX = ("RGBW_WS2812", "RGBWWS2812")


def signature_is_led(signature: str) -> bool:
    """True for an LED-strip EEPROM signature. One home is sa02m_led;
    this copy is the scan's fallback ONLY when that package is not deployed
    (ImportError) — the scan runs as root from a CGI on a board where
    update-www-only.sh may have refreshed the bridge without the package. A
    bug inside the shared helper propagates: masking it with the copy would
    hide a broken one home behind a silently diverging second one
    (led-shared-home sweeps this file as a consumer).
    Exact for all four aliases; prefix only for the two long names — never
    a three-letter prefix (Wiren Board ``ledGe``)."""
    led_dir = os.environ.get("SA02M_LED_DIR", "/opt/sa02m-led")
    if led_dir not in sys.path:
        sys.path.insert(0, led_dir)
    try:
        from sa02m_led.led_mb2ws import signature_looks_like_led
    except ImportError:
        n = (signature or "").strip().upper().replace(" ", "")
        if not n or n in ("—", "-", "NONE", "?"):
            return False
        if n in _LED_SIG_EXACT:
            return True
        return any(n.startswith(p) for p in _LED_SIG_PREFIX)
    return bool(signature_looks_like_led(signature or ""))


def _module_type_from_signature(signature: str):
    """MR-02m module type named by an EEPROM signature, or None.

    Exact code/label match first (``6DO`` must not resolve through the
    ``6DO8DI`` alias containment), then the alias containment the short-name
    resolver already trusts."""
    sig = (signature or "").strip()
    if not sig:
        return None
    sk = _latinize_sig(sig)
    for mt, code in MR02M_MODULE_TYPES.items():
        if sk == _latinize_sig(code) or sk == _latinize_sig(
            MR02M_TYPE_LABELS_RU.get(mt, "")
        ):
            return mt
    for mt in MR02M_MODULE_TYPES:
        if _sig_implies_module_type(sig, mt):
            return mt
    return None


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

    # Before Input reg 0. A live MTD262 reports presence 1 there, and 1 is
    # also the MR-02m type code 6DO8DI — the holding 7..9 identity plus the
    # self-test register is what keeps the sensor from being filed as a module.
    ident = read_holding(ser, addr, 7, 3, timeout=0.15)
    if not mtdx62_match(ident, addr):
        # 19200 on a long line: the first frame is often late. One retry
        # before Input reg 0, which is 1 for a person present and is also
        # the MR-02m type code 6DO8DI.
        ident = read_holding(ser, addr, 7, 3, timeout=0.25)
    if mtdx62_match(ident, addr):
        presence = read_input(ser, addr, 0, 1, timeout=0.05)
        status = read_input(ser, addr, 3, 1, timeout=0.05)
        if (presence and presence[0] in (0, 1)
                and status and 0 <= status[0] <= 6):
            return "template", 0, "MTD262-MB", "MTDX62-MB"

    inp = read_input(ser, addr, 0, 1, timeout=0.05)
    # Type 120 is the RGBW_WS2812 strip, not an MR-02m I/O module — but the
    # SIGNATURE (holding 290, the device's own EEPROM) beats Input reg 0 in
    # BOTH directions, the flasher's rule (module_profiles.scan_type_code):
    # on a shared line reg 0 can be a neighbour's answer, so a crosstalk 120
    # must not turn a module into a strip any more than a false 1..15 turns
    # a strip into a module. Reg 0 decides only when the signature names
    # neither family.
    if inp and inp[0] == LED_TYPE_CODE:
        signature = ""
        sig_regs = read_holding(ser, addr, 290, 12, timeout=0.07)
        if sig_regs:
            signature = decode_signature(sig_regs)
        if not signature_is_led(signature):
            mt = _module_type_from_signature(signature)
            if mt is not None:
                return "mr02m", mt, MR02M_MODULE_TYPES[mt], signature
        return "led", LED_TYPE_CODE, "LED", signature

    if inp and inp[0] in MR02M_MODULE_TYPES:
        mt = inp[0]
        signature = ""
        if read_signature:
            sig_regs = read_holding(ser, addr, 290, 12, timeout=0.07)
            if sig_regs:
                signature = decode_signature(sig_regs)
        if signature_is_led(signature):
            return "led", LED_TYPE_CODE, "LED", signature
        return "mr02m", mt, MR02M_MODULE_TYPES[mt], signature

    # Reg-based fingerprints failed — fall back to the EEPROM signature
    # (reg 290), like hardpy resolve_product: DTV firmwares with Input0=0
    # identify only by "Sens." (holding 1 mirrors sensor data there).
    signature = ""
    sig_regs = read_holding(ser, addr, 290, 12, timeout=0.1)
    if sig_regs:
        signature = decode_signature(sig_regs)
    if signature_is_led(signature):
        return "led", LED_TYPE_CODE, "LED", signature
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
    if dev_type == "led":
        return "LED"
    if dev_type == "template":
        return "MTD262-MB"
    if signature and signature not in ("unknown", ""):
        return signature
    return f"Устройство {addr}"


def load_params(path: Path) -> dict:
    if path.exists() and path.stat().st_size > 0:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


# SECURITY (audit B1 class): the sudoers grant is
# `/usr/bin/python3 …/mqtt_bus_scan.py *`, so www-data chooses this argv and
# this file runs as ROOT. mqtt_scan.cgi validates the params before handing
# them over, but the grant means the CGI is not the only caller — so the
# validation is repeated HERE, at the privilege boundary. Fail closed: refuse
# and print the endpoint's own error shape (exit 0, one JSON document — a
# non-zero exit would make the CGI's unprivileged fallback print a SECOND one).
_PARAMS_NAME_RE = re.compile(r"^sa02m-mqttscan\.[A-Za-z0-9]{6}$")
_PORT_RE = re.compile(r"^/dev/[A-Za-z0-9_-]+$")


def params_path_ok(path: Path) -> bool:
    """The caller's own mktemp template is the contract: /tmp/sa02m-mqttscan.XXXXXX."""
    p = str(path).replace("\\", "/")
    if not p.startswith("/tmp/"):
        return False
    base = p[len("/tmp/"):]
    if "/" in base or not _PARAMS_NAME_RE.match(base):
        return False
    return not os.path.islink(p) and os.path.isfile(p)


def refuse(msg: str) -> None:
    print(json.dumps({"ok": False, "error": msg, "devices": []}, ensure_ascii=False))
    sys.exit(0)


def _scan_row(ser, addr, *, baudrate, stopbits):
    dev_type, module_type, type_name, signature = detect_type(
        ser, addr, read_signature=False)
    row = {
        "addr": addr,
        "type": dev_type,
        "module_type": module_type,
        "type_name": type_name,
        "signature": signature,
        "name": scan_short_name(
            dev_type, module_type, type_name, addr, signature),
        "baudrate": int(baudrate),
        "stopbits": 2 if int(stopbits) == 2 else 1,
    }
    if dev_type == "template":
        row["template"] = MTD_TEMPLATE
    return row


def main() -> None:
    params_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if params_path is not None and not params_path_ok(params_path):
        refuse("refusing params path (expected a regular, non-symlink "
               "/tmp/sa02m-mqttscan.XXXXXX file)")
    try:
        params = load_params(params_path) if params_path else {}
    except (OSError, ValueError):
        refuse("unreadable scan parameters")
    port = str(params.get("port", "/dev/COM1"))
    if not _PORT_RE.match(port):
        refuse("invalid scan parameters (port)")
    try:
        baud = int(params.get("baudrate", 115200))
        max_addr = min(int(params.get("max_addr", 32)), 247)
    except (TypeError, ValueError):
        refuse("invalid scan parameters (baudrate/max_addr)")
    if not (300 <= baud <= 4000000) or not (1 <= max_addr <= 247):
        refuse("invalid scan parameters (baudrate/max_addr)")

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
                devices.append(_scan_row(
                    ser, addr, baudrate=baud, stopbits=1))
            # Fast Modbus lists only our modules. An MTD262 on the same
            # 19200 line does not answer the fast scan, and stopping there
            # would file it later as an 8N2 hit. Probe the addresses the
            # fast scan missed, still at 8N1, and keep an MTD fingerprint.
            if fast_addrs:
                known = {d["addr"] for d in devices}
                for addr in sorted(std_scan(ser, max_addr)):
                    if addr in known:
                        continue
                    row = _scan_row(ser, addr, baudrate=baud, stopbits=1)
                    if row["type"] != "template":
                        continue
                    devices.append(row)

        # The sweep above is 8N1. A sensor that answers only at 2 stop bits
        # is silent there, so a second pass reads the addresses it missed and
        # keeps one only when the MTD fingerprint matches — an 8N1 module
        # that also answers 8N2 is not re-added. The same pass then runs at
        # 9600 (factory rate) and 19200 (a sensor moved onto a CYNTRON port)
        # when that was not the baud the operator already chose.
        already = {d["addr"] for d in devices}
        devices.extend(_mtd_stop2_pass(port, baud, max_addr, already))
        for extra in (9600, 19200):
            if extra != baud:
                devices.extend(_mtd_stop2_pass(port, extra, max_addr, already))

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


def _mtd_stop2_pass(port, baud, max_addr, already):
    """Addresses silent on the 8N1 sweep that answer as an MTDX62-MB at 8N2.

    A hit is kept only when detect_type says template. `already` is updated
    so a later pass at another baud does not report the same slave twice.
    """
    found = []
    try:
        ser = serial.Serial(
            port, baud, bytesize=8, parity="N", stopbits=2, timeout=0.1)
    except Exception:
        return found
    try:
        time.sleep(0.05)
        for addr in sorted(std_scan(ser, max_addr)):
            if addr in already:
                continue
            row = _scan_row(ser, addr, baudrate=baud, stopbits=2)
            if row["type"] != "template":
                continue
            found.append(row)
            already.add(addr)
    finally:
        ser.close()
    return found


if __name__ == "__main__":
    main()

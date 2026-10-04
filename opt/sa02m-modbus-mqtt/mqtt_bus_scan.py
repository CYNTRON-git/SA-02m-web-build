#!/usr/bin/env python3
"""Modbus bus scanner for MQTT device discovery (runs as root via sudo from CGI)."""
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, OSError, ValueError):
    pass

try:
    import serial
except ImportError:
    print(json.dumps({"ok": False, "error": "pyserial not installed", "devices": []}),
          flush=True)
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


def resolve_addr_from(params, max_addr):
    """First address of this request. 1 when omitted.

    A window is ``addr_from``..``max_addr`` so the UI can walk the range in
    short requests. fcgiwrap holds the body until the process exits, and one
    full sweep would move the bar only at the end.
    """
    if not isinstance(params, dict):
        return None
    if "addr_from" not in params or params.get("addr_from") in ("", None):
        return 1
    raw = params.get("addr_from")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if not 1 <= raw <= int(max_addr):
        return None
    return raw


def standard_address_steps(via, phase, baud, max_addr):
    """Address steps that follow Fast Modbus, on one 0–100 scale.

    Gateway: the FC03 sweep, then FC04 for slaves FC03 missed. COM: the 8N1
    sweep, then 8N2 at the chosen baud, then 9600 and 19200 when that baud
    is not already the one the operator picked. ``fast`` is zero steps — the
    UI owns that first share. ``all`` counts the same steps as ``standard``.
    """
    if phase == "fast":
        return 0
    span = int(max_addr)
    if via == "gateway":
        return 2 * span
    passes = 2
    rate = int(baud)
    if rate != 9600:
        passes += 1
    if rate != 19200:
        passes += 1
    return passes * span


class _ScanProgress:
    """NDJSON ``progress`` events for the post-fast address steps.

    ``done``/``total`` cover only that remainder. The UI adds the fast share
    so the bar does not sit at 0 through the sweep or hit 100 before the
    8N2 passes finish.
    """

    def __init__(self, total, emit):
        self.total = int(total)
        self.done = 0
        self._emit = emit

    def step(self, _addr=None):
        if self.total <= 0 or self._emit is None or self.done >= self.total:
            return
        self.done += 1
        self._emit({
            "event": "progress",
            "phase": "standard",
            "done": self.done,
            "total": self.total,
        })


def std_scan(ser, max_a, *, skip=None, on_hit=None, on_step=None, addr_from=1):
    """Классический опрос FC03 reg0 по адресам addr_from..max_a.

    ``skip`` addresses are not probed (already reported). ``on_hit`` runs
    before the next address, so a caller can identify and publish that slave
    while the rest of the range is still ahead. The 60 ms read is unchanged.
    ``on_step`` runs for every address, including ones in ``skip``, after
    that address is finished.
    """
    found = {}
    blocked = skip if skip is not None else ()
    start = int(addr_from) if int(addr_from) >= 1 else 1
    for addr in range(start, max_a + 1):
        hit = False
        if addr not in blocked:
            pdu = make_pdu(addr, 0x03, 0, 1)
            try:
                ser.reset_input_buffer()
                ser.write(pdu)
                resp = read_resp(ser, timeout=0.06)
                if (len(resp) >= 7 and resp[0] == addr and resp[1] == 0x03
                        and valid_crc(resp[:7])):
                    found[addr] = (resp[3] << 8) | resp[4]
                    hit = True
            except Exception:
                hit = False
            if hit and on_hit is not None:
                on_hit(addr)
        if on_step is not None:
            on_step(addr)
    return found


def _fc_reg0_present(ser, addr, func, timeout=0.06):
    """One FC03/FC04 read of register 0. True only on a CRC-valid normal reply."""
    pdu = make_pdu(addr, func, 0, 1)
    try:
        ser.reset_input_buffer()
        ser.write(pdu)
        resp = read_resp(ser, timeout=timeout)
    except Exception:
        return False
    return (len(resp) >= 7 and resp[0] == addr and resp[1] == func
            and valid_crc(resp[:7]))


def gateway_std_scan(ser, max_a, *, skip=None, on_hit=None, on_step=None, addr_from=1):
    """Gateway presence sweep. COM `std_scan` is left as it is.

    The flasher's ordinary search probes holding register 0 (FC03). A module
    that stays silent there still names itself in Input register 0 (FC04) —
    that is how an MR-02m type code is read after the probe. On a gateway,
    a miss of FC03 gets one FC04 probe so that module is not dropped.
    ``on_step`` runs once per address in each of those two passes.
    """
    found = std_scan(
        ser, max_a, skip=skip, on_hit=on_hit, on_step=on_step, addr_from=addr_from)
    blocked = skip if skip is not None else ()
    start = int(addr_from) if int(addr_from) >= 1 else 1
    for addr in range(start, max_a + 1):
        if addr not in found and addr not in blocked:
            if _fc_reg0_present(ser, addr, 0x04):
                found[addr] = None
                if on_hit is not None:
                    on_hit(addr)
        if on_step is not None:
            on_step(addr)
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


def _read_exact(ser, n, deadline):
    """Read ``n`` bytes, waiting out a gap inside the frame until ``deadline``."""
    buf = b""
    while len(buf) < n and time.monotonic() < deadline:
        ser.timeout = min(0.05, max(0.001, deadline - time.monotonic()))
        chunk = ser.read(n - len(buf))
        if chunk:
            buf += chunk
    return buf


def _fc17_payload(ser, addr):
    """Report Slave ID payload, or empty when the slave does not answer.

    A c.pCOmini reply is about 206 bytes. The ordinary 60 ms probe cuts that
    frame, the CRC fails, and the PLC stays ``unknown``. The deadline is the
    wire time of a full frame plus the PLC's think time, and a short gap
    inside the reply does not end the read. One retry: the first frame after
    the line was just opened is sometimes lost.
    """
    if ser is None:
        return b""
    frame = bytes([addr & 0xFF, 0x11])
    frame += struct.pack("<H", crc16(frame))
    baud = int(getattr(ser, "baudrate", 0) or 9600)
    timeout = max(0.6, (251 * 11) / float(baud) + 0.2)
    for attempt in range(2):
        try:
            ser.reset_input_buffer()
            ser.write(frame)
            try:
                ser.flush()
            except Exception:
                pass
            payload = _fc17_take(ser, addr, timeout)
        except Exception:
            payload = b""
        if payload:
            return payload
        if attempt == 0:
            time.sleep(0.05)
    return b""


def _fc17_take(ser, addr, timeout):
    deadline = time.monotonic() + timeout
    head = _read_exact(ser, 3, deadline)
    if len(head) < 3 or head[0] != (addr & 0xFF) or head[1] != 0x11:
        return b""
    n = head[2]
    if n < 8 or n > 246:
        return b""
    rest = _read_exact(ser, n + 2, deadline)
    if len(rest) < n + 2:
        return b""
    body = head + rest[:n + 2]
    if not valid_crc(body):
        return b""
    return body[3:3 + n]


def _carel_parser():
    """``parse_report_slave_id`` from the shared package, or None.

    The scanner is started as root from the CGI and does not inherit the
    bridge's import path. ``/opt/sa02m-carel`` is the installed copy; the
    sibling directory is the repo checkout the unit tests run from.
    """
    try:
        from sa02m_carel.carel_ahu import FAMILY_UARIA, parse_report_slave_id
        return FAMILY_UARIA, parse_report_slave_id
    except ImportError:
        pass
    candidates = [os.environ.get("SA02M_CAREL_DIR", "/opt/sa02m-carel")]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "sa02m-carel"))
    for path in candidates:
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        from sa02m_carel.carel_ahu import FAMILY_UARIA, parse_report_slave_id
    except ImportError:
        return None
    return FAMILY_UARIA, parse_report_slave_id


def _carel_identity(ser, addr):
    """Carel c.pCO / uAria from FC17, or None. Fail closed on an unknown blob."""
    payload = _fc17_payload(ser, addr)
    if not payload:
        return None
    parsed = _carel_parser()
    if parsed is None:
        return None
    family_uaria, parse_report_slave_id = parsed
    fp = parse_report_slave_id(payload)
    if fp is None:
        return None
    app_id = fp.app_id or ""
    if fp.family == family_uaria:
        return "carel", 0, "Carel uAria", app_id
    return "carel", 0, "Carel c.pCOmini", app_id


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
    if dev_type == "carel":
        return _type_name or "Carel"
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
# The repeated port check is only the board UARTs. A wider /dev path must not
# reach a root open.
_PARAMS_NAME_RE = re.compile(r"^sa02m-mqttscan\.[A-Za-z0-9]{6}$")
_PORT_RE = re.compile(r"^/dev/COM[1-5]$")

# Same dotted-decimal rule as bridge_bus.canonical_host. mqtt_scan.cgi repeats
# it before sudo — a hostname or a leading-zero octet must not reach this
# process, and this process refuses them again because sudoers lets any caller
# hand it a params file.
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
_IPV4_RE = re.compile(rf"^{_OCTET}(?:\.{_OCTET}){{3}}$")
_FORBIDDEN_NETS = tuple(ipaddress.IPv4Network(n) for n in (
    "127.0.0.0/8", "0.0.0.0/32", "224.0.0.0/4", "240.0.0.0/4"))
# A transparent gateway adds a hop the 60 ms COM sweep does not have.
_GATEWAY_READ_FLOOR_S = 0.2


def params_path_ok(path: Path) -> bool:
    """The caller's own mktemp template is the contract: /tmp/sa02m-mqttscan.XXXXXX."""
    p = str(path).replace("\\", "/")
    if not p.startswith("/tmp/"):
        return False
    base = p[len("/tmp/"):]
    if "/" in base or not _PARAMS_NAME_RE.match(base):
        return False
    return not os.path.islink(p) and os.path.isfile(p)


def _write_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def refuse(msg: str) -> None:
    _write_json({"ok": False, "error": msg, "devices": []})
    sys.exit(0)


_PHASES = frozenset(("fast", "standard"))


def resolve_phase(params: dict):
    """``fast``, ``standard``, or ``all`` when the client omits the field.

    Any other value is refused. ``all`` is not a client token: a request that
    sends it is rejected, same as an unknown phase.
    """
    if not isinstance(params, dict):
        return None
    if "phase" not in params or params.get("phase") in ("", None):
        return "all"
    phase = params.get("phase")
    if not isinstance(phase, str):
        return None
    phase = phase.strip().lower()
    if phase in _PHASES:
        return phase
    return None


def resolve_known_addrs(params: dict):
    """Addresses a previous phase already published. Empty when omitted.

    Fail closed: not a list, a bool, a non-int, or an address outside 1..247.
    """
    if not isinstance(params, dict):
        return None
    if "known_addrs" not in params or params.get("known_addrs") in ("", None):
        return set()
    raw = params.get("known_addrs")
    if not isinstance(raw, list) or len(raw) > 247:
        return None
    out = set()
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if not 1 <= item <= 247:
            return None
        out.add(item)
    return out


def gateway_endpoint(params: dict):
    """(host, tcp_port) for a transparent-gateway scan, or None.

    IPv4 only. Loopback, 0.0.0.0, multicast and the class-E range are refused:
    this process runs as root and the host is chosen by the browser.
    """
    if not isinstance(params, dict):
        return None
    host = str(params.get("host") or "").strip()
    if not _IPV4_RE.fullmatch(host):
        return None
    ip = ipaddress.IPv4Address(host)
    if any(ip in net for net in _FORBIDDEN_NETS):
        return None
    try:
        tcp_port = int(params.get("tcp_port"))
    except (TypeError, ValueError):
        return None
    if not 1 <= tcp_port <= 65535:
        return None
    return host, tcp_port


class RtuTcpLink:
    """Raw Modbus RTU over one TCP socket (gateway transparent mode).

    The methods the sweep uses are the pyserial ones: ``timeout``, ``read``,
    ``write``, ``reset_input_buffer``. Baud and stop bits stay on the gateway.
    """

    def __init__(self, host: str, tcp_port: int, read_floor: float = _GATEWAY_READ_FLOOR_S):
        self._floor = float(read_floor)
        self._timeout = self._floor
        self._pending = bytearray()
        self._sock = socket.create_connection((host, int(tcp_port)), timeout=3.0)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(self._timeout)

    @property
    def timeout(self) -> float:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._timeout = max(float(value), self._floor)
        self._sock.settimeout(self._timeout)

    def reset_input_buffer(self) -> None:
        self._pending.clear()
        self._sock.setblocking(False)
        try:
            for _ in range(64):
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
        except OSError:
            pass
        finally:
            self._sock.settimeout(self._timeout)

    def write(self, data: bytes) -> None:
        self._sock.settimeout(max(self._timeout, 1.0))
        self._sock.sendall(data)
        self._sock.settimeout(self._timeout)

    def read(self, n: int) -> bytes:
        if n <= 0:
            return b""
        if self._pending:
            take = bytes(self._pending[:n])
            del self._pending[:n]
            return take
        self._sock.settimeout(self._timeout)
        try:
            chunk = self._sock.recv(n)
        except (TimeoutError, socket.timeout):
            return b""
        except OSError as exc:
            if getattr(exc, "errno", None) in (11, 10035, 10060):
                return b""
            raise
        return chunk or b""

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


def _identify_fast_hit(ser, addr):
    """Module type of a slave that already answered Fast Modbus.

    Input register 0 is the MR-02m code (15 → 4TO6DI). An MTD sensor does
    not answer the fast scan, so the holding 7..9 probe and its retry are
    not needed here. None sends the caller through the full ``detect_type``.
    """
    inp = read_input(ser, addr, 0, 1, timeout=0.05)
    if not inp or inp[0] not in MR02M_MODULE_TYPES:
        return None
    mt = inp[0]
    return "mr02m", mt, MR02M_MODULE_TYPES[mt], ""


def _scan_row(ser, addr, *, baudrate, stopbits, fast_hit=False, probe_carel=False):
    identified = _identify_fast_hit(ser, addr) if fast_hit else None
    if identified is None:
        dev_type, module_type, type_name, signature = detect_type(
            ser, addr, read_signature=False)
        # Carel answers FC03 and its Input reg 0 can look like an MR-02m
        # type code. FC17 is the identity; a known signature is left alone.
        if (probe_carel and dev_type in ("unknown", "mr02m")
                and not (signature or "").strip()):
            carel = _carel_identity(ser, addr)
            if carel is not None:
                dev_type, module_type, type_name, signature = carel
    else:
        dev_type, module_type, type_name, signature = identified
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
    if dev_type == "carel":
        row["family"] = "uaria" if "uAria" in type_name else "crst"
    return row


def _remember_row(ser, addr, *, baudrate, stopbits, seen, devices, emit,
                  phase_name, only_template=False, fast_hit=False,
                  probe_carel=False):
    """Identify ``addr`` once and publish the same row the UI table shows."""
    if addr in seen:
        return False
    row = _scan_row(
        ser, addr, baudrate=baudrate, stopbits=stopbits, fast_hit=fast_hit,
        probe_carel=probe_carel)
    if only_template and row.get("type") != "template":
        return False
    seen.add(addr)
    devices.append(row)
    if emit is not None:
        emit({"event": "device", "device": row, "phase": phase_name})
    return True


def _fast_then_standard(ser, max_addr, *, phase, known, baudrate, stopbits,
                        emit, gateway, on_step=None, addr_from=1):
    """Fast Modbus first, then the address sweep for slaves it did not list.

    A hit is published as soon as ``detect_type`` returns, before the next
    address. ``known`` addresses are not probed again and not emitted twice.
    """
    seen = set(known)
    devices = []
    if phase in ("all", "fast"):
        announced = set()
        for item in fast_scan_fmb(ser):
            try:
                addr = int(item.get("addr", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if addr in seen or addr in announced or not 1 <= addr <= 247:
                continue
            announced.add(addr)
            _remember_row(
                ser, addr, baudrate=baudrate, stopbits=stopbits, seen=seen,
                devices=devices, emit=emit, phase_name="fast", fast_hit=True)
    if phase in ("all", "standard"):
        def on_hit(addr):
            _remember_row(
                ser, addr, baudrate=baudrate, stopbits=stopbits, seen=seen,
                devices=devices, emit=emit, phase_name="standard",
                probe_carel=not gateway)

        if gateway:
            gateway_std_scan(
                ser, max_addr, skip=seen, on_hit=on_hit, on_step=on_step,
                addr_from=addr_from)
        else:
            std_scan(
                ser, max_addr, skip=seen, on_hit=on_hit, on_step=on_step,
                addr_from=addr_from)
    if phase == "fast":
        method = "fast"
    elif phase == "standard":
        method = "standard"
    else:
        method = "fast+standard"
    return devices, method, seen


def _scan_gateway(host: str, tcp_port: int, max_addr: int, *,
                  read_floor: float = _GATEWAY_READ_FLOOR_S,
                  phase: str = "all", known=(), emit=None, on_step=None,
                  addr_from: int = 1) -> dict:
    """Modbus RTU sweep through a transparent TCP port. No 8N2 retry: the
    gateway owns the UART framing, and a second pass would only stall.

    ``phase`` is ``all`` (fast, then the address sweep), ``fast``, or
    ``standard``. ``emit`` receives each device event as soon as that slave
    is identified. ``known`` addresses are left to the caller that already
    published them.
    """
    try:
        link = RtuTcpLink(host, tcp_port, read_floor=read_floor)
    except OSError:
        return {"ok": False, "error": "Шлюз недоступен", "devices": []}
    try:
        time.sleep(0.05)
        devices, method, _seen = _fast_then_standard(
            link, max_addr, phase=phase, known=known, baudrate=0, stopbits=1,
            emit=emit, gateway=True, on_step=on_step, addr_from=addr_from)
        return {
            "ok": True,
            "devices": devices,
            "via": "gateway",
            "host": host,
            "tcp_port": int(tcp_port),
            "scan_method": method,
            "phase": phase,
        }
    except Exception:
        return {"ok": False, "error": "Ошибка опроса шлюза", "devices": []}
    finally:
        link.close()


def _bounded_max_addr(params: dict):
    try:
        max_addr = min(int(params.get("max_addr", 32)), 247)
    except (TypeError, ValueError):
        return None
    if not 1 <= max_addr <= 247:
        return None
    return max_addr


class PollLeaseError(Exception):
    """The bridge is polling and this scan could not pause that line."""


LEASE_SOCK = os.environ.get(
    "SA02M_MQTT_SCAN_LEASE_SOCK", "/run/sa02m-modbus-mqtt/scan-lease.sock")


def _lease_trace(msg: str) -> None:
    path = os.environ.get(
        "SA02M_MQTT_SCAN_LEASE_LOG", "/run/sa02m-modbus-mqtt/scan-lease.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


def _bridge_unit_active() -> bool:
    """True when sa02m-modbus-mqtt is running. A missing systemctl is not."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "sa02m-modbus-mqtt.service"],
            timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _lease_readline(sock: socket.socket, limit: int = 1024) -> str:
    buf = b""
    while b"\n" not in buf and len(buf) < limit:
        chunk = sock.recv(limit - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0].decode("utf-8", "replace")


def _uart_holder_pids(port: str):
    """PIDs fuser reports for this UART. Empty when fuser is missing or quiet."""
    try:
        result = subprocess.run(
            ["fuser", port],
            capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    blob = ((result.stdout or "") + " " + (result.stderr or "")).replace(":", " ")
    return [int(tok) for tok in blob.split() if tok.isdigit()]


def _wait_uart_free(port: str, timeout_s: float = 2.0) -> bool:
    """True once this UART has no holder. The scan opens it only after that."""
    deadline = time.monotonic() + timeout_s
    while True:
        pids = _uart_holder_pids(port)
        if not pids:
            return True
        if time.monotonic() >= deadline:
            _lease_trace(
                "still held %s pids=%s" % (port, ",".join(str(p) for p in pids)))
            return False
        time.sleep(0.05)


@contextmanager
def poll_lease(spec: dict):
    """Pause the bridge poll of this line before the caller opens it.

    The pause ends when this context exits (success, empty scan, error).
    A missing lease socket while sa02m-modbus-mqtt is active means this
    process cannot park the line, so the scan does not open it. When the
    bridge is not running there is nothing to pause and the scan proceeds.
    When the socket answers, only the requested line is paused. A socket
    that refuses the lease still fails closed.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    idle = False
    try:
        sock.settimeout(3.0)
        try:
            sock.connect(LEASE_SOCK)
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            if _bridge_unit_active():
                _lease_trace("no lease socket (bridge active), scan refused")
                raise PollLeaseError("Опрос линии не остановлен")
            _lease_trace("no lease socket (bridge down), scan continues")
            idle = True
        if idle:
            yield {"ok": True, "held": False}
            return
        sock.sendall((json.dumps(spec) + "\n").encode("utf-8"))
        try:
            resp = json.loads(_lease_readline(sock) or "null")
        except json.JSONDecodeError:
            resp = None
        if not isinstance(resp, dict) or not resp.get("ok"):
            err = resp.get("error") if isinstance(resp, dict) else None
            raise PollLeaseError(err or "Опрос линии не остановлен")
        sock.settimeout(None)
        yield resp
    except PollLeaseError:
        raise
    except OSError as exc:
        raise PollLeaseError("Опрос линии не остановлен") from exc
    finally:
        if not idle:
            try:
                sock.close()
            except OSError:
                pass


def main() -> None:
    params_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if params_path is not None and not params_path_ok(params_path):
        refuse("refusing params path (expected a regular, non-symlink "
               "/tmp/sa02m-mqttscan.XXXXXX file)")
    try:
        params = load_params(params_path) if params_path else {}
    except (OSError, ValueError):
        refuse("unreadable scan parameters")
    phase = resolve_phase(params)
    known = resolve_known_addrs(params)
    if phase is None or known is None:
        refuse("invalid scan parameters (phase)")
    via = str(params.get("via") or "com").strip().lower()
    if via == "gateway":
        ep = gateway_endpoint(params)
        max_addr = _bounded_max_addr(params)
        if ep is None or max_addr is None:
            refuse("invalid scan parameters (host/tcp_port)")
        if "baudrate" in params and params.get("baudrate") not in ("", None):
            try:
                baud_chk = int(params.get("baudrate"))
            except (TypeError, ValueError):
                refuse("invalid scan parameters (baudrate/max_addr)")
            if not (300 <= baud_chk <= 4000000):
                refuse("invalid scan parameters (baudrate/max_addr)")
        host, tcp_port = ep
        addr_from = resolve_addr_from(params, max_addr)
        if addr_from is None:
            refuse("invalid scan parameters (addr_from)")
        try:
            _lease = poll_lease({
                "via": "gateway", "host": host, "tcp_port": int(tcp_port)})
            _lease.__enter__()
        except PollLeaseError as exc:
            _write_json({"ok": False, "error": str(exc), "devices": []})
            return
        try:
            _lease_trace("scan %s:%d" % (host, int(tcp_port)))
            progress = _ScanProgress(
                standard_address_steps(
                    "gateway", phase, 0, max_addr - addr_from + 1),
                _write_json)
            result = _scan_gateway(
                host, tcp_port, max_addr, phase=phase, known=known,
                emit=_write_json, on_step=progress.step, addr_from=addr_from)
            if not result.get("ok"):
                _write_json({
                    "ok": False,
                    "error": result.get("error") or "Ошибка опроса шлюза",
                    "devices": [],
                })
                return
            _write_json({
                "event": "done",
                "ok": True,
                "scan_method": result.get("scan_method"),
                "phase": phase,
                "via": "gateway",
                "host": host,
                "tcp_port": int(tcp_port),
                "devices": result.get("devices") or [],
            })
        finally:
            _lease.__exit__(None, None, None)
        return
    if via != "com":
        refuse("invalid scan parameters (via)")
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
    addr_from = resolve_addr_from(params, max_addr)
    if addr_from is None:
        refuse("invalid scan parameters (addr_from)")

    try:
        _lease = poll_lease({"via": "com", "port": port})
        _lease.__enter__()
    except PollLeaseError as exc:
        _write_json({"ok": False, "error": str(exc), "devices": []})
        return
    try:
        try:
            if not Path(port).exists():
                raise FileNotFoundError(f"Порт {port} не найден")
            if not _wait_uart_free(port):
                _write_json({
                    "ok": False,
                    "error": "Опрос линии не остановлен",
                    "devices": [],
                })
                return

            _lease_trace("scan %s" % port)
            progress = _ScanProgress(
                standard_address_steps(
                    "com", phase, baud, max_addr - addr_from + 1),
                _write_json)
            with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                               timeout=0.1) as ser:
                time.sleep(0.05)
                devices, scan_method, seen = _fast_then_standard(
                    ser, max_addr, phase=phase, known=known, baudrate=baud,
                    stopbits=1, emit=_write_json, gateway=False,
                    on_step=progress.step, addr_from=addr_from)

            # The sweep above is 8N1. A sensor that answers only at 2 stop bits
            # is silent there, so a second pass reads the addresses it missed and
            # keeps one only when the MTD fingerprint matches — an 8N1 module
            # that also answers 8N2 is not re-added. The same pass then runs at
            # 9600 (factory rate) and 19200 (a sensor moved onto a CYNTRON port)
            # when that was not the baud the operator already chose. Each new
            # template is published before the next address. Fast-only skips it.
            # Each pass steps the same progress counter, so the bar does not
            # reach 100 until the last baud has walked its range.
            if phase != "fast":
                devices.extend(_mtd_stop2_pass(
                    port, baud, max_addr, seen, emit=_write_json,
                    on_step=progress.step, addr_from=addr_from))
                for extra in (9600, 19200):
                    if extra != baud:
                        devices.extend(_mtd_stop2_pass(
                            port, extra, max_addr, seen, emit=_write_json,
                            on_step=progress.step, addr_from=addr_from))

            _write_json({
                "event": "done",
                "ok": True,
                "devices": devices,
                "port": port,
                "baudrate": baud,
                "scan_method": scan_method,
                "phase": phase,
            })
        except FileNotFoundError as e:
            _write_json({"ok": False, "error": str(e), "devices": []})
        except serial.SerialException as e:
            _write_json({"ok": False, "error": f"Ошибка порта: {e}", "devices": []})
        except PermissionError as e:
            _write_json({"ok": False, "error": f"Нет доступа к порту: {e}", "devices": []})
        except Exception as e:
            _write_json({"ok": False, "error": str(e), "devices": []})
    finally:
        _lease.__exit__(None, None, None)


def _mtd_stop2_pass(port, baud, max_addr, already, emit=None, on_step=None,
                    addr_from=1):
    """Addresses silent on the 8N1 sweep that answer as an MTDX62-MB at 8N2.

    A hit is kept only when detect_type says template. `already` is updated
    so a later pass at another baud does not report the same slave twice.
    A kept row is emitted before the next address.
    """
    found = []
    try:
        ser = serial.Serial(
            port, baud, bytesize=8, parity="N", stopbits=2, timeout=0.1)
    except Exception:
        # The pass did not run. Count its addresses so the bar does not
        # sit short of 100 until the final line.
        if on_step is not None:
            start = int(addr_from) if int(addr_from) >= 1 else 1
            for addr in range(start, max_addr + 1):
                on_step(addr)
        return found
    try:
        time.sleep(0.05)

        def on_hit(addr):
            if addr in already:
                return
            row = _scan_row(ser, addr, baudrate=baud, stopbits=2)
            if row["type"] != "template":
                return
            found.append(row)
            already.add(addr)
            if emit is not None:
                emit({"event": "device", "device": row, "phase": "standard"})

        std_scan(ser, max_addr, skip=already, on_hit=on_hit, on_step=on_step,
                 addr_from=addr_from)
    finally:
        ser.close()
    return found


if __name__ == "__main__":
    main()

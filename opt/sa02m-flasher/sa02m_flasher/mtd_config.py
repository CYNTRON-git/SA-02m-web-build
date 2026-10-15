# -*- coding: utf-8 -*-
"""MTDx62-MB presence sensor — settings window backend («Устройства RS-485»).

The window reads and writes the sensor through the same leased Modbus session
as every other device_config route (service.py `_run_device_config_modbus`);
device_config.py only dispatches here.

Where the facts live:
  * holding 0..6 (the tuning settings) — name, scale, units, writability and
    the write RANGE come from the bridge's template
    opt/sa02m-modbus-mqtt/templates/config-mtdx62-mb.json (its `min`/`max`,
    docs/contracts/template-device.md §3), read at a fixed path; nothing from a
    request reaches that path;
  * the registers only this window touches — holding 7..13, input 4..7 and the
    input 3 status texts — are below. Source: the maker's Modbus manual
    (table 23 input, table 24 holding) checked against two read-only dumps of a
    real unit (2026-09-29; 1.0.6.69 docs/contracts/mtd-presence.md §1, §3, §10);
  * the recognition rule is mtd_link.

What it guarantees:
  * the daemon is the AUTHORITY on a write: only a writable holding channel of
    the shipped template (or holding 11), only inside its range, never holding
    7..10 whatever the template says; a missing template refuses every write;
  * a settings write is one FC06 and a READ-BACK; the reply says what the
    device holds, not what was asked;
  * a link change (holding 7..9) writes nothing while anything answers on a
    line it passes through, moves parity only TO none (the bridge opens every
    RS-485 handle 8N1, template-device.md §8), and after every write locates
    the sensor by its whole link triple before the next one.
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import mtd_link
from .modbus_io import parse_regs_be_u16, read_holding, read_input_regs, write_single

log = logging.getLogger(__name__)

MTD_KIND = "mtd"
# The scan row's signature for a recognised sensor (scanner._apply_mtd_fingerprint).
MTD_MODEL = "MTDx62-MB"

TEMPLATE_DIR_ENV = "SA02M_WB_TEMPLATES_DIR"
TEMPLATE_DIR_DEFAULT = "/opt/sa02m-modbus-mqtt/templates"
# The bridge's own seam name for this sensor (template-device.md §1).
TEMPLATE_FILE = "config-mtdx62-mb.json"

# ── input registers (FC04) ────────────────────────────────────────────────────
IR_PRESENCE = 0          # 0 / 1
IR_ILLUMINANCE = 1       # x0.1 lux
IR_TARGET_DISTANCE = 2   # x0.01 m
IR_DEVICE_STATUS = 3     # code, STATUS_TEXT
IR_APP_VERSION = 4       # IR4..IR5, four version bytes
IR_RADAR_VERSION = 6     # IR6..IR7, four version bytes
IR_COUNT = 8             # IR0..IR7, the snapshot's one FC04

# ── holding registers (FC03 / FC06) ───────────────────────────────────────────
HR_DEVICE_ID = 7
HR_BAUD = 8
HR_PARITY = 9
HR_ACTIVE_REPORTING = 10     # 1 = the sensor transmits unsolicited, 2 = off
HR_LIGHT_THRESHOLD = 11      # x0.1 lux, 0 = function off
HR_REPORT_INTERVAL = 12      # s, acts only while active reporting is on
HR_MAC_CODE = 13             # «MAC code», meaning not disclosed by the manual
HR_COUNT = 14                # HR0..HR13, the snapshot's one FC03

ACTIVE_REPORTING_ON = 1
# Never written by the settings path, whatever a template says: 7..9 move the
# sensor off the line (apply_network is their only writer), 10 makes it talk
# over its neighbours.
HARD_DENY_HOLDING = frozenset({HR_DEVICE_ID, HR_BAUD, HR_PARITY, HR_ACTIVE_REPORTING})
NETWORK_REGS = (HR_DEVICE_ID, HR_BAUD, HR_PARITY)

# Holding 11 is window-only (the bridge does not poll it); its range is table 24's.
LIGHT_THRESHOLD = {
    "reg": HR_LIGHT_THRESHOLD, "name": "light_threshold", "title": "Порог освещённости",
    "units": "lux", "scale": 0.1, "min": 0.0, "max": 420.0,
}

PARITY_REG_TO_CHAR = {0: "N", 1: "O", 2: "E"}

# IR3 code → meaning (maker's remarks; 1 = self-test passed, running normally).
STATUS_TEXT = {
    0: "идёт самопроверка",
    1: "норма",
    2: "самопроверка не пройдена",
    3: "прочая неисправность",
    4: "неисправен радар",
    5: "сбой связи RS-485",
    6: "неисправны радар и RS-485",
}
STATUS_UNKNOWN = "неизвестный код"

_RAW_MAX = 0xFFFF
_GRID_EPS = 1e-9


def device_is_mtd(device: Dict[str, Any]) -> bool:
    """A scan row the flasher recognised as an MTD carries the model as its
    signature (scanner._apply_mtd_fingerprint) — the Carel precedent."""
    sig = str((device or {}).get("signature") or "").strip().upper()
    return sig == MTD_MODEL.upper()


def version_text(hi_word: int, lo_word: int) -> str:
    """Two version registers → «X.Y.Z»: bytes [00 reserved, major, minor, revision]."""
    hi = int(hi_word) & 0xFFFF
    lo = int(lo_word) & 0xFFFF
    return "%d.%d.%d" % (hi & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF)


def status_text(code: Any) -> str:
    try:
        return STATUS_TEXT.get(int(code), STATUS_UNKNOWN)
    except (TypeError, ValueError):
        return STATUS_UNKNOWN


# ── template ─────────────────────────────────────────────────────────────────

_template_cache: Dict[str, Any] = {"key": None, "value": None}


def template_path() -> str:
    return os.path.join(os.environ.get(TEMPLATE_DIR_ENV) or TEMPLATE_DIR_DEFAULT, TEMPLATE_FILE)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _raw_bounds(lo: float, hi: float, scale: float) -> Optional[Tuple[int, int]]:
    """Engineering range → inclusive raw-word range on the scale grid, or None."""
    lo_raw = math.ceil(lo / scale - _GRID_EPS)
    hi_raw = math.floor(hi / scale + _GRID_EPS)
    if lo_raw > hi_raw or lo_raw < 0 or hi_raw > _RAW_MAX:
        return None
    return lo_raw, hi_raw


def _setting_from_channel(ch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if ch.get("reg_type") != "holding":
        return None
    reg = ch.get("address")
    if isinstance(reg, bool) or not isinstance(reg, int) or not 0 <= reg < HR_COUNT:
        return None                       # not in the snapshot's HR0..13 read
    if reg in HARD_DENY_HOLDING or reg == HR_LIGHT_THRESHOLD:
        return None                       # shown from this module's own rows
    scale = _number(ch.get("scale", 1))
    if scale is None or scale <= 0:
        return None
    lo, hi = _number(ch.get("min")), _number(ch.get("max"))
    fmt = str(ch.get("format") or "u16")
    bounds = _raw_bounds(lo, hi, scale) if lo is not None and hi is not None else None
    # Writable only with a usable range: a template edit that drops or breaks
    # the range turns the setting read-only, never unbounded.
    writable = ch.get("readonly") is False and fmt == "u16" and bounds is not None
    return {
        "reg": reg,
        "name": str(ch.get("name") or "hr%d" % reg),
        "title": str(ch.get("title") or ch.get("name") or ""),
        "units": str(ch.get("units") or ""),
        "scale": scale,
        "min": lo if writable else None,
        "max": hi if writable else None,
        "writable": writable,
    }


def load_template_settings() -> Optional[List[Dict[str, Any]]]:
    """Holding settings of the shipped template, or None when it is missing or
    unparsable. Cached by (path, mtime_ns)."""
    path = template_path()
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return None
    if _template_cache["key"] == key:
        return _template_cache["value"]
    value: Optional[List[Dict[str, Any]]] = None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        channels = doc["device"]["channels"]
        if isinstance(channels, list):
            value = []
            for ch in channels:
                if isinstance(ch, dict):
                    row = _setting_from_channel(ch)
                    if row is not None:
                        value.append(row)
    except (OSError, ValueError, KeyError, TypeError):
        value = None
    _template_cache["key"], _template_cache["value"] = key, value
    return value


def settings_table() -> Tuple[bool, List[Dict[str, Any]]]:
    """(template_ok, every setting the window shows: template holdings + HR11)."""
    rows = load_template_settings()
    ok = rows is not None
    out = [dict(r) for r in (rows or [])]
    light = dict(LIGHT_THRESHOLD)
    light["writable"] = ok
    out.append(light)
    if not ok:
        for r in out:
            r["writable"] = False
    out.sort(key=lambda r: r["reg"])
    return ok, out


def _writable_setting(reg: int) -> Dict[str, Any]:
    if reg in HARD_DENY_HOLDING:
        raise ValueError("Регистр %d датчика MTD из окна настроек не записывается" % reg)
    ok, rows = settings_table()
    if not ok:
        raise ValueError("Шаблон датчика не найден — запись недоступна")
    for row in rows:
        if row["reg"] == reg and row["writable"]:
            return row
    raise ValueError("Регистр %d датчика MTD из окна настроек не записывается" % reg)


def check_setting(reg: Any, raw: Any) -> Dict[str, Any]:
    """Refuse (ValueError) anything but an in-range raw word for a writable
    setting; returns the setting row. Runs before any bus traffic."""
    if isinstance(reg, bool) or not isinstance(reg, int):
        raise ValueError("Номер регистра — целое число")
    row = _writable_setting(reg)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("Значение регистра — целое число")
    bounds = _raw_bounds(row["min"], row["max"], row["scale"])
    if bounds is None or not bounds[0] <= raw <= bounds[1]:
        raise ValueError("Значение вне диапазона %s…%s" % (_fmt(row["min"]), _fmt(row["max"])))
    return row


def _fmt(value: float) -> str:
    return ("%g" % value) if value is not None else "?"


# ── bus helpers ──────────────────────────────────────────────────────────────

def _words(reader, send, slave: int, start: int, count: int,
           timeout_ms: int = 900) -> Optional[List[int]]:
    payload, err = reader(send, slave, start, count, timeout_ms)
    if err or not payload:
        return None
    words = parse_regs_be_u16(payload)
    return words if len(words) == count else None


def verify_live(send, slave: int) -> Optional[List[int]]:
    """Re-check the sensor's identity on the bus: [HR7, HR8, HR9] when holding
    7..9 and inputs 0/3 pass mtd_link's rule, else None."""
    triple = _words(read_holding, send, slave, HR_DEVICE_ID, len(NETWORK_REGS), 700)
    if not mtd_link.link_triple_match(triple, slave):
        return None
    ir = _words(read_input_regs, send, slave, IR_PRESENCE, IR_DEVICE_STATUS + 1, 700)
    if ir is None or not mtd_link.inputs_match(ir[IR_PRESENCE:IR_PRESENCE + 1],
                                               ir[IR_DEVICE_STATUS:IR_DEVICE_STATUS + 1]):
        return None
    return triple


# ── snapshot ─────────────────────────────────────────────────────────────────

def _device_line(device: Dict[str, Any]) -> Tuple[int, str, int]:
    baud = int(device.get("baudrate") or 9600)
    parity = str(device.get("parity") or "N").strip().upper()[:1] or "N"
    stop = int(device.get("stopbits") or 1)
    return baud, parity, stop


def _setting_value(raw: int, scale: float) -> str:
    if scale == 1:
        return str(raw)
    decimals = max(0, -int(math.floor(math.log10(scale) + _GRID_EPS)))
    return "%.*f" % (decimals, raw * scale)


def read_snapshot(send, slave: int, device: Dict[str, Any]) -> Dict[str, Any]:
    """ONE FC04 of IR0..7 and ONE FC03 of HR0..13 into the window's snapshot."""
    ir = _words(read_input_regs, send, slave, 0, IR_COUNT)
    hr = _words(read_holding, send, slave, 0, HR_COUNT)
    baud, parity, stop = _device_line(device)
    live: Dict[str, Any] = {}
    versions = {"app_version": "—", "radar_version": "—"}
    if ir is not None:
        status = ir[IR_DEVICE_STATUS]
        live = {
            "presence": ir[IR_PRESENCE],
            "illuminance_lux": round(ir[IR_ILLUMINANCE] * 0.1, 1),
            "target_distance_m": round(ir[IR_TARGET_DISTANCE] * 0.01, 2),
            "device_status": status,
            "device_status_text": status_text(status),
        }
        versions = {
            "app_version": version_text(ir[IR_APP_VERSION], ir[IR_APP_VERSION + 1]),
            "radar_version": version_text(ir[IR_RADAR_VERSION], ir[IR_RADAR_VERSION + 1]),
        }
    template_ok, rows = settings_table()
    settings = []
    for row in rows:
        raw = hr[row["reg"]] if hr is not None else None
        item = dict(row)
        item["step"] = row["scale"]
        item["raw"] = raw
        item["value"] = None if raw is None else _setting_value(raw, row["scale"])
        settings.append(item)
    reporting = None
    extra: Dict[str, Any] = {"report_interval_s": None, "mac_code": None}
    if hr is not None:
        raw_rep = hr[HR_ACTIVE_REPORTING]
        reporting = {"raw": raw_rep, "on": raw_rep == ACTIVE_REPORTING_ON}
        extra = {"report_interval_s": hr[HR_REPORT_INTERVAL], "mac_code": hr[HR_MAC_CODE]}
    network = {
        "address": hr[HR_DEVICE_ID] if hr is not None else slave,
        "baudrate": hr[HR_BAUD] if hr is not None else baud,
        "parity": PARITY_REG_TO_CHAR.get(hr[HR_PARITY], "?") if hr is not None else parity,
        "stopbits": stop,
        "writable": True,
        "bauds": list(mtd_link.MTD_BAUDS),
        "parities": ["N"],
    }
    return {
        "kind": MTD_KIND,
        "family": None,
        "bus_mode_supported": False,
        "snapshot_detail": "full",
        "info": {
            "address": slave,
            "serial": 0,
            "signature": MTD_MODEL,
            "model": MTD_MODEL,
            "app_version": versions["app_version"],
            "radar_version": versions["radar_version"],
            "bootloader_version": "—",
            "type_code": None,
            # The line this snapshot was actually read on.
            "line": {"baudrate": baud, "parity": parity, "stopbits": stop},
        },
        "network": network,
        "mtd": {
            "answered": ir is not None or hr is not None,
            "template_ok": template_ok,
            "live": live,
            "settings": settings,
            "active_reporting": reporting,
            "report_interval_s": extra["report_interval_s"],
            "mac_code": extra["mac_code"],
            "never_written": sorted(HARD_DENY_HOLDING),
        },
    }


# ── settings write ───────────────────────────────────────────────────────────

def write_setting(send, slave: int, reg: int, raw: int) -> Dict[str, Any]:
    """One setting: range check (authority) → FC06 → read-back of that register."""
    check_setting(reg, raw)
    log.info("mtd write: addr %d HR%d = %d", slave, reg, raw)
    err = write_single(send, slave, reg, raw, 800)
    if err:
        raise RuntimeError("Запись рег. %d: %s" % (reg, err))
    back = _words(read_holding, send, slave, reg, 1, 800)
    held = back[0] if back else None
    return {"reg": reg, "written": raw, "read_back": held, "applied": held == raw}


# ── link change (holding 7..9) ───────────────────────────────────────────────

def _validate_network(network: Dict[str, Any]) -> List[int]:
    """Requested [HR7, HR8, HR9], or ValueError — before any bus traffic."""
    try:
        address = network.get("address")
        baud = network.get("baudrate")
        if isinstance(address, bool) or isinstance(baud, bool):
            raise TypeError
        address, baud = int(address), int(baud)
    except (TypeError, ValueError):
        raise ValueError("Адрес и скорость — целые числа") from None
    if not 1 <= address <= 247:
        raise ValueError("Modbus-адрес: только 1..247")
    if baud not in mtd_link.MTD_BAUDS:
        raise ValueError("Скорость датчика: только %s"
                         % ", ".join(str(b) for b in mtd_link.MTD_BAUDS))
    parity = str(network.get("parity") or "N").strip().upper()[:1]
    if parity != "N":
        raise ValueError("Чётность: только N — мост опрашивает линию RS-485 без контроля чётности")
    return [address, baud, 0]


def _line_after(line: Dict[str, Any], reg: int, raw: int) -> Dict[str, Any]:
    out = dict(line)
    if reg == HR_DEVICE_ID:
        out["address"] = raw
    elif reg == HR_BAUD:
        out["baudrate"] = raw
    elif reg == HR_PARITY:
        out["parity"] = PARITY_REG_TO_CHAR[raw]
    return out


def _line_text(line: Dict[str, Any]) -> str:
    return "адресе %s, %s %s" % (line.get("address"), line.get("baudrate"),
                                 line.get("parity") or "N")


_PROBE_TRIES = 2       # one retry: a single lost or garbled frame decides nothing


def _read_link(open_conn, line: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[str]]:
    """One FC03 of HR7..HR9 on `line`: (payload, err)."""
    send, close = open_conn(line)
    try:
        return read_holding(send, int(line.get("address") or 1),
                            HR_DEVICE_ID, len(NETWORK_REGS), 700)
    finally:
        close()


def _link_triple(open_conn, line: Dict[str, Any]) -> Optional[List[int]]:
    """[HR7, HR8, HR9] read on `line`, or None when no attempt reads cleanly."""
    for _ in range(_PROBE_TRIES):
        payload, err = _read_link(open_conn, line)
        if not err and payload:
            words = parse_regs_be_u16(payload)
            if len(words) == len(NETWORK_REGS):
                return words
    return None


def _occupied(open_conn, line: Dict[str, Any]) -> bool:
    """Something answers on `line`: a reply, a Modbus exception or a garbled
    frame (5 bytes or more — the serial layer drops a shorter fragment as no
    answer). Only silence, twice, is a free line."""
    for _ in range(_PROBE_TRIES):
        payload, err = _read_link(open_conn, line)
        if payload is not None or err != "Таймаут":
            return True
    return False


def _write_on(open_conn, line: Dict[str, Any], reg: int, raw: int) -> Optional[str]:
    send, close = open_conn(line)
    try:
        return write_single(send, int(line.get("address") or 1), reg, raw, 900)
    finally:
        close()


def apply_network(device: Dict[str, Any], network: Dict[str, Any],
                  open_conn: Callable[[Dict[str, Any]], Tuple[Any, Callable[[], None]]],
                  *, port: str = "") -> Dict[str, Any]:
    """Change address / baud (and parity TO none) — address first, then baud,
    then parity — and after EVERY write find where the sensor answers before
    the next one.

    Measured on the real sensor (1.0.6.69 contract §1): an FC06 to HR7 / HR8
    applies at once, NO reply comes back on the old line, and the new value
    survives a power cycle. So a write's reply is never the verdict.

    1. The request is validated with zero bus traffic (address 1..247, a baud
       of the sensor's list, parity N only).
    2. The sensor is re-verified live on its current line (mtd_link's rule);
       only the registers that differ from the live HR7..9 are planned.
    3. Every line the change passes through is probed BEFORE the first write:
       anything answering there (reply, exception, garbled frame) is another
       device — refuse (RuntimeError) with zero writes, naming that line.
    4. After each write the sensor is located by its whole link triple: on the
       new line (applied at once — continue there); else on the line it had
       (written, in force only after a power cycle — `applies_after_restart`);
       else if that line still answers something else — stop with `error`
       naming where the sensor is; else nowhere — RuntimeError naming both.
       Every probe gets one retry.

    Returns {"device", "applies_after_restart", "written", "error"}; `device`
    is always the line the sensor was last found on.
    """
    want = _validate_network(network)
    old = dict(device)
    slave = int(old.get("address") or 0)
    if not 1 <= slave <= 247:
        raise ValueError("Недопустимый Modbus-адрес устройства")
    send, close = open_conn(old)
    try:
        current = verify_live(send, slave)
    finally:
        close()
    if current is None:
        raise RuntimeError("Датчик MTD на %s не отвечает или не опознан — параметры "
                           "связи не изменены" % _line_text(old))
    plan = [(reg, want[i]) for i, reg in enumerate(NETWORK_REGS) if current[i] != want[i]]
    line = old
    for reg, raw in plan:
        line = _line_after(line, reg, raw)
        if _occupied(open_conn, line):
            raise RuntimeError(
                "На %s уже отвечает другое устройство — параметры связи не "
                "изменены. Выберите свободный адрес." % _line_text(line))
    here = dict(old)            # the line the sensor answers on right now
    held = list(current)
    deferred = False
    written: List[Tuple[int, int]] = []
    for reg, raw in plan:
        log.info("mtd link: %s %s write HR%d = %d", port, _line_text(here), reg, raw)
        _write_on(open_conn, here, reg, raw)
        held[reg - HR_DEVICE_ID] = raw
        moved = _line_after(here, reg, raw)
        if not deferred and _link_triple(open_conn, moved) == held:
            here = moved
        else:
            on_here = _link_triple(open_conn, here)
            if on_here == held:
                deferred = True
            elif on_here is not None:
                log.info("mtd link: %s HR%d refused, sensor on %s", port, reg, _line_text(here))
                return {"device": here, "applies_after_restart": deferred,
                        "written": written,
                        "error": "Датчик не принял рег. %d = %d; он отвечает на %s"
                                 % (reg, raw, _line_text(here))}
            else:
                raise RuntimeError(
                    "После записи рег. %d датчик не отвечает ни на %s, ни на %s"
                    % (reg, _line_text(moved), _line_text(here)))
        log.info("mtd link: %s sensor located on %s%s", port, _line_text(here),
                 " (applies after restart)" if deferred else "")
        written.append((reg, raw))
    return {"device": here, "applies_after_restart": deferred,
            "written": written, "error": None}

# -*- coding: utf-8 -*-
"""
Снимки и запись настроек устройств для веб-окон конфигурации.

Поддерживаются:
  - линейка MP/MR-02m: сведения + сеть;
  - DTV / Sens.: живые данные, профильные настройки, сеть;
  - CE-02m-3: живые данные, ТТ/фазы, сеть.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from . import dtv_registers
from .flash_protocol import FlasherProtocol
from .modbus_io import (
    coil_bits_from_payload,
    decode_bootloader_version_registers_8,
    decode_signature_from_holding_290_payload,
    make_serial_send_rtu_persistent,
    parse_regs_be_u16,
    read_discrete_inputs,
    read_coils,
    read_holding,
    read_input_regs,
    regs_u32_lo_hi,
    uint32_from_modbus_reg_pair_be,
    write_coil,
    write_multiple,
    write_single,
)
from . import module_profiles

REG_SERIAL_LO = 270
REG_SIGNATURE = 290
REG_SIGNATURE_COUNT = 12
REG_APP_VERSION = 320
REG_APP_VERSION_COUNT = 4
REG_BOOTLOADER_VER = 330
REG_BOOTLOADER_VER_COUNT = 8

REG_NET_BAUD = 110
REG_NET_PARITY = 111
REG_NET_STOP = 112
REG_FAST_MODBUS = 122
REG_NET_ADDR = 128

CE_INPUT_START = 500
CE_INPUT_COUNT = 48
CE_CFG_START = 553
CE_CFG_COUNT = 7

DO_COIL_START = 1
INP_DO_FIRST = 1
INP_DO_CNT_BASE = 45
INP_DI_FIRST = 18
INP_AO_FIRST = 33
INP_DI_CNT_BASE = 77
INP_DI_SHORT_CNT_BASE = 695
INP_DI_LONG_CNT_BASE = 711
INP_DI_DOUBLE_CNT_BASE = 727
INP_DI_FREQ_BASE = 759
REG_RELAY_MODE = 130
REG_RELAY_OPTIONS = 131
REG_MODBUS_INACTIVITY_S = 134
REG_RESET_DO_COUNTERS = 135
REG_SAFE_DO_BASE = 600
REG_TIMER_DO_BASE = 616
REG_POWER_STAGGER = 622
REG_REDELAY_DO_FIRST = 623
REG_DI_MODE_BASE = 630
REG_DI_DEBOUNCE_BASE = 646
REG_DI_LONG_PRESS_BASE = 662
REG_DI_DOUBLE_CLICK_BASE = 678
REG_RESET_DI_COUNTERS = 694
REG_DI_FREQ_MODE_BASE = 750

# MR-02м MCU diagnostics (как в desktop module_config_window.py)
REG_MCU_OP_DAYS = 114          # Holding 114: наработка в днях (uint16)
REG_MCU_POWER_TEMP = 123       # Holding 123-124: vdd_raw (×0.01 В), tmcu_raw (×0.1 °C, int16)
INP_MCU_UPTIME_LO = 105        # Input 105-106: uptime seconds (uint32, lo-hi)
INP_MCU_DIAG_START = 65505     # Input 65505: свободная ОЗУ; 65506: используемая; 65507: стек;
                               # 65508: причина перезагрузки; 65509-65510: счётчик обновлений u32 lo-hi

PARITY_CHAR_TO_REG = {"N": 0, "O": 1, "E": 2}
PARITY_REG_TO_CHAR = {v: k for k, v in PARITY_CHAR_TO_REG.items()}
ALLOWED_BAUD_REG_CODES = (12, 24, 48, 96, 192, 384, 576, 1152)


def _info_plural_days(days: int) -> str:
    d = abs(days) % 100
    d1 = d % 10
    if 11 <= d <= 19:
        return f"{days} дней"
    if d1 == 1:
        return f"{days} день"
    if 2 <= d1 <= 4:
        return f"{days} дня"
    return f"{days} дней"


def _info_reset_reason(code: int) -> str:
    """MR-02m Input 65508: decode_reset_csr в Core/Src/i2c.c (не порядок RCC-битов STM32)."""
    reasons = {
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
    return reasons.get(int(code) & 0xFF, f"код {code & 0xFF}")


def _info_format_uptime(seconds: int) -> str:
    s = int(seconds)
    d = s // 86400
    h = (s % 86400) // 3600
    m = (s % 3600) // 60
    ss = s % 60
    if d > 0:
        return f"{d} д {h} ч {m} мин"
    if h > 0:
        return f"{h} ч {m} мин {ss} с"
    return f"{m} мин {ss} с"


def _read_mr_mcu_info(send, slave: int) -> Dict[str, Any]:
    """Чтение диагностики МК модуля MR/MP-02м для вкладки Сведения."""
    out: Dict[str, Any] = {
        "power_v": None,
        "temp_c": None,
        "uptime_s": None,
        "uptime_str": None,
        "op_days": None,
        "op_days_str": None,
        "ram_free": None,
        "ram_used": None,
        "fw_updates": None,
        "reset_reason": None,
    }
    # Holding 123-124: питание (vdd × 0.01 В) и температура (tmcu × 0.1 °C, int16)
    pt_regs = _read_regs(send, slave, REG_MCU_POWER_TEMP, 2, input_regs=False, timeout_ms=900)
    if len(pt_regs) >= 2:
        vdd_raw = int(pt_regs[0]) & 0xFFFF
        tmcu_raw = int(pt_regs[1]) & 0xFFFF
        if tmcu_raw >= 0x8000:
            tmcu_raw -= 0x10000
        out["power_v"] = round(vdd_raw / 100.0, 2)
        out["temp_c"] = round(tmcu_raw / 10.0, 1)
    # Holding 114: наработка в днях
    op_regs = _read_regs(send, slave, REG_MCU_OP_DAYS, 1, input_regs=False, timeout_ms=700)
    if len(op_regs) >= 1:
        days = int(op_regs[0]) & 0xFFFF
        out["op_days"] = days
        out["op_days_str"] = _info_plural_days(days)
    # Input 105-106: uptime seconds (uint32 lo-hi)
    up_regs = _read_regs(send, slave, INP_MCU_UPTIME_LO, 2, input_regs=True, timeout_ms=900)
    if len(up_regs) >= 2:
        up_s = (int(up_regs[1]) & 0xFFFF) << 16 | (int(up_regs[0]) & 0xFFFF)
        out["uptime_s"] = up_s
        out["uptime_str"] = _info_format_uptime(up_s)
    # Input 65505-65510: диагностика МК
    diag_regs = _read_regs(send, slave, INP_MCU_DIAG_START, 6, input_regs=True, timeout_ms=1000)
    if len(diag_regs) >= 2:
        out["ram_free"] = int(diag_regs[0]) & 0xFFFF
        out["ram_used"] = int(diag_regs[1]) & 0xFFFF
    if len(diag_regs) >= 6:
        out["reset_reason"] = _info_reset_reason(int(diag_regs[3]))
        fw_cnt = ((int(diag_regs[5]) & 0xFFFF) << 16) | (int(diag_regs[4]) & 0xFFFF)
        out["fw_updates"] = fw_cnt
    return out


def _normalize_parity(parity: Any) -> str:
    p = str(parity or "N").strip().upper()[:1]
    return p if p in PARITY_CHAR_TO_REG else "N"


def _device_slave(device: Dict[str, Any]) -> int:
    addr = int(device.get("address") or 0) & 0xFFFF
    if not 1 <= addr <= 247:
        raise ValueError("Недопустимый Modbus-адрес устройства")
    return addr


def _device_line(device: Dict[str, Any]) -> Tuple[int, str, int]:
    baud = int(device.get("baudrate") or 0)
    if baud <= 0:
        raise ValueError("Недопустимая скорость устройства")
    stop = int(device.get("stopbits") or 1)
    if stop not in (1, 2):
        stop = 1
    return baud, _normalize_parity(device.get("parity")), stop


def _open_transport(device_path: str, device: Dict[str, Any]):
    baud, parity, stop = _device_line(device)
    return make_serial_send_rtu_persistent(
        device_path,
        baud,
        parity,
        stop,
        default_timeout_ms=700,
        pre_open_delay_s=0.02,
    )


def _read_u16(send, slave: int, reg: int, timeout_ms: int = 700) -> Optional[int]:
    payload, err = read_holding(send, slave, reg, 1, timeout_ms)
    if err or not payload:
        return None
    regs = parse_regs_be_u16(payload)
    return int(regs[0]) & 0xFFFF if regs else None


def _read_regs(send, slave: int, start: int, count: int, *, input_regs: bool = False, timeout_ms: int = 900) -> List[int]:
    reader = read_input_regs if input_regs else read_holding
    payload, err = reader(send, slave, start, count, timeout_ms)
    if err or not payload:
        return []
    return parse_regs_be_u16(payload)


def _read_live_identity(send, slave: int, fallback: Dict[str, Any]) -> Dict[str, Any]:
    signature = ""
    serial = int(fallback.get("serial") or 0) & 0xFFFFFFFF
    app_version = str(fallback.get("app_version") or "—").strip() or "—"
    bl_version = str(fallback.get("bootloader_version") or "—").strip() or "—"
    type_code = None

    pl_sig, err_sig = read_holding(send, slave, REG_SIGNATURE, REG_SIGNATURE_COUNT, 900)
    if not err_sig and pl_sig:
        signature = decode_signature_from_holding_290_payload(pl_sig).strip()

    pl_ser, err_ser = read_holding(send, slave, REG_SERIAL_LO, 2, 900)
    if not err_ser and pl_ser:
        serial = uint32_from_modbus_reg_pair_be(pl_ser, 0)

    pl_ver, err_ver = read_holding(send, slave, REG_APP_VERSION, REG_APP_VERSION_COUNT, 900)
    if not err_ver and pl_ver:
        parsed = FlasherProtocol.parse_app_version_from_holding_payload(pl_ver)
        if parsed:
            app_version = parsed

    pl_bl, err_bl = read_holding(send, slave, REG_BOOTLOADER_VER, REG_BOOTLOADER_VER_COUNT, 900)
    if not err_bl and pl_bl:
        parsed_bl = decode_bootloader_version_registers_8(pl_bl)
        if parsed_bl:
            bl_version = parsed_bl

    regs0 = _read_regs(send, slave, 0, 1, input_regs=True, timeout_ms=500)
    if regs0:
        type_code = int(regs0[0]) & 0xFFFF
    elif not regs0:
        hold0 = _read_regs(send, slave, 0, 1, input_regs=False, timeout_ms=500)
        if hold0:
            type_code = int(hold0[0]) & 0xFFFF

    return {
        "signature": signature or str(fallback.get("signature") or "").strip(),
        "serial": serial,
        "app_version": app_version,
        "bootloader_version": bl_version,
        "type_code": type_code,
    }


def _kind_from_identity(signature: str, type_code: Optional[int]) -> Optional[str]:
    sig = str(signature or "").strip()
    sig_code = module_profiles.code_from_signature(sig)
    if sig_code == module_profiles.DTV:
        return "dtv"
    if sig_code == module_profiles.MP02_CE02M3 or type_code == module_profiles.MP02_CE02M3:
        return "ce"
    if module_profiles.is_mp_module_signature_for_batch_flash(sig):
        return "mr"
    return None


def _device_type_code_hint(device: Dict[str, Any]) -> Optional[int]:
    for key in ("type_code", "module_type"):
        raw = device.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw) & 0xFFFF
        except (TypeError, ValueError):
            continue
    return None


def _resolve_kind(identity: Dict[str, Any], device: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Определить kind; при сбое live-Modbus — сигнатура/тип из scan-записи устройства."""
    kind = _kind_from_identity(identity["signature"], identity["type_code"])
    if kind is not None:
        return kind, identity
    fb_sig = str(device.get("signature") or "").strip()
    fb_type = _device_type_code_hint(device)
    kind = _kind_from_identity(fb_sig, fb_type)
    if kind is None:
        raise ValueError("Окно настройки доступно только для устройств нашей линейки")
    patched = dict(identity)
    if fb_sig:
        patched["signature"] = fb_sig
    if fb_type is not None:
        patched["type_code"] = fb_type
    return kind, patched


def _module_kind_from_identity(signature: str, type_code: Optional[int]) -> module_profiles.ModuleKind:
    code = int(type_code) if type_code is not None else 0
    if code in module_profiles.TYPE_IO_CAPS:
        kind = module_profiles.kind_from_type_code(code)
        if kind.max_do or kind.max_di or kind.max_ao or kind.max_ai:
            return kind
    sig_code = module_profiles.code_from_signature(signature)
    if sig_code in module_profiles.TYPE_IO_CAPS:
        kind = module_profiles.kind_from_type_code(int(sig_code))
        if kind.max_do or kind.max_di or kind.max_ao or kind.max_ai:
            return kind
    caps = module_profiles.caps_from_signature(signature)
    if caps:
        sig_name = module_profiles.strip_bootloader_signature_suffix(signature).strip() or "MR/MP-02м"
        return module_profiles.ModuleKind(code, sig_name, caps[0], caps[1], caps[2], caps[3])
    return module_profiles.kind_from_type_code(code)


def _s16_from_reg(word: int) -> int:
    value = int(word) & 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def _s32_from_u32(value: int) -> int:
    raw = int(value) & 0xFFFFFFFF
    return raw - 0x100000000 if raw >= 0x80000000 else raw


def _relay_mode_module(kind: module_profiles.ModuleKind) -> bool:
    return int(kind.code) in (module_profiles.MP02_DO6DI8, module_profiles.MP02_DO4DI6)


def _read_do_bits(send, slave: int, count: int) -> List[bool]:
    bits: List[bool] = []
    payload, err = read_coils(send, slave, DO_COIL_START, count, 900)
    if not err and payload:
        bits = coil_bits_from_payload(payload, count)
    if len(bits) >= count:
        return [bool(bits[idx]) for idx in range(count)]
    regs = _read_regs(send, slave, INP_DO_FIRST, count, input_regs=True, timeout_ms=900)
    if regs:
        return [bool(int(regs[idx]) & 0xFFFF) if idx < len(regs) else False for idx in range(count)]
    return [False] * count


def _read_u32_pairs(send, slave: int, start: int, count_u32: int, *, input_regs: bool = True, timeout_ms: int = 1000) -> List[int]:
    regs = _read_regs(send, slave, start, max(0, int(count_u32)) * 2, input_regs=input_regs, timeout_ms=timeout_ms)
    values: List[int] = []
    for idx in range(int(count_u32)):
        base = idx * 2
        values.append(regs_u32_lo_hi(regs, base) if base + 1 < len(regs) else 0)
    return values


def _read_bit_mirror(send, slave: int, start: int, count: int) -> List[int]:
    for reader in (read_coils, read_discrete_inputs):
        payload, err = reader(send, slave, start, count, 900)
        if err or not payload:
            continue
        bits = coil_bits_from_payload(payload, count)
        if len(bits) >= count:
            return [1 if bits[idx] else 0 for idx in range(count)]
    return [0] * count


def _ao_raw_to_volts(raw: int) -> float:
    return float(int(raw) & 0xFFFF) / 100.0


def _read_ai_channels(send, slave: int, kind: module_profiles.ModuleKind) -> List[Dict[str, Any]]:
    channels: List[Dict[str, Any]] = []
    if kind.max_ai <= 0:
        return channels
    stride = module_profiles.ai_channel_stride(kind.code, kind)
    start = module_profiles.ai_channel_base_register(1, kind.code, kind)
    total = int(kind.max_ai) * int(stride)
    input_regs = _read_regs(send, slave, start, total, input_regs=True, timeout_ms=1200)
    holding_regs = _read_regs(send, slave, start, total, input_regs=False, timeout_ms=1200)
    for ch in range(1, int(kind.max_ai) + 1):
        offset = (ch - 1) * stride
        live = input_regs[offset : offset + stride]
        hold = holding_regs[offset : offset + stride] if holding_regs else live
        sensor_code = int((hold[0] if hold else live[0] if live else 0) or 0) & 0xFFFF
        measured_raw = None
        scaled_raw = None
        if len(live) >= 10:
            measured_raw = _s32_from_u32(regs_u32_lo_hi(live, 2))
            scaled_raw = _s32_from_u32(regs_u32_lo_hi(live, 8))
        elif len(live) >= 4:
            measured_raw = _s32_from_u32(regs_u32_lo_hi(live, 1))
            scaled_raw = _s16_from_reg(live[3])
        item: Dict[str, Any] = {
            "channel": ch,
            "register_base": module_profiles.ai_channel_base_register(ch, kind.code, kind),
            "sensor_code": sensor_code,
            "sensor_label": module_profiles.ai_sensor_label(sensor_code),
            "sidebar_tag": module_profiles.ai_sidebar_nav_mode_tag(sensor_code),
            "ui_bucket": module_profiles.ai_ui_sensor_bucket(sensor_code),
            "calibration_applicable": module_profiles.ai_ui_temperature_calibration_applicable(sensor_code),
            "measured_raw": measured_raw,
            "scaled_raw": scaled_raw,
            "calibration": _s16_from_reg(hold[4]) if len(hold) > 4 else 0,
        }
        if module_profiles.kind_has_mp_ai_adc_filters(kind):
            stor = (
                module_profiles.ai_stor_for_6ao6ai_p(ch)
                if module_profiles.is_6ao6ai_module(kind)
                else module_profiles.ai_stor_for_12ai_channel(ch)
            )
            kalman_reg = module_profiles.ai_kalman_holding_reg(stor)
            sps_reg, avg_reg, tau_reg = module_profiles.ai_wb_filter_holding_regs(stor)
            kalman = _read_u16(send, slave, kalman_reg, 800)
            filters = _read_regs(send, slave, sps_reg, 3, timeout_ms=900)
            item["filters"] = {
                "stor": stor,
                "kalman": 1 if int(kalman or 0) else 0,
                "sps": int(filters[0]) if len(filters) > 0 else module_profiles.AI_ADC_SAMPLE_RATES_SPS[1],
                "avg": int(filters[1]) if len(filters) > 1 else 0,
                "tau": int(filters[2]) if len(filters) > 2 else 0,
            }
        channels.append(item)
    return channels


def _mr_module_payload(kind: module_profiles.ModuleKind) -> Dict[str, Any]:
    max_do = int(kind.max_do)
    max_di = int(kind.max_di)
    max_ao = int(kind.max_ao)
    max_ai = int(kind.max_ai)
    return {
        "code": int(kind.code),
        "name": kind.name,
        "max_do": max_do,
        "max_di": max_di,
        "max_ao": max_ao,
        "max_ai": max_ai,
        "relay_mode_panel": _relay_mode_module(kind),
    }


def _mr_do_block_full(send, slave: int, kind: module_profiles.ModuleKind) -> Dict[str, Any]:
    max_do = int(kind.max_do)
    if max_do <= 0:
        return {"bits": [], "counts": [], "safe": [], "timer_words": [], "redelay": []}
    return {
        "bits": _read_do_bits(send, slave, max_do),
        "counts": _read_u32_pairs(send, slave, INP_DO_CNT_BASE, max_do, input_regs=True, timeout_ms=1000),
        "safe": _read_regs(send, slave, REG_SAFE_DO_BASE, max_do, timeout_ms=1000),
        "timer_words": _read_regs(send, slave, REG_TIMER_DO_BASE, min(6, max_do), timeout_ms=1000),
        "redelay": _read_regs(send, slave, REG_REDELAY_DO_FIRST, min(6, max_do), timeout_ms=1000),
    }


def _mr_di_block_full(send, slave: int, kind: module_profiles.ModuleKind) -> Dict[str, Any]:
    max_di = int(kind.max_di)
    if max_di <= 0:
        return {
            "values": [],
            "counts": [],
            "short_counts": [],
            "long_counts": [],
            "double_counts": [],
            "freq": [],
            "mode": [],
            "debounce": [],
            "long_press": [],
            "double_click": [],
            "freq_mode": [],
        }
    return {
        "values": [
            1 if int(v) else 0
            for v in _read_regs(send, slave, INP_DI_FIRST, max_di, input_regs=True, timeout_ms=1000)
        ],
        "counts": _read_u32_pairs(send, slave, INP_DI_CNT_BASE, max_di, input_regs=True, timeout_ms=1000),
        "short_counts": _read_regs(send, slave, INP_DI_SHORT_CNT_BASE, max_di, input_regs=True, timeout_ms=1000),
        "long_counts": _read_regs(send, slave, INP_DI_LONG_CNT_BASE, max_di, input_regs=True, timeout_ms=1000),
        "double_counts": _read_regs(send, slave, INP_DI_DOUBLE_CNT_BASE, max_di, input_regs=True, timeout_ms=1000),
        "freq": _read_regs(send, slave, INP_DI_FREQ_BASE, max_di, input_regs=True, timeout_ms=1000),
        "mode": _read_regs(send, slave, REG_DI_MODE_BASE, max_di, timeout_ms=1000),
        "debounce": _read_regs(send, slave, REG_DI_DEBOUNCE_BASE, max_di, timeout_ms=1000),
        "long_press": _read_regs(send, slave, REG_DI_LONG_PRESS_BASE, max_di, timeout_ms=1000),
        "double_click": _read_regs(send, slave, REG_DI_DOUBLE_CLICK_BASE, max_di, timeout_ms=1000),
        "freq_mode": _read_bit_mirror(send, slave, REG_DI_FREQ_MODE_BASE, min(8, max_di)),
    }


def _mr_ao_block_full(send, slave: int, kind: module_profiles.ModuleKind) -> Dict[str, Any]:
    max_ao = int(kind.max_ao)
    if max_ao <= 0:
        return {"current_raw": [], "setpoint": [], "safe": [], "safe_holding_regs": [], "current_volts": []}
    current_raw = _read_regs(send, slave, INP_AO_FIRST, max_ao, input_regs=True, timeout_ms=1000)
    payload: Dict[str, Any] = {
        "current_raw": current_raw,
        "setpoint": _read_regs(send, slave, INP_AO_FIRST, max_ao, timeout_ms=1000),
        "safe": _read_regs(
            send,
            slave,
            module_profiles.ao_safe_holding_register(1, kind),
            max_ao,
            timeout_ms=1000,
        ),
        "safe_holding_regs": [
            module_profiles.ao_safe_holding_register(ch, kind) for ch in range(1, max_ao + 1)
        ],
    }
    payload["current_volts"] = [_ao_raw_to_volts(raw) for raw in current_raw]
    return payload


def _mr_relay_block(send, slave: int, kind: module_profiles.ModuleKind) -> Dict[str, Any]:
    if not _relay_mode_module(kind):
        return {"mode": 0, "options": 0, "power_stagger": 0}
    return {
        "mode": int(_read_u16(send, slave, REG_RELAY_MODE, 800) or 0),
        "options": int(_read_u16(send, slave, REG_RELAY_OPTIONS, 800) or 0),
        "power_stagger": int(_read_u16(send, slave, REG_POWER_STAGGER, 800) or 0),
    }


def _read_mr_snapshot(send, slave: int, identity: Dict[str, Any]) -> Dict[str, Any]:
    kind = _module_kind_from_identity(identity.get("signature", ""), identity.get("type_code"))
    payload: Dict[str, Any] = {
        "module": _mr_module_payload(kind),
        "do": _mr_do_block_full(send, slave, kind),
        "di": _mr_di_block_full(send, slave, kind),
        "ao": _mr_ao_block_full(send, slave, kind),
        "ai": {"channels": _read_ai_channels(send, slave, kind)},
        "inactivity_s": int(_read_u16(send, slave, REG_MODBUS_INACTIVITY_S, 800) or 0),
        "relay": _mr_relay_block(send, slave, kind),
        "mcu": _read_mr_mcu_info(send, slave),
    }
    return payload


def _read_mr_snapshot_minimal(send, slave: int, identity: Dict[str, Any]) -> Dict[str, Any]:
    """Лёгкий снимок для сайдбара и live-подписей (без полных AI/DI/AO настроек)."""
    kind = _module_kind_from_identity(identity.get("signature", ""), identity.get("type_code"))
    max_do = int(kind.max_do)
    max_di = int(kind.max_di)
    max_ao = int(kind.max_ao)
    max_ai = int(kind.max_ai)

    ai_channels: List[Dict[str, Any]] = []
    if max_ai > 0:
        stride = module_profiles.ai_channel_stride(kind.code, kind)
        total_regs = max_ai * stride
        holding_regs = _read_regs(send, slave, 400, total_regs, timeout_ms=1200)
        for ch in range(1, max_ai + 1):
            offset = (ch - 1) * stride
            sensor_code = (
                int(holding_regs[offset]) & 0xFFFF if offset < len(holding_regs) else 0
            )
            ai_channels.append(
                {
                    "channel": ch,
                    "register_base": module_profiles.ai_channel_base_register(ch, kind.code, kind),
                    "sensor_code": sensor_code,
                    "sensor_label": module_profiles.ai_sensor_label(sensor_code),
                    "sidebar_tag": module_profiles.ai_sidebar_nav_mode_tag(sensor_code),
                    "ui_bucket": module_profiles.ai_ui_sensor_bucket(sensor_code),
                }
            )

    payload: Dict[str, Any] = {
        "module": _mr_module_payload(kind),
        "do": {
            "bits": _read_do_bits(send, slave, max_do) if max_do > 0 else [],
            "counts": _read_u32_pairs(send, slave, INP_DO_CNT_BASE, max_do, input_regs=True, timeout_ms=1000)
            if max_do > 0
            else [],
        },
        "di": {
            "values": [
                1 if int(v) else 0
                for v in _read_regs(send, slave, INP_DI_FIRST, max_di, input_regs=True, timeout_ms=1000)
            ]
            if max_di > 0
            else [],
        },
        "ao": {
            "current_raw": _read_regs(send, slave, INP_AO_FIRST, max_ao, input_regs=True, timeout_ms=1000)
            if max_ao > 0
            else [],
        },
        "ai": {"channels": ai_channels},
        "inactivity_s": int(_read_u16(send, slave, REG_MODBUS_INACTIVITY_S, 800) or 0),
        "relay": _mr_relay_block(send, slave, kind),
        "mcu": {},
    }
    payload["ao"]["current_volts"] = [_ao_raw_to_volts(raw) for raw in payload["ao"]["current_raw"]]
    return payload


def _read_mr_snapshot_panel(
    send,
    slave: int,
    identity: Dict[str, Any],
    active_tab: Optional[str],
) -> Dict[str, Any]:
    """Снимок для фонового опроса: минимум для сайдбара + полный блок только активной вкладки IO."""
    kind = _module_kind_from_identity(identity.get("signature", ""), identity.get("type_code"))
    base = _read_mr_snapshot_minimal(send, slave, identity)
    tab = str(active_tab or "").strip().lower()
    max_do = int(kind.max_do)
    max_di = int(kind.max_di)
    max_ao = int(kind.max_ao)
    max_ai = int(kind.max_ai)

    out: Dict[str, Any] = {
        "module": base["module"],
        "do": dict(base["do"]),
        "di": dict(base["di"]),
        "ao": dict(base["ao"]),
        "ai": {"channels": list(base["ai"]["channels"])},
        "inactivity_s": base["inactivity_s"],
        "relay": dict(base["relay"]),
        "mcu": base.get("mcu", {}),
    }

    if tab == "info":
        out["mcu"] = _read_mr_mcu_info(send, slave)
    elif tab.startswith("do_") and max_do > 0:
        out["do"] = _mr_do_block_full(send, slave, kind)
    elif tab.startswith("di_") and max_di > 0:
        out["di"] = _mr_di_block_full(send, slave, kind)
    elif tab.startswith("ao_") and max_ao > 0:
        out["ao"] = _mr_ao_block_full(send, slave, kind)
    elif tab.startswith("ai_") and max_ai > 0:
        out["ai"] = {"channels": _read_ai_channels(send, slave, kind)}
    elif tab == "relay" and _relay_mode_module(kind):
        out["relay"] = _mr_relay_block(send, slave, kind)

    return out


def _allowed_mr_holding_registers(kind: module_profiles.ModuleKind) -> set[int]:
    allowed: set[int] = set()
    max_do = int(kind.max_do)
    max_di = int(kind.max_di)
    max_ao = int(kind.max_ao)
    max_ai = int(kind.max_ai)
    if max_do > 0:
        allowed.update(range(REG_SAFE_DO_BASE, REG_SAFE_DO_BASE + max_do))
        allowed.update(range(REG_TIMER_DO_BASE, REG_TIMER_DO_BASE + min(6, max_do)))
        allowed.update(range(REG_REDELAY_DO_FIRST, REG_REDELAY_DO_FIRST + min(6, max_do)))
        allowed.add(REG_MODBUS_INACTIVITY_S)
        allowed.add(REG_RESET_DO_COUNTERS)
    allowed.add(120)  # Команда перезагрузки устройства
    if _relay_mode_module(kind):
        allowed.update({REG_RELAY_MODE, REG_RELAY_OPTIONS, REG_POWER_STAGGER})
    if max_di > 0:
        allowed.update(range(REG_DI_MODE_BASE, REG_DI_MODE_BASE + max_di))
        allowed.update(range(REG_DI_DEBOUNCE_BASE, REG_DI_DEBOUNCE_BASE + max_di))
        allowed.update(range(REG_DI_LONG_PRESS_BASE, REG_DI_LONG_PRESS_BASE + max_di))
        allowed.update(range(REG_DI_DOUBLE_CLICK_BASE, REG_DI_DOUBLE_CLICK_BASE + max_di))
        allowed.update(range(REG_DI_FREQ_MODE_BASE, REG_DI_FREQ_MODE_BASE + min(8, max_di)))
        allowed.add(REG_RESET_DI_COUNTERS)
    if max_ao > 0:
        allowed.update(range(INP_AO_FIRST, INP_AO_FIRST + max_ao))
        ao_safe_start = module_profiles.ao_safe_holding_register(1, kind)
        allowed.update(range(ao_safe_start, ao_safe_start + max_ao))
        allowed.add(REG_MODBUS_INACTIVITY_S)
    if max_ai > 0:
        for ch in range(1, max_ai + 1):
            base = module_profiles.ai_channel_base_register(ch, kind.code, kind)
            allowed.add(base)
            allowed.add(module_profiles.ai_calibration_holding_register(ch, kind.code, kind))
            if module_profiles.kind_has_mp_ai_adc_filters(kind):
                stor = (
                    module_profiles.ai_stor_for_6ao6ai_p(ch)
                    if module_profiles.is_6ao6ai_module(kind)
                    else module_profiles.ai_stor_for_12ai_channel(ch)
                )
                allowed.add(module_profiles.ai_kalman_holding_reg(stor))
                allowed.update(module_profiles.ai_wb_filter_holding_regs(stor))
    return allowed


def _read_network(send, slave: int, fallback_device: Dict[str, Any]) -> Dict[str, Any]:
    regs_110 = _read_regs(send, slave, REG_NET_BAUD, 3, timeout_ms=800)
    addr_u16 = _read_u16(send, slave, REG_NET_ADDR, 800)
    fast_u16 = _read_u16(send, slave, REG_FAST_MODBUS, 800)

    if len(regs_110) >= 3:
        baud = int(regs_110[0]) * 100
        parity = PARITY_REG_TO_CHAR.get(int(regs_110[1]) & 0xFFFF, "N")
        stop = int(regs_110[2]) & 0xFFFF
    else:
        baud, parity, stop = _device_line(fallback_device)
    if stop not in (1, 2):
        stop = 1
    if baud <= 0:
        baud = int(fallback_device.get("baudrate") or 9600)

    return {
        "baudrate": baud,
        "parity": parity,
        "stopbits": stop,
        "address": int(addr_u16 or fallback_device.get("address") or 1) & 0xFFFF,
        "fast_modbus": bool(fast_u16) if fast_u16 is not None else False,
    }


def _read_ce_snapshot(send, slave: int) -> Dict[str, Any]:
    regs = _read_regs(send, slave, CE_INPUT_START, CE_INPUT_COUNT, input_regs=True, timeout_ms=1100)
    if len(regs) < CE_INPUT_COUNT:
        regs = list(regs) + [0] * (CE_INPUT_COUNT - len(regs))

    def _s16(v: int) -> int:
        return v - 0x10000 if v >= 0x8000 else v

    def _i32(lo: int, hi: int) -> int:
        v = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
        return v - 0x100000000 if v >= 0x80000000 else v

    cfg = _read_regs(send, slave, CE_CFG_START, CE_CFG_COUNT, input_regs=False, timeout_ms=1000)
    while len(cfg) < CE_CFG_COUNT:
        cfg.append(0)

    return {
        "live": {
            "ua": regs[0] / 10.0,
            "ub": regs[1] / 10.0,
            "uc": regs[2] / 10.0,
            "uab": regs[6] / 10.0,
            "ubc": regs[7] / 10.0,
            "uca": regs[8] / 10.0,
            "ia": regs[10] / 1000.0,
            "ib": regs[11] / 1000.0,
            "ic": regs[12] / 1000.0,
            "in": regs[13] / 1000.0,
            "pa": _i32(regs[18], regs[19]),
            "pb": _i32(regs[20], regs[21]),
            "pc": _i32(regs[22], regs[23]),
            "pt": _i32(regs[24], regs[25]),
            "qa": _i32(regs[26], regs[27]),
            "qb": _i32(regs[28], regs[29]),
            "qc": _i32(regs[30], regs[31]),
            "qt": _i32(regs[32], regs[33]),
            "sa": _i32(regs[34], regs[35]),
            "sb": _i32(regs[36], regs[37]),
            "sc": _i32(regs[38], regs[39]),
            "st": _i32(regs[40], regs[41]),
            "freq": regs[42] / 100.0,
            "cfa": _s16(regs[43]) / 1000.0,
            "cfb": _s16(regs[44]) / 1000.0,
            "cfc": _s16(regs[45]) / 1000.0,
            "cft": _s16(regs[46]) / 1000.0,
            "tasic": _s16(regs[47]),
        },
        "config": {
            "ph_loss": int(cfg[0]) & 0xFFFF,
            "inv_a": int(cfg[1]) & 0xFFFF,
            "inv_b": int(cfg[2]) & 0xFFFF,
            "inv_c": int(cfg[3]) & 0xFFFF,
            "kt_a": int(cfg[4]) & 0xFFFF,
            "kt_b": int(cfg[5]) & 0xFFFF,
            "kt_c": int(cfg[6]) & 0xFFFF,
        },
    }


def _read_dtv_snapshot(send, slave: int) -> Dict[str, Any]:
    regs = _read_regs(
        send,
        slave,
        dtv_registers.DTV_INPUT_LIVE_START,
        dtv_registers.DTV_INPUT_LIVE_COUNT,
        input_regs=True,
        timeout_ms=1100,
    )
    while len(regs) < dtv_registers.DTV_INPUT_LIVE_COUNT:
        regs.append(0)

    def _s16_tenths(v: int) -> float:
        return float(dtv_registers.dtv_u16_to_s16(v)) / 10.0

    eco2_ppm = float(int(regs[20]) & 0xFFFF) / 2.0
    calib_regs = _read_regs(send, slave, 31, 9, input_regs=False, timeout_ms=1000)
    while len(calib_regs) < 9:
        calib_regs.append(0)
    mov_avg = _read_regs(
        send,
        slave,
        dtv_registers.DTV_MOV_AVG_HOLDING_ANCHOR,
        dtv_registers.DTV_MOV_AVG_HOLDING_SPAN,
        input_regs=False,
        timeout_ms=1100,
    )
    ext_sel = _read_u16(send, slave, dtv_registers.DTV_HOLDING_EXT_TEMP_SELECT, 800)
    pres_delay = _read_u16(send, slave, dtv_registers.DTV_HOLDING_PRESENCE_OFF_DELAY, 800)

    coils_payload, coils_err = read_coils(
        send,
        slave,
        dtv_registers.DTV_COIL_BUZZER,
        2,
        1000,
    )
    coil_bits = coil_bits_from_payload(coils_payload or b"", 2) if not coils_err else [False, False]

    moving_average_depths: List[int] = []
    for slot in range(dtv_registers.DTV_MOV_AVG_SLOT_COUNT):
        idx = 3 * slot + 1
        raw = int(mov_avg[idx]) if idx < len(mov_avg) else dtv_registers.DTV_MOV_AVG_DEPTH_MIN
        moving_average_depths.append(
            max(dtv_registers.DTV_MOV_AVG_DEPTH_MIN, min(dtv_registers.DTV_MOV_AVG_DEPTH_MAX, raw))
        )

    return {
        "live": {
            "t_ds": _s16_tenths(regs[0]),
            "t_mcp": _s16_tenths(regs[1]),
            "t_hdc": _s16_tenths(regs[2]),
            "t_bme280": _s16_tenths(regs[3]),
            "t_bme680": _s16_tenths(regs[4]),
            "t_ext": _s16_tenths(regs[5]),
            "h_hdc": regs[6] / 10.0,
            "h_bme280": regs[7] / 10.0,
            "h_bme680": regs[8] / 10.0,
            "p_bme280_mmhg": dtv_registers.dtv_u16_to_s16(regs[9]),
            "p_bme680_mmhg": dtv_registers.dtv_u16_to_s16(regs[10]),
            "p_bme280_kpa": dtv_registers.dtv_u16_to_s16(regs[11]) / 100.0,
            "p_bme680_kpa": dtv_registers.dtv_u16_to_s16(regs[12]) / 100.0,
            "alt_bme280": dtv_registers.dtv_u16_to_s16(regs[13]),
            "alt_bme680": dtv_registers.dtv_u16_to_s16(regs[14]),
            "voc": int(regs[15]) & 0xFFFF,
            "iaq_bme": int(regs[16]) & 0xFFFF,
            "co2_bme": int(regs[17]) & 0xFFFF,
            "tvoc_z": int(regs[18]) & 0xFFFF,
            "iaq_z": int(regs[19]) & 0xFFFF,
            "eco2_z": eco2_ppm,
            "etoh_z": int(regs[21]) & 0xFFFF,
            "light": int(regs[24]) & 0xFFFF,
            "presence": int(regs[25]) & 0xFFFF,
            "presence_ld2412": int(regs[26]) & 0xFFFF,
            "ld_dist_mov": int(regs[27]) & 0xFFFF,
            "ld_dist_still": int(regs[28]) & 0xFFFF,
            "ld_dist": int(regs[29]) & 0xFFFF,
        },
        "settings": {
            "ext_temp_select": 1 if int(ext_sel or 0) == 1 else 0,
            "presence_off_delay": int(pres_delay or 0),
            "buzzer_on": bool(coil_bits[0]) if coil_bits else False,
            "leds_on": bool(coil_bits[1]) if len(coil_bits) > 1 else False,
            "calibration_offsets": [
                dtv_registers.dtv_u16_to_s16(v & 0xFFFF)
                for v in calib_regs[:9]
            ],
            "moving_average_depths": moving_average_depths,
        },
    }


def snapshot_for_device(
    device_path: str,
    device: Dict[str, Any],
    *,
    snapshot_detail: str = "full",
    active_tab: Optional[str] = None,
) -> Dict[str, Any]:
    """Снимок для web UI.
    ``snapshot_detail``: ``full`` — полный MR-блок; ``minimal`` — только live сайдбара;
    ``panel`` — minimal + расширенное чтение блока, соответствующего ``active_tab`` (как desktop wake-тик).
    """
    detail = str(snapshot_detail or "full").strip().lower()
    if detail not in ("full", "minimal", "panel"):
        detail = "full"
    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        identity = _read_live_identity(send, slave, device)
        kind, identity = _resolve_kind(identity, device)

        network = _read_network(send, slave, device)
        payload: Dict[str, Any] = {
            "kind": kind,
            "info": {
                "address": network["address"],
                "serial": int(identity["serial"]) & 0xFFFFFFFF,
                "signature": identity["signature"] or str(device.get("signature") or ""),
                "app_version": identity["app_version"],
                "bootloader_version": identity["bootloader_version"],
                "type_code": identity["type_code"],
                "line": {
                    "baudrate": network["baudrate"],
                    "parity": network["parity"],
                    "stopbits": network["stopbits"],
                },
            },
            "network": network,
        }
        detail_out = detail
        if kind == "ce":
            payload["ce"] = _read_ce_snapshot(send, slave)
            detail_out = "full"
        elif kind == "dtv":
            payload["dtv"] = _read_dtv_snapshot(send, slave)
            detail_out = "full"
        elif kind == "mr":
            if detail == "minimal":
                payload["mr"] = _read_mr_snapshot_minimal(send, slave, identity)
            elif detail == "panel":
                payload["mr"] = _read_mr_snapshot_panel(send, slave, identity, active_tab)
            else:
                payload["mr"] = _read_mr_snapshot(send, slave, identity)
        payload["snapshot_detail"] = detail_out
        payload["active_tab"] = active_tab
        return payload
    finally:
        close_transport()


def apply_network_settings(
    device_path: str,
    device: Dict[str, Any],
    network: Dict[str, Any],
) -> Dict[str, Any]:
    baud = int(network.get("baudrate") or 0)
    baud_reg = baud // 100
    if baud_reg not in ALLOWED_BAUD_REG_CODES:
        raise ValueError("Скорость линии вне допустимого диапазона")
    parity = _normalize_parity(network.get("parity"))
    stopbits = int(network.get("stopbits") or 1)
    if stopbits not in (1, 2):
        raise ValueError("Стоп-биты: только 1 или 2")
    address = int(network.get("address") or 0)
    if not 1 <= address <= 247:
        raise ValueError("Modbus-адрес: только 1..247")
    want_fast = bool(network.get("fast_modbus"))

    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        current = _read_network(send, slave, device)
        current_fast = bool(current.get("fast_modbus"))
        if current["address"] != address:
            err = write_multiple(send, slave, REG_NET_ADDR, [address & 0xFFFF], 900)
            if err:
                raise RuntimeError(f"Запись рег. 128: {err}")
            slave = address

        if want_fast and not current_fast:
            err = write_single(send, slave, REG_FAST_MODBUS, 1, 800)
            if err:
                raise RuntimeError(f"Запись рег. 122: {err}")

        line_changed = (
            int(current.get("baudrate") or 0) != baud
            or str(current.get("parity") or "N") != parity
            or int(current.get("stopbits") or 1) != stopbits
        )
        if line_changed:
            err = write_multiple(
                send,
                slave,
                REG_NET_BAUD,
                [baud_reg & 0xFFFF, PARITY_CHAR_TO_REG[parity], stopbits & 0xFFFF],
                1200,
            )
            if err:
                raise RuntimeError(f"Запись рег. 110-112: {err}")
            if int(current.get("baudrate") or 0) != baud:
                time.sleep(0.40)

        if (not want_fast) and current_fast:
            err = write_single(send, slave, REG_FAST_MODBUS, 0, 800)
            if err:
                raise RuntimeError(f"Запись рег. 122: {err}")
    finally:
        close_transport()

    updated_device = dict(device)
    updated_device.update(
        {
            "address": address,
            "baudrate": baud,
            "parity": parity,
            "stopbits": stopbits,
        }
    )
    return snapshot_for_device(device_path, updated_device, snapshot_detail="full")


def write_allowed_holding(
    device_path: str,
    device: Dict[str, Any],
    reg: int,
    value: int,
) -> Dict[str, Any]:
    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        identity = _read_live_identity(send, slave, device)
        kind, identity = _resolve_kind(identity, device)
        allowed_regs: set[int] = set()
        if kind == "dtv":
            allowed_regs.update({dtv_registers.DTV_HOLDING_EXT_TEMP_SELECT, dtv_registers.DTV_HOLDING_PRESENCE_OFF_DELAY})
            allowed_regs.update(range(31, 40))
            allowed_regs.update(dtv_registers.DTV_MOV_AVG_DEPTH_REGS)
        elif kind == "ce":
            allowed_regs.update(range(553, 560))
        elif kind == "mr":
            allowed_regs.update(
                _allowed_mr_holding_registers(
                    _module_kind_from_identity(identity["signature"], identity["type_code"])
                )
            )
        if reg not in allowed_regs:
            raise ValueError("Запись этого регистра через веб-окно не разрешена")
        target_val = int(value) & 0xFFFF
        err = write_single(send, slave, reg, target_val, 700)
        if err:
            raise RuntimeError(f"Запись рег. {reg}: {err}")
    finally:
        close_transport()
    return snapshot_for_device(device_path, device, snapshot_detail="full")


def write_allowed_coil(
    device_path: str,
    device: Dict[str, Any],
    coil: int,
    on: bool,
) -> Dict[str, Any]:
    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        identity = _read_live_identity(send, slave, device)
        kind, identity = _resolve_kind(identity, device)
        if kind == "dtv":
            if coil not in (dtv_registers.DTV_COIL_BUZZER, dtv_registers.DTV_COIL_LEDS_ALL):
                raise ValueError("Запись этой катушки через веб-окно не разрешена")
        elif kind == "mr":
            module_kind = _module_kind_from_identity(identity["signature"], identity["type_code"])
            if coil < DO_COIL_START or coil >= DO_COIL_START + int(module_kind.max_do):
                raise ValueError("Запись этой катушки через веб-окно не разрешена")
            if _relay_mode_module(module_kind):
                # Как в desktop flasher: для релейных модулей сначала пытаемся
                # перевести устройство в ручной режим, но не блокируем управление
                # DO, если эта вспомогательная запись не ответила.
                relay_mode = _read_u16(send, slave, REG_RELAY_MODE, 800)
                if relay_mode is None or int(relay_mode) != 0:
                    write_single(send, slave, REG_RELAY_MODE, 0, 1200)
        else:
            raise ValueError("Запись этой катушки через веб-окно не разрешена")
        err = write_coil(send, slave, coil, bool(on), 2000)
        if err and kind == "mr":
            bit_idx = int(coil) - DO_COIL_START
            bits = _read_do_bits(send, slave, int(module_kind.max_do))
            if 0 <= bit_idx < len(bits) and bool(bits[bit_idx]) == bool(on):
                err = None
        if err:
            raise RuntimeError(f"Запись coil {coil}: {err}")
    finally:
        close_transport()
    return snapshot_for_device(device_path, device, snapshot_detail="full")

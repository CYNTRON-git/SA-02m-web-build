# -*- coding: utf-8 -*-
"""
Снимки и запись настроек устройств для веб-окон конфигурации.

Поддерживаются:
  - линейка MP/MR-02m: сведения + сеть;
  - DTV / Sens.: живые данные, профильные настройки, сеть;
  - CE-02m-3: живые данные, ТТ/фазы, сеть;
  - приточная установка Carel (c.pCOmini / uAria): живые данные и команды.

Carel — чужой ПЛК, и это меняет форму снимка: у него нет ни сигнатуры (рег. 290),
ни серийного (270–271), ни блока сети (110–112, 122, 128). Опрос этих адресов
даёт восемь таймаутов подряд — около пяти секунд внутри окна, которое обновляется
раз в секунду. Поэтому личность установки берётся из строки скана (её заполнил
FC17), а сетевой блок отдаётся как «только чтение».
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import bus_mode
from . import carel_poll
from . import dtv_registers
from . import led_poll
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
REG_FAST_MODBUS = 122  # family-common bus-mode selector: 0 classic / 1 Fast / 2 BACnet
REG_NET_ADDR = 128

CAREL_KIND = "carel"
LED_KIND = "led"


def family_from_kind(kind: Optional[str]) -> Optional[str]:
    """Семейство bus_mode из kind снимка: mr → FAMILY_MR, dtv → FAMILY_DTV,
    иначе None (CE-02m-3 / WB / bootloader — селектор шины не поддерживают)."""
    if kind == "mr":
        return bus_mode.FAMILY_MR
    if kind == "dtv":
        return bus_mode.FAMILY_DTV
    return None

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
    # Carel — первым: его app id из FC17 ни на что из нашей линейки не похож, а
    # code_from_signature ниже уже вернул бы CAREL_AHU и увёл ветку в «mr».
    if module_profiles.signature_is_carel(sig):
        return CAREL_KIND
    # Лента — до линейки MR: её сигнатура не проходит батч-прошивочный отбор
    # (L2), поэтому без этой ветки окно настройки для неё вообще не открылось бы.
    # Порядок «сигнатура важнее рег. 0» задаёт module_profiles.scan_type_code —
    # одно место, здесь только вызов.
    if module_profiles.scan_type_code(sig, type_code) == module_profiles.RGBW_WS2812:
        return LED_KIND
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
    # Лента: сигнатура перевешивает Input reg 0. Порядок и его причина — в
    # module_profiles.scan_type_code (одно место), здесь только вызов: ветка
    # ниже начинает с рег. 0, и ложный код 1..15 с общей линии увёл бы ленту
    # в карту дискретного модуля.
    if module_profiles.scan_type_code(signature, type_code) == module_profiles.RGBW_WS2812:
        return module_profiles.kind_from_type_code(module_profiles.RGBW_WS2812)
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
    # Fault flags: Input 107 = below-limit, Input 108 = above-limit, one bit per
    # logical channel (bit ch-1). One paired read per snapshot (O(1), not per
    # channel) on the modal-scoped poll. Ref: MR-02m MODBUS_VARIABLES.txt:261-262;
    # decode ai_input_limit_flags_for_channel. Sensor-type gating is client-side.
    fault_low_word = 0
    fault_high_word = 0
    fault_regs = _read_regs(send, slave, 107, 2, input_regs=True, timeout_ms=900)
    if len(fault_regs) >= 2:
        fault_low_word = int(fault_regs[0]) & 0xFFFF
        fault_high_word = int(fault_regs[1]) & 0xFFFF
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
            # calibration applicability is derived client-side from sensor_code
            # (ai_ui_uses_value_calibration: temp ∪ volt ∪ curr) — not a snapshot
            # field; see docs/contracts/module-config-ai.md.
            "measured_raw": measured_raw,
            "scaled_raw": scaled_raw,
            "calibration": _s16_from_reg(hold[4]) if len(hold) > 4 else 0,
            "limit_low": _s16_from_reg(hold[5]) if len(hold) > 5 else 0,
            "limit_high": _s16_from_reg(hold[6]) if len(hold) > 6 else 0,
            "fault_low": bool((fault_low_word >> (ch - 1)) & 1) if ch <= 16 else False,
            "fault_high": bool((fault_high_word >> (ch - 1)) & 1) if ch <= 16 else False,
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
            # AI measurement-range limits (int16): Нижний/Верхний предел, base+5/base+6.
            # Editable for active volt/curr modes; rescales the module scale in-place.
            allowed.add(base + 5)
            allowed.add(base + 6)
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
        # Legacy 2-state view kept byte-identical for a cached old bundle
        # (deployed-cache compatibility): Fast enabled ⇔ reg122 == 1.
        "fast_modbus": bool(fast_u16) if fast_u16 is not None else False,
        # 3-state field-bus selector (§5.1): raw read + fail-closed sanitized value.
        "bus_mode_raw": (int(fast_u16) & 0xFFFF) if fast_u16 is not None else None,
        "bus_mode": bus_mode.sanitize(fast_u16),
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


# ── приточная установка Carel ──────────────────────────────────────────────
# Список команд закрыт: окно шлёт имя действия, а план записи собирает общий
# пакет. Одиночные регистры и катушки установке не пишутся вовсе — пуск это
# последовательность (разрешение → выдержка → команда), а не один регистр.
CAREL_ACTIONS: Tuple[str, ...] = (
    "start",
    "stop",
    "alarm_reset",
    "net_enable",
    "sys_mode",
    "sp_winter",
    "sp_summer",
    "fan_supply",
    "fan_exhaust",
    "fan_step",
)

_CAREL_SINGLE_WRITE_REFUSED = (
    "Установка Carel управляется командами окна, а не записью "
    "одиночного регистра или катушки"
)

# Вкладка «Входы/выходы»: дорогой блок (два набора блоков IR + длинный DI),
# читается только когда она открыта, не на каждом такте опроса.
CAREL_IO_TAB = "carel_io"


def _carel_map() -> Any:
    """Общая карта Carel через шов пакета (module_profiles.carel_ahu())."""
    ca = module_profiles.carel_ahu()
    if ca is None:
        raise ValueError("Пакет карты Carel не установлен на этом устройстве")
    return ca


def _device_is_carel(device: Dict[str, Any]) -> bool:
    """Опознание по строке скана: живого чтения для этого не делаем (см. модуль)."""
    return module_profiles.signature_is_carel(str(device.get("signature") or ""))


def _carel_identity(device: Dict[str, Any]) -> Dict[str, Any]:
    ca = _carel_map()
    signature = str(device.get("signature") or "").strip()
    app_version = str(device.get("app_version") or "—").strip() or "—"
    variant = str(device.get("carel_variant") or "").strip().upper()
    return {
        "signature": signature,
        "family": ca.family_from_signature(signature),
        "app_version": app_version,
        "variant": variant,
    }


def _carel_unit_status_text(ca: Any, family: str, snap: Dict[str, Any], app_version: str) -> str:
    unit = snap.get("unit")
    if unit is None:
        return ""
    if family == ca.FAMILY_UARIA:
        return ca.uaria_unit_status_label(int(unit))
    return ca.unit_status_label(int(unit), app_version)


def _carel_unit_status_algo(ca: Any, family: str, app_version: str) -> str:
    """Какой таблицей UnitStatus подписан код.

    У uAria своя таблица (короткая карта), поэтому «v2» здесь было бы неправдой —
    честнее назвать её своим именем, чем подогнать под пару v1/v2 c.pCOmini.
    """
    if family == ca.FAMILY_UARIA:
        return "uaria"
    return "v2" if ca.unit_status_use_v2(app_version) else "v1"


def _carel_payload(
    send,
    slave: int,
    device: Dict[str, Any],
    *,
    io_hw: bool = False,
) -> Dict[str, Any]:
    ca = _carel_map()
    ident = _carel_identity(device)
    family = ident["family"]
    baud, parity, stop = _device_line(device)
    snap = carel_poll.read_carel_snapshot(
        send, slave, family, io_hw=io_hw, variant=ident["variant"]
    ) or {}
    carel: Dict[str, Any] = dict(snap)
    carel["plant_state"] = ca.plant_run_state(snap, family, version=ident["app_version"])
    carel["unit_status_text"] = _carel_unit_status_text(ca, family, snap, ident["app_version"])
    carel["unit_status_algo"] = _carel_unit_status_algo(ca, family, ident["app_version"])
    carel["alarm_reset_coil"] = ca.alarm_reset_coil(family)
    carel["answered"] = bool(snap)
    line = {"baudrate": baud, "parity": parity, "stopbits": stop}
    return {
        "kind": CAREL_KIND,
        "family": family,
        "bus_mode_supported": False,
        "info": {
            "address": slave,
            "signature": ident["signature"],
            "app_version": ident["app_version"],
            "carel_variant": ident["variant"],
            "variant_label": ca.variant_label(ident["variant"]),
            "model": ca.info_model_label(ident["signature"]),
            "line": dict(line),
        },
        "network": dict(line, address=slave, writable=ca.network_params_writable(family)),
        "carel": carel,
    }


def _carel_coil_state(send, slave: int, coil: int, timeout_ms: int = 700) -> Optional[bool]:
    payload, err = read_coils(send, slave, coil, 1, timeout_ms)
    if err or not payload:
        return None
    bits = coil_bits_from_payload(payload, 1)
    return bool(bits[0]) if bits else None


def _carel_number(params: Dict[str, Any], key: str = "value") -> float:
    raw = params.get(key)
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("Недопустимое значение команды Carel") from None


def _carel_write_plan(
    ca: Any,
    family: str,
    action: str,
    params: Dict[str, Any],
    *,
    net_enable: Optional[int] = None,
) -> List[Any]:
    """План записи для действия окна. Планы приходят из общей карты; здесь только
    выбор плана и разбор параметра. Катушка 30 uAria (местное управление) не
    встречается ни в одной ветке — установку по сети запускает катушка 0."""
    uaria = family == ca.FAMILY_UARIA
    if action == "start":
        if uaria:
            writes, err = ca.uaria_start_writes(net_enable, True)
            if err:
                raise ValueError("Не прочитано разрешение пуска по сети (Gs04)")
            return writes
        writes, _err = ca.crst_start_writes({}, None, True)
        return writes
    if action == "stop":
        if uaria:
            writes, _err = ca.uaria_start_writes(net_enable, False)
            return writes
        writes, _err = ca.start_write_plan(None, None, False)
        return writes
    if action == "net_enable":
        return [ca.net_enable_write(family, bool(params.get("enable")))]
    if action == "sys_mode":
        if uaria:
            raise ValueError("Режим системы доступен только для c.pCOmini")
        target = ca.clamp_sys_mode(int(_carel_number(params)))
        if target == 0:
            writes, _err = ca.start_write_plan(None, 0, None)
            return writes
        writes, _err = ca.crst_start_writes({}, target, True)
        return writes
    if action in ("sp_winter", "sp_summer"):
        summer = action == "sp_summer"
        if uaria:
            addr = ca.HR_UARIA_SP_SUMMER if summer else ca.HR_UARIA_SP
            hi, lo = ca.float32_to_be_words(ca.clamp_uaria_sp_c(_carel_number(params)))
            return [ca.CarelWrite(ca.KIND_HOLDING_MULTI, addr, 0, (hi, lo))]
        addr = ca.HR_SP_SUMMER if summer else ca.HR_SP_WINTER
        raw = ca.phys_to_raw_x10(_carel_number(params), ca.SP_C_MIN, ca.SP_C_MAX)
        return [ca.CarelWrite(ca.KIND_HOLDING, addr, raw)]
    if action in ("fan_supply", "fan_exhaust"):
        if uaria:
            raise ValueError("Уставки вентиляторов в процентах доступны только для c.pCOmini")
        addr = ca.HR_FAN_EXHAUST if action == "fan_exhaust" else ca.HR_FAN_SUPPLY
        raw = ca.phys_to_raw_x10(_carel_number(params), ca.FAN_PCT_MIN, ca.FAN_PCT_MAX)
        return [ca.CarelWrite(ca.KIND_HOLDING, addr, raw)]
    if action == "fan_step":
        if not uaria:
            raise ValueError("Ступень вентилятора доступна только для uAria")
        step = ca.clamp_uaria_fan_step(int(_carel_number(params)))
        return [ca.CarelWrite(ca.KIND_HOLDING, ca.HR_UARIA_FAN_SP, step)]
    raise ValueError("Неизвестная команда установки Carel")


def _carel_run_writes(ca: Any, send, slave: int, family: str, writes: Sequence[Any]) -> None:
    """Выполнить план. Ma18 (разрешение сети) требует выдержки перед пуском:
    без неё ПЛК принимает катушку 65 раньше, чем разрешение вступило в силу."""
    for w in writes:
        addr = int(w.address)
        if (
            family == ca.FAMILY_UARIA
            and w.kind == ca.KIND_COIL
            and addr == ca.COIL_UARIA_LOCAL
        ):
            # Защита в глубину: ни один план сюда не приводит, и если приведёт —
            # обмена не будет. Катушка местного управления принадлежит панели ПЛК.
            raise ValueError("Катушка местного управления uAria из веба не пишется")
        if w.kind == ca.KIND_COIL:
            err = write_coil(send, slave, addr, bool(w.value), 800)
            if err is None and addr == ca.COIL_MA18 and int(w.value) == 1:
                time.sleep(ca.START_MA18_SETTLE_S)
        elif w.kind == ca.KIND_HOLDING_MULTI:
            err = write_multiple(send, slave, addr, [int(x) & 0xFFFF for x in w.words], 800)
        else:
            err = write_single(send, slave, addr, int(w.value) & 0xFFFF, 800)
        if err:
            raise RuntimeError(f"Запись Carel ({w.kind} {addr}): {err}")


def _carel_alarm_reset(ca: Any, send, slave: int, family: str) -> None:
    """Сброс тревог — импульс: у части прошивок катушка сама не снимается."""
    coil = ca.alarm_reset_coil(family)
    err = write_coil(send, slave, coil, True, 800)
    if err:
        raise RuntimeError(f"Сброс тревог (coil {coil}): {err}")
    time.sleep(ca.ALARM_RESET_PULSE_S)
    err = write_coil(send, slave, coil, False, 800)
    if err:
        raise RuntimeError(f"Снятие импульса сброса (coil {coil}): {err}")


def carel_write(
    device_path: str,
    device: Dict[str, Any],
    action: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Выполнить команду окна установки Carel и вернуть свежий снимок."""
    act = str(action or "").strip().lower()
    if act not in CAREL_ACTIONS:
        raise ValueError("Неизвестная команда установки Carel")
    if not _device_is_carel(device):
        raise ValueError("Команда доступна только для приточной установки Carel")
    p = dict(params) if isinstance(params, dict) else {}
    ca = _carel_map()
    family = _carel_identity(device)["family"]

    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        if act == "alarm_reset":
            _carel_alarm_reset(ca, send, slave, family)
        else:
            net_enable: Optional[int] = None
            if family == ca.FAMILY_UARIA and act in ("start", "stop"):
                state = _carel_coil_state(send, slave, ca.COIL_UARIA_NET_ENABLE)
                net_enable = None if state is None else (1 if state else 0)
            _carel_run_writes(
                ca, send, slave, family,
                _carel_write_plan(ca, family, act, p, net_enable=net_enable),
            )
        payload = _carel_payload(send, slave, device)
    finally:
        close_transport()
    payload["action"] = act
    return payload


# ── светодиодная лента MR-02m (RGBW_WS2812, тип 120) ───────────────────────
# The action list is closed: the window sends a NAME, and the register plan is
# assembled here from the shared map. Single-register and single-coil writes are
# refused for the strip entirely — half its settings live behind the reg-410
# lock, and a plain write to a locked device is REJECTED while the wire looks
# fine (PlayCtrl 416 is inside that block, so a "Stop" written that way leaves
# the strip playing).
LED_ACTIONS: Tuple[str, ...] = (
    "pwm",
    "di",
    "strip",
    "scene",
    "text",
    "clock",
    "weather",
    "spy",
    "play",
    "stop",
    "refresh",
    "load_flash",
)

_LED_SINGLE_WRITE_REFUSED = (
    "Светодиодная лента управляется командами окна, а не записью одиночного "
    "регистра или катушки: блок настроек 400–419 пишется только под снятой "
    "блокировкой рег. 410"
)

# One step of an action's plan.
#   LED_OP_REGS  — a {register: value} batch handed to rgbw_lock_bracket_writes(),
#                  which decides ITSELF whether the unlock/lock bracket is needed.
#   LED_OP_BLOCK — an FC16 block write; the marquee window is the only one.
LED_OP_REGS = "regs"
LED_OP_BLOCK = "block"

# Wiring checkboxes of the matrix layout (reg 418, bits 0-3). The request carries
# the state of the four boxes; a box the request omits reads as unchecked, which
# is exactly what an unchecked box sends.
_LED_LAYOUT_FLAG_KEYS = ("progressive", "origin_bottom", "mirror_x", "swap_xy")


def _led_map() -> Any:
    """The shared LED map through the package seam (module_profiles.led_mb2ws())."""
    lm = module_profiles.led_mb2ws()
    if lm is None:
        raise ValueError("Пакет карты светодиодной ленты не установлен на этом устройстве")
    return lm


def _device_is_led(device: Dict[str, Any]) -> bool:
    """Recognised from the scan row: signature first, Input reg 0 second — the
    order and its reason live in module_profiles.scan_type_code()."""
    return (
        module_profiles.scan_type_code(
            str(device.get("signature") or ""), _device_type_code_hint(device)
        )
        == module_profiles.RGBW_WS2812
    )


def _led_number(raw: Any, field: str) -> float:
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("Недопустимое значение поля «%s» команды ленты" % field) from None


def _led_int(
    params: Dict[str, Any],
    key: str,
    lo: int,
    hi: int,
    *,
    default: Optional[int] = None,
    required: bool = False,
) -> Optional[int]:
    """A request field as a bounded int. Absent/blank → ``default`` (or a refusal).

    Values are CLAMPED, never wrapped: every caller has already chosen the target
    register from the map, so a clamp cannot move a write to another address —
    while a silently masked 0x1FFFF could hand firmware a value it answers with
    exception 3.
    """
    raw = params.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ValueError("Поле «%s» команды ленты обязательно" % key)
        return default
    return max(int(lo), min(int(hi), int(_led_number(raw, key))))


def _led_opt_float(
    params: Dict[str, Any], key: str, lo: float, hi: float
) -> Optional[float]:
    raw = params.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return max(float(lo), min(float(hi), _led_number(raw, key)))


def _led_enum(params: Dict[str, Any], key: str, allowed: Sequence[Any], default: Any) -> Any:
    """An enum field. An unlisted value is REFUSED, never folded to the default —
    a fold would write a mode the user did not pick and report success."""
    raw = params.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    value = str(raw).strip() if isinstance(allowed[0], str) else int(_led_number(raw, key))
    if value not in allowed:
        raise ValueError("Недопустимое значение поля «%s» команды ленты" % key)
    return value


def _led_color(lm: Any, container: Dict[str, Any], key: str) -> int:
    """'#RRGGBB' or a raw RGB565 word → the u16 the colour register takes."""
    raw = container.get(key)
    if isinstance(raw, str) and raw.strip().startswith("#"):
        return int(lm.rgbw_hex_to_rgb565(raw.strip())) & 0xFFFF
    value = _led_int(container, key, 0, 0xFFFF, required=True)
    return int(value or 0)


def _led_op_regs(regs: Dict[int, int]) -> Tuple[str, Any]:
    return (LED_OP_REGS, {int(r): int(v) & 0xFFFF for r, v in regs.items()})


def _led_op_block(addr: int, words: Sequence[int]) -> Tuple[str, Any]:
    return (LED_OP_BLOCK, (int(addr), [int(w) & 0xFFFF for w in words]))


def _led_layout_wiring(lm: Any, params: Dict[str, Any]) -> Optional[int]:
    """The four wiring bits of reg 418, or None when the request names none."""
    if not any(k in params for k in _LED_LAYOUT_FLAG_KEYS):
        return None
    masks = (
        lm.MB2WS_MATRIX_LAYOUT_PROGRESSIVE,
        lm.MB2WS_MATRIX_LAYOUT_ORIGIN_BOTTOM,
        lm.MB2WS_MATRIX_LAYOUT_MIRROR_X,
        lm.MB2WS_MATRIX_LAYOUT_SWAP_XY,
    )
    wiring = 0
    for key, mask in zip(_LED_LAYOUT_FLAG_KEYS, masks):
        if params.get(key):
            wiring |= int(mask)
    return wiring


def _led_strip_writes(
    lm: Any, params: Dict[str, Any], layout_prior: Optional[int]
) -> Dict[int, int]:
    """«Адресная лента» → one settings batch (the executor brackets it).

    Every value comes out of the map's own builders: the line mode through
    ``rgbw_line_mode_to_regs`` (which owns the pixel-pool clamps), the geometry
    through ``rgbw_matrix_type_writes`` (which REFUSES rather than clamps when the
    pixels do not fit), and reg 418 through ``rgbw_matrix_layout_write_value_ex``,
    which returns None — meaning *do not write it at all* — when the prior value
    is unknown and the request does not supply every tiling field. Writing 418
    anyway would compose the missing fields from nothing and silently zero the
    user's tiling.
    """
    line = _led_enum(
        params, "line", [k for k, _lab in lm.rgbw_line_ui_choices()], lm.RGBW_LINE_UI_SINGLE
    )
    led_type = _led_enum(
        params, "led_type", [c for c, _lab in lm.rgbw_led_type_choices()], lm.MB2WS_LED_TYPE_WS2812_0
    )
    writes = dict(
        lm.rgbw_line_mode_to_regs(
            line,
            led_type,
            _led_int(params, "led_count0", 0, 0xFFFF, default=0),
            _led_int(params, "led_count1", 0, 0xFFFF, default=0),
            _led_int(params, "pixel_format", 0, 1, default=0),
            byte_order=_led_int(params, "byte_order", 0, 0xFFFF),
            auto_refresh=_led_int(params, "auto_refresh", 0, 1),
        )
    )
    gamma = _led_int(params, "gamma", 0, 255)
    if gamma is not None:
        writes[lm.MB2WS_GAMMA] = gamma
    ch2 = _led_int(params, "ch2_mode", lm.MB2WS_CH2_OFF, lm.MB2WS_CH2_CONTINUATION)
    if ch2 is not None:
        writes[lm.MB2WS_CH2_MODE] = ch2

    tile_count = _led_int(params, "tile_count", 0, lm.MB2WS_MATRIX_LAYOUT_TILECOUNT_MAX)
    tile_mode = _led_int(params, "tile_mode", 0, lm.MB2WS_MATRIX_LAYOUT_TILEMODE_MAX)
    width = _led_int(params, "matrix_width", 1, 64)
    height = _led_int(params, "matrix_height", 0, 255)
    if width is not None and height is not None:
        geometry = lm.rgbw_matrix_type_writes(
            width,
            height,
            tile_count or 0,
            pixel_format=writes.get(lm.MB2WS_PIXEL_FORMAT, 0),
            led_count1=writes.get(lm.MB2WS_LED_COUNT1, 0),
            ui_mode=line,
        )
        if geometry is None:
            raise ValueError(
                "Выбранная геометрия матрицы не помещается в пул пикселей ленты"
            )
        writes.update(geometry)

    wiring = _led_layout_wiring(lm, params)
    touches_layout = wiring is not None or tile_mode is not None or tile_count is not None
    if wiring is None and layout_prior is not None:
        wiring = int(layout_prior) & lm.MB2WS_MATRIX_LAYOUT_WIRING_MASK
    if touches_layout and wiring is not None:
        layout = lm.rgbw_matrix_layout_write_value_ex(
            layout_prior, wiring, tile_mode, tile_count
        )
        if layout is not None:
            writes[lm.MB2WS_MATRIX_LAYOUT] = layout
    return writes


def _led_scene_writes(lm: Any, params: Dict[str, Any], *, play: bool) -> Dict[int, int]:
    """«Сцена» → one settings batch.

    Scale (460) is never written: firmware stores it and no render path reads it
    (``rgbw_scale_is_honoured()`` is False), so the window does not offer it and
    the plan must not smuggle it in.

    PlayCtrl rides the batch only for flash playback — the desktop's behaviour and
    the reason for it: firmware starts a built-in effect when the scene registers
    land, so a bare PlayCtrl=1 was a no-op for every mode except flash.
    """
    source = _led_enum(
        params, "source", [k for k, _lab in lm.rgbw_scene_ui_choices()], lm.RGBW_SCENE_UI_POOL
    )
    writes = dict(
        lm.rgbw_scene_mode_to_regs(
            source,
            fx_id=_led_int(params, "fx_id", 0, lm.RGBW_FX_MODE_COUNT - 1, default=0),
            fx_speed=_led_int(params, "fx_speed", 0, 255, default=128),
            fx_param=_led_int(params, "fx_param", 0, 255, default=255),
            flash_slot=_led_int(params, "flash_slot", 0, 31, default=0),
            loop=bool(params.get("loop")),
            scale=None,
        )
    )
    if play and source == lm.RGBW_SCENE_UI_FLASH:
        writes[lm.MB2WS_PLAY_CTRL] = lm.MB2WS_PLAY_PLAY
    return writes


def _led_scene_plain_writes(lm: Any, params: Dict[str, Any]) -> Dict[int, int]:
    """FxAux (455) and FxDensity (456) from a scene request — the per-effect
    adjustments, packed for the effect the REQUEST selects.

    ``fx_aux`` is ``{low, flag, high}`` as the window's widgets stand; the map's
    composer clamps every field to what firmware accepts for this mode and
    forces the high byte to 0 where the mode ignores it — so a colour combo can
    never leak into Escort's pool length. Absent keys write nothing.
    """
    out: Dict[int, int] = {}
    fx_id = _led_int(params, "fx_id", 0, lm.RGBW_FX_MODE_COUNT - 1, default=0)
    aux = params.get("fx_aux")
    if aux is not None:
        if not isinstance(aux, dict):
            raise ValueError("Поле «fx_aux» команды ленты задаётся объектом")
        out[lm.MB2WS_FX_AUX] = lm.rgbw_fx_aux_compose(
            fx_id,
            low=_led_int(aux, "low", 0, 255, default=0),
            flag=bool(aux.get("flag")),
            high=_led_int(aux, "high", 0, 255, default=0),
        )
    density = _led_int(params, "fx_density", 0, 255)
    if density is not None:
        out[lm.MB2WS_FX_DENSITY] = density
    return out


def _led_text_color_writes(lm: Any, colors: Any) -> Dict[int, int]:
    if not isinstance(colors, dict):
        raise ValueError("Цвета бегущей строки задаются объектом")
    homes = (
        ("color1", lm.MB2WS_TEXT_COLOR1),
        ("color2", lm.MB2WS_TEXT_COLOR2),
        ("bg1", lm.MB2WS_TEXT_BG1),
        ("bg2", lm.MB2WS_TEXT_BG2),
    )
    out: Dict[int, int] = {}
    for key, reg in homes:
        if key in colors:
            out[reg] = _led_color(lm, colors, key)
    if not out:
        raise ValueError("Цвета бегущей строки: не задано ни одного известного поля")
    return out


def _led_spy_writes(lm: Any, params: Dict[str, Any]) -> Dict[int, int]:
    """«Индикатор линии» (эффект 81) → the Spy/Master block 640..695.

    Every address is asked of the map (``rgbw_spy_slot_reg`` / ``rgbw_spy_lim_reg``
    / ``rgbw_wx_spy_reg``); the codes are enums checked against the map's own
    choice lists, so no request value reaches a register unvalidated.
    """
    mode = _led_int(params, "mode", lm.MB2WS_SPY_MODE_OFF, lm.MB2WS_SPY_MODE_MASTER, default=lm.MB2WS_SPY_MODE_OFF)
    if params.get("tap"):
        mode = int(mode) | lm.MB2WS_SPY_TAP_BIT
    stopbits = 2 if _led_int(params, "stopbits", 1, 2, default=1) == 2 else 1
    writes: Dict[int, int] = {
        lm.MB2WS_SPY_WORK_PORT: _led_int(params, "port", 0, 2, default=0),
        lm.MB2WS_SPY_WORK_MODE: mode,
        lm.MB2WS_SPY_BAUD: _led_enum(
            params, "baud", [c for c, _lab in lm.rgbw_spy_baud_choices()],
            lm.rgbw_spy_baud_choices()[0][0],
        ),
        lm.MB2WS_SPY_LINE: _led_int(params, "parity", 0, 2, default=0) | (stopbits << 8),
        lm.MB2WS_SPY_POLL_MS: _led_int(params, "poll_ms", 10, 60000, default=1000),
        lm.MB2WS_SPY_TIMEOUT_MS: _led_int(params, "timeout_ms", 10, 10000, default=1000),
        lm.MB2WS_SPY_STALE_MS: _led_int(params, "stale_ms", 100, 60000, default=10000),
    }
    fc_codes = [c for c, _lab in lm.rgbw_spy_fc_choices()]
    type_codes = [c for c, _lab in lm.rgbw_spy_type_choices()]
    slots = params.get("slots") or []
    if not isinstance(slots, (list, tuple)):
        raise ValueError("Слоты индикатора линии задаются списком")
    for n in range(min(lm.MB2WS_SPY_SLOT_COUNT, len(slots))):
        slot = slots[n]
        if not isinstance(slot, dict):
            raise ValueError("Слот индикатора линии задаётся объектом")
        unit01, unit23 = lm.rgbw_spy_pack_unit(str(slot.get("unit") or ""))
        base = lm.rgbw_spy_slot_reg(n, 0)
        writes[base + 0] = _led_int(slot, "uid", 0, 247, default=0)
        writes[base + 1] = _led_enum(slot, "fc", fc_codes, lm.MB2WS_SPY_FC_HOLDING)
        writes[base + 2] = _led_int(slot, "reg", 0, 0xFFFF, default=0)
        writes[base + 3] = _led_enum(slot, "type", type_codes, lm.MB2WS_SPY_TYPE_INT16)
        writes[base + 4] = _led_int(slot, "decimals", 0, 3, default=0)
        writes[base + 5] = unit01
        writes[base + 6] = unit23
        writes[base + 7] = 0
        writes[lm.rgbw_spy_lim_reg(n, 0)] = lm.rgbw_i16_to_u16(
            _led_int(slot, "lo", -32768, 32767, default=0)
        )
        writes[lm.rgbw_spy_lim_reg(n, 1)] = lm.rgbw_i16_to_u16(
            _led_int(slot, "hi", -32768, 32767, default=0)
        )
    weather = params.get("weather") or []
    if not isinstance(weather, (list, tuple)):
        raise ValueError("Поля погоды индикатора линии задаются списком")
    for n in range(min(lm.MB2WS_WX_SPY_COUNT, len(weather))):
        field = weather[n]
        if not isinstance(field, dict):
            raise ValueError("Поле погоды индикатора линии задаётся объектом")
        writes[lm.rgbw_wx_spy_reg(n, 0)] = lm.rgbw_wx_spy_pack_uid_fc(
            _led_int(field, "uid", 0, 247, default=0),
            _led_enum(field, "fc", fc_codes, lm.MB2WS_SPY_FC_HOLDING),
        )
        writes[lm.rgbw_wx_spy_reg(n, 1)] = _led_int(field, "reg", 0, 0xFFFF, default=0)
    return writes


def _led_weather_writes(lm: Any, params: Dict[str, Any]) -> Dict[int, int]:
    """The weather block. An omitted field writes its firmware SENTINEL, not 0 —
    0 is a real reading (0 °C, the 1st of month 0) and would render as one.

    Scale (460) sits between humidity (459) and pressure (461) and is deliberately
    not part of the batch: these are single-register writes, so the gap costs
    nothing and 460 is not a weather field.
    """
    day = _led_int(params, "day", 1, 31)
    month = _led_int(params, "month", 1, 12)
    temp = _led_opt_float(params, "temp_c", -3276.7, 3276.7)
    humidity = _led_opt_float(params, "humidity_pct", 0.0, 100.0)
    pressure = _led_opt_float(params, "pressure_mmhg", 400.0, 850.0)
    year = _led_int(params, "year", lm.MB2WS_WX_YEAR_MIN, lm.MB2WS_WX_YEAR_MAX)
    return {
        lm.MB2WS_WX_DATE: (
            lm.rgbw_wx_date_pack(day, month)
            if (day and month)
            else lm.MB2WS_WX_DATE_UNSET
        ),
        lm.MB2WS_WX_TEMP: (
            lm.rgbw_i16_to_u16(int(round(temp * 10.0)))
            if temp is not None
            else lm.MB2WS_WX_TEMP_UNSET
        ),
        lm.MB2WS_WX_HUM: (
            int(round(humidity * 10.0)) if humidity is not None else lm.MB2WS_WX_HUM_UNSET
        ),
        lm.MB2WS_WX_PRESS: (
            int(round(pressure * 10.0)) if pressure is not None else lm.MB2WS_WX_PRESS_UNSET
        ),
        lm.MB2WS_WX_YEAR: year if year is not None else lm.MB2WS_WX_YEAR_UNSET,
    }


def _led_write_plan(
    lm: Any,
    action: str,
    params: Dict[str, Any],
    *,
    layout_prior: Optional[int] = None,
    device_fx_id: Optional[int] = None,
) -> List[Tuple[str, Any]]:
    """Action name → the ordered plan. Only the action decides the plan's shape;
    whether a step needs the reg-410 bracket is the executor's question and it
    asks the map, never a list here."""
    if action == "pwm":
        ops: List[Tuple[str, Any]] = []
        if "mode" in params:
            ops.append(
                _led_op_regs(
                    {
                        lm.RGBW_PWM_STRIP_MODE_HOLDING: _led_enum(
                            params, "mode", [c for c, _lab in lm.rgbw_pwm_mode_choices()],
                            lm.RGBW_PWM_MODE_RGBW,
                        )
                    }
                )
            )
        if "channel" in params or "value" in params:
            idx = _led_int(params, "channel", 0, lm.RGBW_PWM_CHANNELS - 1, required=True)
            level = _led_int(params, "value", 0, lm.RGBW_PWM_PERMILLE_MAX, required=True)
            # Level first, then the mirror register that enables the channel —
            # the desktop's order, so the channel never lights at the old level.
            ops.append(_led_op_regs({lm.RGBW_PWM_HOLDING_BASE + int(idx): int(level)}))
            ops.append(_led_op_regs({lm.RGBW_PWM_MIRROR_BASE + int(idx): int(level)}))
        if "color" in params:
            # The colour picker (RGB + W mode): one '#RRGGBB' → the R/G/B permille
            # triple, each channel level-then-mirror as above, in one session.
            # A malformed value is REFUSED (None from the map), never black.
            triple = lm.rgbw_hex_to_pwm_permille(str(params.get("color") or ""))
            if triple is None:
                raise ValueError("Цвет каналов RGB задаётся строкой вида #RRGGBB")
            for ch, level in enumerate(triple):
                ops.append(_led_op_regs({lm.RGBW_PWM_HOLDING_BASE + ch: int(level)}))
                ops.append(_led_op_regs({lm.RGBW_PWM_MIRROR_BASE + ch: int(level)}))
        if not ops:
            raise ValueError("Команда «RGBW каналы» без параметров")
        return ops

    if action == "di":
        idx = _led_int(params, "channel", 0, lm.RGBW_DI_COUNT - 1, required=True)
        ops = []
        if "mode" in params:
            ops.append(
                _led_op_regs(
                    {lm.RGBW_DI_MODE_BASE + int(idx): 1 if _led_int(params, "mode", 0, 1, required=True) else 0}
                )
            )
        if "debounce_ms" in params:
            ops.append(
                _led_op_regs(
                    {lm.RGBW_DI_DEBOUNCE_BASE + int(idx): _led_int(params, "debounce_ms", 10, 2000, required=True)}
                )
            )
        if not ops:
            raise ValueError("Команда «Входы DI» без параметров")
        return ops

    if action == "strip":
        return [_led_op_regs(_led_strip_writes(lm, params, layout_prior))]

    if action in ("scene", "play"):
        ops = [_led_op_regs(_led_scene_writes(lm, params, play=action == "play"))]
        plain = _led_scene_plain_writes(lm, params)
        if plain:
            # A SECOND op, not merged into the batch above: 455/456 are not
            # lock-gated, and folding them into the bracketed batch would put
            # two plain registers under an open lock on a shared line.
            ops.append(_led_op_regs(plain))
        return ops

    if action == "text":
        ops = []
        if "text" in params:
            raw = params.get("text")
            if not isinstance(raw, str):
                raise ValueError("Текст бегущей строки задаётся строкой")
            if not lm.rgbw_text_within_limit(raw):
                raise ValueError(
                    "Текст бегущей строки длиннее %d символов" % lm.MB2WS_TEXT_MAX_CHARS
                )
            ops.append(_led_op_block(lm.MB2WS_TEXT_BASE, lm.rgbw_pack_text_cp1251(raw)))
        if "lines" in params:
            ops.append(
                _led_op_regs(
                    {
                        lm.MB2WS_TEXT_LINES: _led_enum(
                            params, "lines", [c for c, _key in lm.rgbw_text_lines_choices()],
                            lm.MB2WS_TEXT_LINES_SINGLE,
                        )
                    }
                )
            )
        if params.get("colors") is not None:
            ops.append(_led_op_regs(_led_text_color_writes(lm, params.get("colors"))))
        if not ops:
            raise ValueError("Команда «Бегущая строка» без параметров")
        return ops

    if action == "clock":
        if params.get("unset"):
            return [_led_op_regs(dict(lm.rgbw_tod_unset_writes()))]
        if params.get("from_pc"):
            now = datetime.now()
            writes = dict(lm.rgbw_pc_clock_writes(now))
            # The weather renderer composes "HH:MM DD.MM.YYYY" from both blocks,
            # so seeding the time without the date leaves half the panel unset.
            # Which effect is running is asked of the DEVICE, not of the request.
            if device_fx_id is not None and lm.rgbw_fx_uses_weather(device_fx_id):
                writes.update(lm.rgbw_pc_date_writes(now))
            return [_led_op_regs(writes)]
        hours = _led_int(params, "hours", 0, 23)
        minutes = _led_int(params, "minutes", 0, 59)
        if hours is None or minutes is None:
            # Firmware ticks only while BOTH registers are set, and 0 is a real
            # time — a half-filled request must not seed midnight.
            raise ValueError("Часы и минуты задаются вместе — иначе часы ленты не пойдут")
        return [_led_op_regs({lm.MB2WS_TOD_HOURS: hours, lm.MB2WS_TOD_MINUTES: minutes})]

    if action == "weather":
        ops = [_led_op_regs(_led_weather_writes(lm, params))]
        if "temp_color" in params:
            ops.append(_led_op_regs({lm.MB2WS_WX_TEMP_COLOR: _led_color(lm, params, "temp_color")}))
        return ops

    if action == "spy":
        return [_led_op_regs(_led_spy_writes(lm, params))]

    if action == "stop":
        # Order is load-bearing (stop → pool → clear → latch), so each register
        # is its own step. The `lock_protected` column of the map's plan is not
        # read here: the executor asks rgbw_reg_is_lock_gated() through
        # rgbw_lock_bracket_writes(), which is where that column comes from too.
        return [
            _led_op_regs({int(reg): int(value)})
            for reg, value, _locked in lm.rgbw_stop_blank_writes()
        ]

    if action == "refresh":
        return [_led_op_regs({lm.MB2WS_CMD: lm.MB2WS_CMD_REFRESH})]

    if action == "load_flash":
        ops = []
        slot = _led_int(params, "slot", 0, 31)
        if slot is not None:
            ops.append(_led_op_regs({lm.MB2WS_FLASH_SLOT: slot}))
        ops.append(_led_op_regs({lm.MB2WS_CMD: lm.MB2WS_CMD_LOAD_FLASH}))
        return ops

    raise ValueError("Неизвестная команда светодиодной ленты")


def _led_run_plan(lm: Any, send, slave: int, plan: Sequence[Tuple[str, Any]]) -> None:
    """Execute the plan. Two defence-in-depth guards live here rather than in the
    plan builder, so they hold for any future action too:

      * the ONLY block write is the marquee window at 516. The pre-1.0.2.2 base
        495 overlapped the family safe-state AO block 503..506, so writing text
        there silently drove the module's PWM safe values to 100 %.
      * CMD 3 (save-to-flash) never reaches the wire. Firmware ACKS it and stores
        nothing, so it looks successful and does nothing at all.
    """
    for kind, payload in plan:
        if kind == LED_OP_BLOCK:
            addr, words = payload
            if int(addr) != lm.MB2WS_TEXT_BASE:
                raise ValueError(
                    "Блочная запись ленты разрешена только в окно бегущей строки "
                    "(рег. %d)" % lm.MB2WS_TEXT_BASE
                )
            err = write_multiple(send, slave, int(addr), list(words), 1000)
            if err:
                raise RuntimeError(f"Запись бегущей строки (рег. {addr}): {err}")
            continue
        regs: Dict[int, int] = dict(payload)
        for reg, value in regs.items():
            if int(reg) == lm.MB2WS_CMD and not lm.rgbw_cmd_is_allowed(int(value)):
                raise ValueError(
                    "Команда %d ленте не отправляется — прошивка подтверждает её "
                    "на шине и не выполняет" % int(value)
                )
        bracketed = any(lm.rgbw_reg_is_lock_gated(int(r)) for r in regs)
        for reg, value in lm.rgbw_lock_bracket_writes(regs):
            err = write_single(send, slave, int(reg), int(value) & 0xFFFF, 800)
            if err:
                if bracketed:
                    # Leave the device locked as we found it; the desktop does the
                    # same, and an aborted batch must not leave 400..419 open.
                    write_single(send, slave, lm.MB2WS_LOCK, 0, 800)
                raise RuntimeError(f"Запись рег. {reg}: {err}")


def led_write(
    device_path: str,
    device: Dict[str, Any],
    action: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    active_tab: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one LED-window command and return a fresh snapshot."""
    act = str(action or "").strip().lower()
    if act not in LED_ACTIONS:
        raise ValueError("Неизвестная команда светодиодной ленты")
    if not _device_is_led(device):
        raise ValueError("Команда доступна только для светодиодной ленты")
    p = dict(params) if isinstance(params, dict) else {}
    lm = _led_map()
    # Refuse a malformed request BEFORE the line is opened — a bad field must not
    # cost a transport on a bus shared with MPLC4 and the MQTT bridge. The plan is
    # pure, so building it twice costs nothing, and the two device-read inputs are
    # absent on purpose: each can only ADD a register to the plan, never turn a
    # valid request into an invalid one.
    _led_write_plan(lm, act, p)

    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        # Two reads the plan needs FROM THE DEVICE, not from the request: reg 418
        # is read-modify-write (tiling is preserved, never composed from nothing),
        # and the PC-clock seed must know which effect is running.
        layout_prior: Optional[int] = None
        if act == "strip":
            layout_prior = _read_u16(send, slave, lm.MB2WS_MATRIX_LAYOUT, 700)
        device_fx_id: Optional[int] = None
        if act == "clock" and p.get("from_pc"):
            raw_fx = _read_u16(send, slave, lm.MB2WS_FX_ID, 700)
            if raw_fx is not None:
                device_fx_id = lm.rgbw_resolve_fx_id(int(raw_fx))
        _led_run_plan(
            lm,
            send,
            slave,
            _led_write_plan(lm, act, p, layout_prior=layout_prior, device_fx_id=device_fx_id),
        )
    finally:
        close_transport()
    payload = snapshot_for_device(
        device_path, device, snapshot_detail="full", active_tab=active_tab
    )
    payload["action"] = act
    return payload


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
        if _device_is_carel(device):
            # Ни живой личности, ни сетевого блока: ПЛК не отвечает ни на один из
            # этих адресов, и восемь таймаутов не помещаются в такт окна.
            payload = _carel_payload(send, slave, device, io_hw=active_tab == CAREL_IO_TAB)
            payload["snapshot_detail"] = "full"
            payload["active_tab"] = active_tab
            return payload
        identity = _read_live_identity(send, slave, device)
        kind, identity = _resolve_kind(identity, device)

        network = _read_network(send, slave, device)
        family = family_from_kind(kind)
        payload: Dict[str, Any] = {
            "kind": kind,
            "family": family,
            "bus_mode_supported": bus_mode.selector_supported(family),
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
        elif kind == LED_KIND:
            # The strip IS one of ours: it answers the identity and network block
            # above, so only the LED-specific reads are added here. Their cost is
            # dialled by `active_tab`, not by `snapshot_detail` — see led_poll.
            # The static choice lists ride every reply but the 1 s `panel` poll:
            # the window caches them from the first full snapshot.
            payload["led"] = led_poll.read_led_snapshot(
                send, slave, active_tab=active_tab, include_choices=detail != "panel"
            )
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
    if _device_is_carel(device):
        # Снимок отдаёт network.writable=false; здесь та же правда механически:
        # у ПЛК нет регистров 110–112/122/128, запись ушла бы в чужие адреса.
        raise ValueError("Параметры линии установки Carel меняются только с панели ПЛК")

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
    if _device_is_carel(device):
        raise ValueError(_CAREL_SINGLE_WRITE_REFUSED)
    if _device_is_led(device):
        raise ValueError(_LED_SINGLE_WRITE_REFUSED)
    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        identity = _read_live_identity(send, slave, device)
        kind, identity = _resolve_kind(identity, device)
        if kind == CAREL_KIND:
            raise ValueError(_CAREL_SINGLE_WRITE_REFUSED)
        if kind == LED_KIND:
            raise ValueError(_LED_SINGLE_WRITE_REFUSED)
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


def write_bus_mode(
    device_path: str,
    device: Dict[str, Any],
    mode: int,
) -> Dict[str, Any]:
    """Записать 3-state поле полевой шины (рег.122) для MR/DTV (§5.1).

    Значение валидируется в {0,1,2}; семейство должно поддерживать селектор
    (CE/WB/bootloader — отказ). Возврат: результат с целевым режимом и, для
    live-переходов 0/1, свежий снимок. Для BACnet(2) устройство уходит в
    deferred-reboot — свежий снимок не читается (§5.3-проверку делает верхний
    слой после окна применения через /bacnet/verify)."""
    if not bus_mode.bus_mode_valid(mode):
        raise ValueError("Режим шины: только 0 (классический), 1 (Fast) или 2 (BACnet)")
    target = int(mode) & 0xFFFF
    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        identity = _read_live_identity(send, slave, device)
        kind, identity = _resolve_kind(identity, device)
        family = family_from_kind(kind)
        if not bus_mode.selector_supported(family):
            raise ValueError("Полевая шина недоступна для этого семейства устройств")
        prior_raw = _read_u16(send, slave, REG_FAST_MODBUS, 800)
        err = write_single(send, slave, REG_FAST_MODBUS, target, 800)
        if err:
            raise RuntimeError(f"Запись рег. 122: {err}")
    finally:
        close_transport()
    result: Dict[str, Any] = {
        "kind": kind,
        "family": family,
        "requested_mode": target,
        "prior_mode": bus_mode.sanitize(prior_raw),
        "reboot_pending": target == bus_mode.BUS_BACNET,
    }
    if target != bus_mode.BUS_BACNET:
        # Live switch (classic ↔ Fast) — no reboot; a fresh snapshot is safe.
        result["snapshot"] = snapshot_for_device(device_path, device, snapshot_detail="full")
    return result


def probe_bus_mode(device_path: str, device: Dict[str, Any]) -> Dict[str, Any]:
    """Прочитать рег.122 и сказать, отвечает ли устройство по Modbus (§5.3).

    Используется верхним слоем после окна применения BACnet: если устройство ещё
    отвечает по Modbus и рег.122 == 2 — прошивка без поддержки BACnet или
    переключение не применилось (inert-honesty). Тишина ⇒ answered=False."""
    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        raw = _read_u16(send, slave, REG_FAST_MODBUS, 800)
    finally:
        close_transport()
    answered = raw is not None
    return {
        "answered": answered,
        "bus_mode_raw": (int(raw) & 0xFFFF) if answered else None,
        "bus_mode": bus_mode.sanitize(raw) if answered else None,
        "verdict": bus_mode.bacnet_switch_verdict(answered, raw),
    }


def write_allowed_coil(
    device_path: str,
    device: Dict[str, Any],
    coil: int,
    on: bool,
) -> Dict[str, Any]:
    if _device_is_carel(device):
        raise ValueError(_CAREL_SINGLE_WRITE_REFUSED)
    if _device_is_led(device):
        raise ValueError(_LED_SINGLE_WRITE_REFUSED)
    send, close_transport = _open_transport(device_path, device)
    try:
        slave = _device_slave(device)
        identity = _read_live_identity(send, slave, device)
        kind, identity = _resolve_kind(identity, device)
        if kind == CAREL_KIND:
            raise ValueError(_CAREL_SINGLE_WRITE_REFUSED)
        if kind == LED_KIND:
            raise ValueError(_LED_SINGLE_WRITE_REFUSED)
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

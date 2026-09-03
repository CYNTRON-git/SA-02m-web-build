# -*- coding: utf-8 -*-
"""Чтение состояния приточной установки Carel (c.pCOmini / uAria) по Modbus.

Порт настольного флешера (MR-02m-flasher, ветка `carel`,
flasher_windows/carel_poll.py) с тремя осознанными отличиями:

  * часы ПЛК (IR111–115 / HR430–434 + coil 124) не читаются и не пишутся: окно
    настройки в вебе их не показывает, а это пять лишних транзакций внутри
    односекундного опроса;
  * `tr(...)` заменён кодом строки и её русским текстом — переводит словарь
    страницы, а не демон;
  * карта регистров здесь не повторяется ни одним числом: она приходит из общего
    пакета через единственный шов пакета — ``module_profiles.carel_ahu()``.

Никаких CYNTRON-регистров: ПЛК не отвечает ни на 290, ни на 110–112, ни на 122.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .modbus_io import (
    SendRtuFn,
    coil_bits_from_payload,
    parse_regs_be_u16,
    read_coils,
    read_discrete_inputs,
    read_holding,
    read_input_regs,
)
from . import module_profiles

# Тексты Sv_DiStat, которых нет в общей карте uAria: у короткой карты нет пакетов
# DiStat, состояние собирается из трёх дискретных входов.
_UARIA_DISTAT_TEXT = {
    "heat": "ТЭН",
    "fan": "Приточный вентилятор",
    "crit": "Критическая тревога",
}


def _ca() -> Any:
    """Общая карта Carel через шов пакета; без неё чтение невозможно."""
    ca = module_profiles.carel_ahu()
    if ca is None:
        raise RuntimeError(
            "Пакет sa02m_carel не установлен (/opt/sa02m-carel) — "
            "карта регистров Carel недоступна"
        )
    return ca


def _s16(raw: int) -> int:
    v = int(raw) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _ir_u16(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int = 800,
) -> Optional[List[int]]:
    payload, err = read_input_regs(send, slave, start, count, timeout_ms)
    if err or not payload:
        return None
    return parse_regs_be_u16(payload)


def _coil_bits(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int,
    *,
    discrete: bool = False,
) -> Optional[List[bool]]:
    reader = read_discrete_inputs if discrete else read_coils
    payload, err = reader(send, slave, start, count, timeout_ms)
    if err or not payload:
        return None
    return coil_bits_from_payload(payload, count)


def read_carel_snapshot(
    send: SendRtuFn,
    slave: int,
    fam: str,
    *,
    compact: bool = False,
    io_hw: bool = False,
    variant: str = "",
) -> Optional[Dict[str, Any]]:
    """Снимок живых значений установки.

    *compact* — короткий набор для фонового опроса (без тревог DiStat и уставок).
    *io_hw* — дочитать блок «входы/выходы» (дорогой: отдельные блоки IR и DI).
    *variant* — исполнение платы c.pCOmini («B»/«E»/«H») из метки FC17: у Basic
    нет DI1/DI2 и Y1/Y2.
    """
    ca = _ca()
    out: Dict[str, Any] = {"fam": fam, "variant": variant}
    if fam == ca.FAMILY_UARIA:
        return _read_uaria(send, slave, out, io_hw=io_hw)
    if compact:
        return _read_crst_compact(send, slave, out)
    return _read_crst(send, slave, out, io_hw=io_hw, variant=variant)


def _read_uaria(
    send: SendRtuFn,
    slave: int,
    out: Dict[str, Any],
    *,
    io_hw: bool,
) -> Optional[Dict[str, Any]]:
    """Короткая карта uAria: аналоговые значения — float32 ABCD в двух словах."""
    ca = _ca()
    regs = _ir_u16(send, slave, 0, 36, 800)
    if not regs:
        return None

    def f32(addr: int) -> float:
        if addr + 1 >= len(regs):
            return float("nan")
        return ca.be_float32(regs[addr], regs[addr + 1])

    out["oat"] = f32(ca.IR_UARIA_OAT)
    out["sat"] = f32(ca.IR_UARIA_SAT)
    out["rwt"] = f32(ca.IR_UARIA_RWT)
    out["valve"] = f32(ca.IR_UARIA_VALVE)
    out["fan"] = f32(ca.IR_UARIA_FAN)
    if ca.IR_UARIA_STATUS < len(regs):
        out["unit"] = int(regs[ca.IR_UARIA_STATUS]) & 0xFFFF

    payload, err = read_holding(send, slave, ca.HR_UARIA_SP, 5, 800)
    if not err and payload:
        hr = parse_regs_be_u16(payload)
        if len(hr) >= 2:
            out["sp_w"] = ca.be_float32(hr[0], hr[1])
        if len(hr) >= 4:
            out["sp_s"] = ca.be_float32(hr[2], hr[3])
        if len(hr) >= 5:
            out["season_code"] = int(hr[4]) & 0xFFFF

    payload, err = read_holding(send, slave, ca.HR_UARIA_FAN_MIN, 3, 800)
    if not err and payload:
        fr = parse_regs_be_u16(payload)
        if len(fr) >= 2:
            out["fan_min"] = ca.be_float32(fr[0], fr[1])
        if len(fr) >= 3:
            out["fan_sp"] = int(fr[2]) & 0xFF
            if "fan_min" in out:
                out["fan_calc"] = ca.uaria_fan_step_to_pct(out["fan_sp"], out["fan_min"])

    for key, coil in (
        ("uaria_run", ca.COIL_UARIA_NET_ON_OFF),
        ("gs04", ca.COIL_UARIA_NET_ENABLE),
        ("uaria_local", ca.COIL_UARIA_LOCAL),
    ):
        bits = _coil_bits(send, slave, coil, 1, 700)
        if bits:
            out[key] = bool(bits[0])

    db = _coil_bits(send, slave, 0, 86, 900, discrete=True)
    if db:
        out["pump"] = _bit(db, ca.DI_UARIA_PUMP)
        out["fan_on"] = _bit(db, ca.DI_UARIA_FAN)
        out["heat_on"] = _bit(db, ca.DI_UARIA_HEAT)
        out["crit"] = _bit(db, ca.DI_UARIA_CRIT)

    ab = _coil_bits(send, slave, 101, 57, 900, discrete=True)
    if ab:
        out["alarms"] = ca.decode_uaria_alarm_dis([101 + i for i, bit in enumerate(ab) if bit])
    else:
        out["alarms"] = []

    out["distat"] = [
        {"code": code, "text": _UARIA_DISTAT_TEXT[code]}
        for code, key in (("heat", "heat_on"), ("fan", "fan_on"), ("crit", "crit"))
        if out.get(key)
    ]
    if io_hw:
        out.update(_uaria_io_from_snapshot(out, db))
    return out


def _bit(bits: Sequence[bool], index: int) -> bool:
    return bool(bits[index]) if len(bits) > index else False


def _read_crst_compact(
    send: SendRtuFn,
    slave: int,
    out: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    ca = _ca()
    ir = _ir_u16(send, slave, ca.IR_OAT, 4, 700)
    if not ir or len(ir) < 4:
        return None
    _crst_temps(ca, out, ir)
    unit = _ir_u16(send, slave, ca.IR_UNIT_STATUS, 1, 700)
    if unit:
        out["unit"] = unit[0]
    packs = _ir_u16(send, slave, 301, 10, 800)
    if packs:
        out["alarms"] = ca.decode_alarm_packs(
            {301 + i: packs[i] for i in range(min(10, len(packs)))}
        )
    return out


def _crst_temps(ca: Any, out: Dict[str, Any], ir: Sequence[int]) -> None:
    """IR1..4: наружная / приток / помещение / обратка, int16 ×10."""
    out["oat"] = ca.int16_x10_to_phys(ir[0])
    out["sat"] = ca.int16_x10_to_phys(ir[1])
    out["rwt"] = ca.int16_x10_to_phys(ir[3])


def _read_crst(
    send: SendRtuFn,
    slave: int,
    out: Dict[str, Any],
    *,
    io_hw: bool,
    variant: str,
) -> Optional[Dict[str, Any]]:
    """c.pCOmini часто отвергает FC04 на 56 регистров (в 1..56 дыры) — читаем
    только известные адреса, блоками, которые ПЛК на стенде реально отдаёт."""
    ca = _ca()
    ir = _ir_u16(send, slave, ca.IR_OAT, 4, 700)
    if not ir or len(ir) < 4:
        return None
    _crst_temps(ca, out, ir)

    valve = _ir_u16(send, slave, ca.IR_HEAT_VALVE, 1, 700)
    if valve:
        out["valve"] = float(_s16(valve[0]))
    disp_sp = _ir_u16(send, slave, ca.IR_DISP_SP, 1, 700)
    if disp_sp:
        out["disp_sp"] = ca.int16_x10_to_phys(disp_sp[0])
    unit = _ir_u16(send, slave, ca.IR_UNIT_STATUS, 1, 700)
    if unit:
        out["unit"] = unit[0]

    packs = _ir_u16(send, slave, 301, 17, 900)
    if not packs:
        packs = _ir_u16(send, slave, 301, 10, 800)
    dmap: Dict[int, int] = {}
    if packs:
        amap = {301 + i: packs[i] for i in range(min(10, len(packs)))}
        if len(packs) > 15:
            amap[316] = packs[15]
        if len(packs) > 10:
            for addr in ca.IR_DISTAT_PACKS:
                idx = addr - 301
                if 0 <= idx < len(packs):
                    dmap[addr] = packs[idx]
        out["alarms"] = ca.decode_alarm_packs(amap)
        out["distat"] = ca.decode_distat_packs(dmap)

    payload, err = read_holding(send, slave, ca.HR_SYS_MODE, 6, 800)
    if not err and payload:
        hr = parse_regs_be_u16(payload)
        if len(hr) >= 6:
            out["mode"] = hr[0]
            out["sp_w"] = ca.int16_x10_to_phys(hr[2])
            out["sp_s"] = ca.int16_x10_to_phys(hr[3])
            out["fan_sa"] = ca.int16_x10_to_phys(hr[4])
            out["fan_ea"] = ca.int16_x10_to_phys(hr[5])

    bits = _coil_bits(send, slave, ca.COIL_BMS_OFF_ON, 3, 700)
    if bits:
        out["bms_run"] = bool(bits[0])
        out["season"] = _bit(bits, 2)
    mam = _coil_bits(send, slave, ca.COIL_MA17, 4, 700)
    if mam:
        out["ma17"] = bool(mam[0])
        out["ma18"] = _bit(mam, 1)
        out["ma19"] = _bit(mam, 2)
        out["ma20"] = _bit(mam, 3)

    kb = _coil_bits(send, slave, ca.DI_KEYBOARD, 2, 600, discrete=True)
    if kb:
        out["keyboard_on"] = bool(kb[0])
        out["sys_on"] = _bit(kb, 1)
    pump = _coil_bits(send, slave, ca.DI_PUMP, 1, 600, discrete=True)
    if pump:
        out["pump"] = bool(pump[0])

    if io_hw:
        out.update(_crst_io_hw(send, slave, dmap, variant))
    return out


def _ir_chunks(send: SendRtuFn, slave: int, ranges: Sequence[Sequence[int]]) -> Dict[int, int]:
    """Блоки IR с одним повтором: на смешанной линии отдельный блок изредка бьётся,
    из-за чего целая колонка входов/выходов уходила в прочерки."""
    raw: Dict[int, int] = {}
    for start, count in ranges:
        words = _ir_u16(send, slave, int(start), int(count), 700)
        if not words:
            time.sleep(0.05)
            words = _ir_u16(send, slave, int(start), int(count), 900)
        if not words:
            continue
        for i, w in enumerate(words):
            raw[int(start) + i] = w
    return raw


def _crst_io_hw(
    send: SendRtuFn,
    slave: int,
    dmap: Mapping[int, int],
    variant: str = "",
) -> Dict[str, Any]:
    """Состояния входов/выходов так, как их отдаёт карта BMS — по функциям программы.

    Клеммы (U1–U10, DO1–DO6, DI1/DI2, Y1/Y2) в карте не адресуются: их назначение
    задаёт мастер I/O в ПЛК, поэтому подписывать значения номерами клемм нельзя —
    на другой конфигурации подписи оказались бы чужими.
    """
    ca = _ca()
    del variant  # состав клемм по сети не читается; вариант нужен только вебу
    u_raw = _ir_chunks(send, slave, ((1, 12), (14, 1), (16, 5)))
    ao_raw = _ir_chunks(send, slave, ((21, 1), (23, 2), (27, 1), (29, 9), (44, 2), (47, 1)))
    count = max(addr for addr, _tag, _text in ca.CRST_NO_DI)
    bits: Dict[int, bool] = {}
    no_bits = _coil_bits(send, slave, 1, count, 900, discrete=True)
    if no_bits:
        for i, b in enumerate(no_bits):
            bits[1 + i] = bool(b)
    return {
        "io_di": ca.decode_distat_all(dmap),
        "io_u": ca.analog_io_rows(ca.FAMILY_CRST, u_raw, analog_out=False),
        "io_ao": ca.analog_io_rows(ca.FAMILY_CRST, ao_raw, analog_out=True),
        "io_no": ca.no_io_rows(ca.FAMILY_CRST, bits),
    }


def _uaria_io_from_snapshot(
    out: Mapping[str, Any],
    db: Optional[Sequence[bool]],
) -> Dict[str, Any]:
    """У uAria отдельного блока входов/выходов нет — колонки собираются из уже
    прочитанного снимка, ни одной дополнительной транзакции."""
    ca = _ca()
    ir_u: Dict[int, Any] = {}
    for key, addr in (
        ("oat", ca.IR_UARIA_OAT),
        ("sat", ca.IR_UARIA_SAT),
        ("rwt", ca.IR_UARIA_RWT),
    ):
        if key in out:
            ir_u[addr] = out[key]
    ao: Dict[int, Any] = {}
    if "valve" in out:
        ao[ca.IR_UARIA_VALVE] = out["valve"]
    if "fan" in out:
        ao[ca.IR_UARIA_FAN] = out["fan"]
    no_bits: Dict[int, bool] = {}
    if "uaria_run" in out:
        no_bits[ca.COIL_UARIA_NET_ON_OFF] = bool(out["uaria_run"])
    if "gs04" in out:
        no_bits[ca.COIL_UARIA_NET_ENABLE] = bool(out["gs04"])
    di: List[Dict[str, Any]] = []
    if db:
        for i in range(min(16, len(db))):
            di.append({"tag": "DI%d" % (i + 1), "text": "DI%d" % (i + 1), "on": bool(db[i])})
    return {
        "io_di": di,
        "io_u": ca.analog_io_rows(ca.FAMILY_UARIA, ir_u, analog_out=False),
        "io_ao": ca.analog_io_rows(ca.FAMILY_UARIA, ao, analog_out=True),
        "io_no": ca.no_io_rows(ca.FAMILY_UARIA, no_bits),
    }

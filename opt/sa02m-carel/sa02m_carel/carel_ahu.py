# -*- coding: utf-8 -*-
"""Carel AHU (c.pCO / c.pCOmini / uAria) identity, BMS map helpers, start-write plan.

One home for FC17 fingerprinting and CRST/uAria register semantics on the board:
the flasher daemon (scan + config window) and the Modbus-MQTT bridge both import
this package, neither copies the map. Ported from the desktop flasher
(MR-02m-flasher, branch `carel`, flasher_windows/carel_ahu.py) with two edits:
the non-Carel predicate is injected instead of imported, and the PID-tuner token
(desktop-only) is dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .carel_ahu_map import (
    ALARM_BITS,
    CRST_AO_IR,
    CRST_NO_DI,
    CRST_U_IR,
    DISTAT_BITS,
    UARIA_ALARM_DI,
    UARIA_AO_IR,
    UARIA_NO_COIL,
    UARIA_U_IR,
)

FAMILY_CRST = "crst"
FAMILY_UARIA = "uaria"


# Holding / input / coil addresses (0-based, as on the wire / xls).
IR_OAT = 1
IR_SAT = 2
IR_RWT = 4
IR_HEAT_VALVE = 21
IR_DISP_SP = 50
IR_UNIT_STATUS = 116
IR_ALARM_PACKS: Tuple[int, ...] = (301, 302, 303, 304, 305, 306, 307, 308, 309, 310, 316)
IR_DISTAT_PACKS: Tuple[int, ...] = (311, 312, 313, 314, 315, 317)
HR_SYS_MODE = 49
HR_SP_WINTER = 51
HR_SP_SUMMER = 52
HR_FAN_SUPPLY = 53
HR_FAN_EXHAUST = 54
COIL_BMS_OFF_ON = 65
COIL_ALARM_RESET = 66
COIL_WIN_SUM = 67
COIL_MA17 = 129
COIL_MA18 = 130  # BMS on/off enable (Mam18)
COIL_MA19 = 131
COIL_MA20 = 132
DI_PUMP = 9
DI_KEYBOARD = 95  # Sys_KeyBoardOffOn — must be On before BMS coil 65 can run the plant
DI_SYS_ON = 96
START_MA18_SETTLE_S = 0.8

# uAria.xlsx / live CRSTDm_AHU (float32 ABCD, not CRST int16×10).
IR_UARIA_OAT = 0
IR_UARIA_SAT = 2
IR_UARIA_RWT = 4
IR_UARIA_VALVE = 18
IR_UARIA_STATUS = 26
IR_UARIA_FAN = 33
HR_UARIA_SP = 30
HR_UARIA_SP_SUMMER = 32
HR_UARIA_SEASON = 34
HR_UARIA_FAN_MIN = 195  # REAL Fs03, min analog fan %
HR_UARIA_FAN_SP = 197  # USINT Set/SF_1, steps 1..10 (manual §12.1.2.2 / §4.6.2)
COIL_UARIA_HEAT_COOL = 17
COIL_UARIA_NET_ON_OFF = 0  # BMS network start (manual 12.1.2.2)
COIL_UARIA_NET_ENABLE = 13  # Gs04 allow network on/off
COIL_UARIA_LOCAL = 30  # local terminal — do not write from the flasher
COIL_UARIA_ALARM_RESET = 37
ALARM_RESET_PULSE_S = 5.0  # some PLCs have no auto-clear; hold ON then write 0
DI_UARIA_RUN = 0
DI_UARIA_HEAT = 45
DI_UARIA_FAN = 53
DI_UARIA_PUMP_MAIN = 52
DI_UARIA_CRIT = 61
DI_UARIA_NONCRIT = 62
DI_UARIA_PUMP = 52
IR_UARIA_PROG_VER = 100

# CRSTDrAHAQ STD MODBUS: PLC clock (xls NewHour… / CurrHour…). Not in short uAria BMS map.
IR_CURR_HOUR = 111
IR_CURR_MINUTE = 112
IR_CURR_DAY = 113
IR_CURR_MONTH = 114
IR_CURR_YEAR = 115
HR_NEW_HOUR = 430
HR_NEW_MINUTE = 431
HR_NEW_DAY = 432
HR_NEW_MONTH = 433
HR_NEW_YEAR = 434
COIL_NEW_DATE_WR = 124
CLOCK_WRITE_PULSE_S = 0.3

SP_C_MIN = 0.0
SP_C_MAX = 99.0
UARIA_SP_MIN = 0.0
UARIA_SP_MAX = 50.0
FAN_PCT_MIN = 20.0
FAN_PCT_MAX = 100.0
UARIA_FAN_STEP_MIN = 1
UARIA_FAN_STEP_MAX = 10
SYS_MODE_MIN = 0
SYS_MODE_MAX = 5
# xls SystemStatus: v2 UnitStatus table from application 2.02.xx.52.
UNIT_STATUS_V2_FROM = (2, 2, 0, 52)
PLANT_RUN = "run"
PLANT_STOP = "stop"
PLANT_ALARM = "alarm"

KIND_COIL = "coil"
KIND_HOLDING = "holding"
KIND_HOLDING_MULTI = "holding_multi"

FC17_MAX_BYTE_COUNT = 246

# Longer ASCII ids first so "c.pCOmini" wins over "c.pCO".
_FC17_APP_IDS: Tuple[Tuple[bytes, str], ...] = (
    (b"CRSTDrAHAQ", FAMILY_CRST),
    (b"CRKRFAHAQ", FAMILY_CRST),
    (b"CRSTDm_AHU", FAMILY_UARIA),  # live slave 2: float32 uAria map, not int16 CRST
    (b"c.pCOmini", FAMILY_CRST),
    (b"uARIA", FAMILY_UARIA),
    (b"uAria", FAMILY_UARIA),
    (b"c.pCO", FAMILY_CRST),
)

_SIG_CARELS = (
    "CRSTDRAHAQ",
    "CRKRFAHAQ",
    "CRSTDM_AHU",
    "CRSTDM",
    "UARIA",
    "C.PCOMINI",
    "CPCOMINI",
    "C.PCO",
)

UARIA_UNIT_STATUS: Dict[int, str] = {
    1: "Включено",
    2: "Выключено по тревоге",
    3: "Выключено по сети",
    4: "Выключено с цифрового входа",
    5: "Выключено с клавиатуры",
}

UARIA_SEASON: Dict[int, str] = {
    0: "нет",
    1: "вручную",
    2: "авто по Тнар",
}

UNIT_STATUS_V1: Dict[int, str] = {
    0: "Выключено с клавиатуры",
    1: "Включено с клавиатуры",
    2: "Выключено по расписанию",
    3: "Включено по расписанию",
    4: "Выключено с цифрового входа",
    5: "Включено с цифрового входа",
    6: "Выключено с thTune",
    7: "Включено с thTune",
    8: "Прогрев водяного нагревателя",
    9: "Выключено по тревоге",
    10: "Ожидание соединения с платой расширения",
    11: "Приточный вентилятор в сервисном режиме",
    12: "Вытяжной вентилятор в сервисном режиме",
    13: "Вентиляторы в сервисном режиме",
    15: "Продувка",
    16: "Приоритет минимальной температуры обратной воды",
    17: "Выключено с цифрового входа и по расписанию",
    18: "Включено с цифрового входа и по расписанию",
    19: "Выключено. Сервисный режим",
}

UNIT_STATUS_V2: Dict[int, str] = {
    0: "---",
    1: "Включено",
    2: "Выключено по тревоге",
    3: "Выключено по сети BMS",
    4: "Выключено по расписанию",
    5: "Выключено с цифрового входа",
    6: "Выключено с th-Tune",
    7: "Вентиляторы в сервисном режиме",
    8: "Выключено с клавиатуры",
    9: "Продувка воздуховодов",
    10: "ожидание связи с платами(ой) расширения",
    11: "прогрев водяного нагревателя",
}

# Short English gist for the UI / tests; unknown codes stay "<code> alarm".
_ALARM_EN_GIST: Dict[str, str] = {
    "E00": "power fail",
    "E01": "fire alarm",
    "E22": "water heater frost pre-alarm",
    "E23": "water heater frost",
    "A16": "frost protection",
    "A32": "supply fan protection",
    "A41": "fire alarm",
}

# uAria short float32 map (IR/HR word addresses). Missing CRST controls stay disabled.
UARIA_FLOAT_IR: Tuple[Tuple[int, str, str], ...] = (
    (0, "oat", "OutdoorTemp"),
    (2, "sat", "SupplyTemp"),
    (4, "rwt", "ReturnWaterTemp"),
    (18, "heat_valve", "MainHeatValve"),
    (33, "fan", "FanSpeed"),
)
UARIA_FLOAT_HR: Tuple[Tuple[int, str, str], ...] = (
    (30, "sp", "TempSetpoint"),
)


@dataclass(frozen=True)
class CarelFingerprint:
    app_id: str
    std_mark: str
    version: Tuple[int, int, int, int]
    family: str

    def version_str(self) -> str:
        if self.version == (0, 0, 0, 0):
            return "—"
        a, b, c, d = self.version
        return "%d.%02d.%02d.%02d" % (a, b, c, d)


@dataclass(frozen=True)
class CarelWrite:
    kind: str
    address: int
    value: int = 0
    words: Tuple[int, ...] = ()


def format_carel_version(ver: Tuple[int, int, int, int]) -> str:
    return "%d.%02d.%02d.%02d" % ver


def normalize_sig(signature: str) -> str:
    return (signature or "").strip().upper().replace(" ", "")


def signature_looks_like_carel(signature: str) -> bool:
    n = normalize_sig(signature)
    if not n or n in ("—", "-", "NONE", "?"):
        return False
    for key in _SIG_CARELS:
        if key in n:
            return True
    return False


def _sig_blank(signature: str) -> bool:
    t = (signature or "").strip()
    return t == "" or t == "—" or t == "-"


def known_non_carel_module_signature(signature, *, is_known_module=None) -> bool:
    """MP/MR, WB, PI, DTV, LED: do not send FC17 on a mixed bus.

    ``is_known_module`` answers "this signature belongs to a non-Carel family we
    already recognise"; the flasher passes its own module_profiles predicate. It
    is injected rather than imported so this package stays the one home of the
    Carel map without depending on either consumer (the bridge has no module
    registry at all). No predicate ⇒ nothing is known ⇒ FC17 is allowed, which
    is the safe direction: an extra FC17 costs one frame, a skipped one loses
    the identity.
    """
    if signature_looks_like_carel(signature) or _sig_blank(signature):
        return False
    if is_known_module is None:
        return False
    return bool(is_known_module(signature))


def scan_should_probe_fc17(*, serial: int, signature: str, is_known_module=None) -> bool:
    """FC17 only for unknown / Carel rows. Known ours/WB keep HR290 even if SN is 0."""
    if signature_looks_like_carel(signature):
        return True
    if known_non_carel_module_signature(signature, is_known_module=is_known_module):
        return False
    sn = int(serial) & 0xFFFFFFFF
    if sn in (0, 0xFFFFFFFF):
        return True
    if _sig_blank(signature):
        return True
    return False


def network_params_writable(_family: str) -> bool:
    return False


def family_from_signature(signature: str) -> str:
    n = normalize_sig(signature)
    if "UARIA" in n or "CRSTDM" in n:
        return FAMILY_UARIA
    return FAMILY_CRST


def family_has_plc_clock(family: str) -> bool:
    """CurrHour IR111–115 / NewHour HR430–434 + coil 124 — CRST end-user map only."""
    return family == FAMILY_CRST


# ── c.pCOmini: вариант платы и физический состав входов/выходов ──────────────
# Прошивки поставляются папками STD_<версия>_B / _E / _H — Basic, Enhanced, HighEnd;
# метка попадает в ответ FC17 (STD_B / STD_E / STD_H) и разбирается в std_mark.
VARIANT_BASIC = "B"
VARIANT_ENHANCED = "E"
VARIANT_HIGHEND = "H"

_VARIANT_LABELS = {
    VARIANT_BASIC: "Basic",
    VARIANT_ENHANCED: "Enhanced",
    VARIANT_HIGHEND: "HighEnd",
}

# Физический состав c.pCOmini: 10 универсальных входов и 6 релейных выходов есть у
# всех вариантов; DI1/DI2 и Y1/Y2 — только у Enhanced и HighEnd. Какая функция на
# какой клемме, по сети не читается, поэтому интерфейс показывает функции карты BMS.


def variant_from_std_mark(std_mark: str) -> str:
    """"STD_E" → "E"; "STD"/пусто/неизвестное → "" (вариант не определён)."""
    m = str(std_mark or "").strip().upper()
    if m.startswith("STD_"):
        tail = m[4:]
        if tail in _VARIANT_LABELS:
            return tail
    return ""


def variant_label(variant: str) -> str:
    return _VARIANT_LABELS.get(str(variant or "").strip().upper(), "")


def variant_has_ao_di(variant: str) -> bool:
    """DI1/DI2 и Y1/Y2 есть у Enhanced/HighEnd. Basic — нет; неизвестный вариант не прячем."""
    return str(variant or "").strip().upper() != VARIANT_BASIC


def info_model_label(signature: str) -> str:
    return "uAria" if family_from_signature(signature) == FAMILY_UARIA else "c.pCOmini"


def info_photo_filename(signature: str) -> str:
    return "uAria.png" if family_from_signature(signature) == FAMILY_UARIA else "cpCOmini.png"


def normalize_clock_year(year: int) -> int:
    """Two-digit 0–99 reads as 2000+yy (26 → 2026); a full year (CurrYearFull) passes through."""
    y = int(year) & 0xFFFF
    if 0 <= y <= 99:
        return 2000 + y
    return y


def clock_tuple_valid(year: int, month: int, day: int, hour: int, minute: int) -> bool:
    y, mo, d, h, mi = (int(year), int(month), int(day), int(hour), int(minute))
    y = normalize_clock_year(y)
    if y < 2000 or y > 2099:
        return False
    if mo < 1 or mo > 12 or d < 1 or d > 31:
        return False
    if h < 0 or h > 23 or mi < 0 or mi > 59:
        return False
    return True


def format_plc_clock(year: int, month: int, day: int, hour: int, minute: int) -> str:
    if not clock_tuple_valid(year, month, day, hour, minute):
        return "—"
    y = normalize_clock_year(int(year))
    return "%04d-%02d-%02d %02d:%02d" % (y, int(month), int(day), int(hour), int(minute))


def clock_from_ir_words(words: Sequence[int]) -> Optional[Tuple[int, int, int, int, int]]:
    """IR 111..115 → (year, month, day, hour, minute) or None. Year 26 → 2026."""
    if not words or len(words) < 5:
        return None
    hour = int(words[0]) & 0xFFFF
    minute = int(words[1]) & 0xFFFF
    day = int(words[2]) & 0xFFFF
    month = int(words[3]) & 0xFFFF
    year = normalize_clock_year(int(words[4]) & 0xFFFF)
    if not clock_tuple_valid(year, month, day, hour, minute):
        return None
    return (year, month, day, hour, minute)


def clock_to_hr_words(year: int, month: int, day: int, hour: int, minute: int) -> List[int]:
    """HR 430..434: NewHour, NewMinute, NewDay, NewMonth, NewYearFull (xls range 0–9999, 2026 → 2026)."""
    y, mo, d, h, mi = int(year), int(month), int(day), int(hour), int(minute)
    y = normalize_clock_year(y)
    return [h & 0xFFFF, mi & 0xFFFF, d & 0xFFFF, mo & 0xFFFF, y & 0xFFFF]


def family_has_crst_controls(family: str) -> bool:
    return family == FAMILY_CRST


def _extract_version(payload: bytes) -> Optional[Tuple[int, int, int, int]]:
    """Only accept a 4-byte version that appears at least twice (CRSTDrAHAQ bench).

    A single walk picks garbage (IEEE floats, TLV) on CRSTDm_AHU.
    """
    counts: Dict[bytes, int] = {}
    for i in range(0, max(0, len(payload) - 3)):
        q = payload[i : i + 4]
        if 1 <= q[0] <= 9 and q[1] <= 99 and q[2] <= 99 and q[3] <= 99:
            counts[q] = counts.get(q, 0) + 1
    twice = [k for k, n in counts.items() if n >= 2]
    if not twice:
        return None
    pick = max(twice, key=lambda k: counts[k])
    return (pick[0], pick[1], pick[2], pick[3])


def parse_report_slave_id(payload: bytes) -> Optional[CarelFingerprint]:
    """Fail-closed: unknown ASCII → None (do not declare Carel)."""
    if not payload or len(payload) < 8:
        return None
    app_id: Optional[str] = None
    family: Optional[str] = None
    for raw, fam in _FC17_APP_IDS:
        if raw in payload:
            app_id = raw.decode("ascii")
            family = fam
            break
    if app_id is None:
        up = payload.upper()
        if b"UARIA" in up:
            app_id = "uARIA"
            family = FAMILY_UARIA
        elif b"CRSTDM" in up:
            app_id = "CRSTDm_AHU"
            family = FAMILY_UARIA
        else:
            return None
    ver = _extract_version(payload) or (0, 0, 0, 0)
    std = ""
    for mark in (b"STD_E", b"STD_H", b"STD_B", b"STD"):
        if mark in payload:
            std = mark.decode("ascii")
            break
    return CarelFingerprint(app_id=app_id, std_mark=std, version=ver, family=family)


def unit_status_labels(code: int) -> Tuple[str, str]:
    n = int(code) & 0xFFFF
    v1 = UNIT_STATUS_V1.get(n) or ("код %d" % n)
    v2 = UNIT_STATUS_V2.get(n) or ("код %d" % n)
    if n in UNIT_STATUS_V1 and not UNIT_STATUS_V1[n].strip():
        v1 = "код %d" % n
    return v1, v2


def parse_app_version(text: object) -> Optional[Tuple[int, int, int, int]]:
    s = str(text or "").strip()
    if not s or s in ("—", "-", "0.0.0.0"):
        return None
    parts = s.replace(",", ".").split(".")
    nums: List[int] = []
    for p in parts[:4]:
        try:
            nums.append(int(p, 10))
        except ValueError:
            return None
    if len(nums) < 2:
        return None
    while len(nums) < 4:
        nums.append(0)
    return (nums[0], nums[1], nums[2], nums[3])


def unit_status_use_v2(version: object) -> bool:
    """V2 UnitStatus table from application 2.02.xx.52 (xls SystemStatus)."""
    if isinstance(version, tuple) and len(version) >= 4:
        ver = (int(version[0]), int(version[1]), int(version[2]), int(version[3]))
    else:
        ver = parse_app_version(version)
    if ver is None:
        return True
    return ver >= UNIT_STATUS_V2_FROM


def unit_status_label(code: int, version: object = None) -> str:
    v1, v2 = unit_status_labels(code)
    return v2 if unit_status_use_v2(version) else v1


def plant_run_state(snap: Mapping[str, object], family: str, *, version: object = None) -> str:
    """Headline: alarm > running > stopped."""
    alarms = snap.get("alarms") or []
    if alarms or snap.get("crit") is True:
        return PLANT_ALARM
    unit = snap.get("unit")
    n = None if unit is None else int(unit) & 0xFFFF
    if family == FAMILY_UARIA:
        if n == 2:
            return PLANT_ALARM
        if snap.get("uaria_run") is True or n == 1:
            return PLANT_RUN
        return PLANT_STOP
    if n is not None:
        if unit_status_use_v2(version):
            if n == 2:
                return PLANT_ALARM
        elif n == 9:
            return PLANT_ALARM
    if snap.get("sys_on") is True or snap.get("bms_run") is True:
        return PLANT_RUN
    if n is None:
        return PLANT_STOP
    if unit_status_use_v2(version):
        return PLANT_RUN if n in (1, 7, 9, 11) else PLANT_STOP
    return PLANT_RUN if n in (1, 3, 5, 7, 8, 11, 12, 13, 15, 16, 18) else PLANT_STOP


def net_enable_write(family: str, enable: bool) -> CarelWrite:
    if family == FAMILY_UARIA:
        return CarelWrite(KIND_COIL, COIL_UARIA_NET_ENABLE, 1 if enable else 0)
    return CarelWrite(KIND_COIL, COIL_MA18, 1 if enable else 0)


def mode_label_key(mode: int) -> str:
    n = clamp_sys_mode(int(mode))
    return "carel_mode_%d" % n


def decode_distat_all(
    packs: Union[Mapping[int, int], Sequence[int]],
) -> List[Dict[str, Union[str, int, bool]]]:
    """Все известные биты Sv_DiStat с именем переменной Cd_* (xls «Регистры цифровых входов»).

    Клеммы не подписываем: какой вход на какой клемме — знает только мастер I/O в ПЛК.
    """
    words: List[int] = [0] * len(IR_DISTAT_PACKS)
    if isinstance(packs, Mapping):
        for i, addr in enumerate(IR_DISTAT_PACKS):
            if addr in packs:
                words[i] = int(packs[addr]) & 0xFFFF
    else:
        for i, w in enumerate(packs):
            if i >= len(words):
                break
            words[i] = int(w) & 0xFFFF
    out: List[Dict[str, Union[str, int, bool]]] = []
    for pi, bit, code, ru in DISTAT_BITS:
        on = False
        if 0 <= pi < len(words):
            on = bool(words[pi] & (1 << bit))
        out.append({
            "tag": code,
            "code": code,
            "text": ru,
            "on": on,
            "bit": bit,
            "reg": IR_DISTAT_PACKS[pi] if 0 <= pi < len(IR_DISTAT_PACKS) else 0,
        })
    return out


def _s16(raw: int) -> int:
    v = int(raw) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


# Probes the PLC does NOT alarm when they are absent. An unused analog input on
# these programs answers a successful read with a literal 0 and raises nothing —
# measured on bench 192.168.1.135 addr 1, where 16 of 18 analog inputs read
# exactly 0.0 and only the two wired probes (supply air, return water) carried a
# value, with no alarm set. A single reading therefore cannot tell "no probe
# fitted" from "the probe really reads zero".
#
# We resolve that ambiguity per probe, by whether the PLC would complain:
#   * OPTIONAL_PROBES — outdoor and room air. Their absence raises no alarm, so
#     an exact 0 is not publishable: on an installation with no outdoor probe it
#     would be a permanent, plausible-looking lie ("0.0 °C outside") that never
#     self-corrects.
#
#     The cost, stated exactly: a working probe sitting at the freezing point is
#     withheld on EVERY poll that reads exactly 0.0 — not once — and while that
#     lasts the control carries a read-ERROR flag, so a healthy sensor is
#     reported to consumers as a faulty one. It returns as soon as the value
#     leaves exact zero, which at a tenth of a degree is the next movement, but
#     a probe held precisely at 0.0 stays hidden. We accept that: it is a
#     bounded, self-correcting wrong state on an installation that HAS the
#     probe, against a permanent invention on every installation that has not.
#   * The supply-air and return-water probes are NOT in this set. Their absence
#     IS alarmed (E04, E05), so a 0 with no alarm standing is a real 0 and is
#     published as measured.
OPTIONAL_PROBES = ("oat", "rmt")


def probe_is_unfitted(probe: str, raw: object) -> bool:
    """True when this probe's raw reading must be reported as no reading.

    `probe` is a key from the snapshot ("oat", "sat", "rmt", "rwt"); only the
    OPTIONAL_PROBES are ever judged, so a supply-air or return-water zero can
    never be suppressed by a mistake at a call site. `raw` is the register word
    as read (int16 x10 on c.pCO, the float on uAria).

    A raw value that is missing or unreadable returns False — "not judged". The
    caller then publishes what it has: losing a real reading because a key was
    absent would be silent data loss, which is worse than the zero this rule
    exists to catch.
    """
    if probe not in OPTIONAL_PROBES:
        return False
    if raw is None:
        return False
    try:
        return float(raw) == 0.0
    except (TypeError, ValueError):
        return False


def int16_x10_to_phys(raw: int) -> float:
    return _s16(raw) / 10.0


def phys_to_raw_x10(phys: float, lo: float, hi: float) -> int:
    p = max(float(lo), min(float(hi), float(phys)))
    return int(round(p * 10.0))


def clamp_sp_c(phys: float) -> float:
    return max(SP_C_MIN, min(SP_C_MAX, float(phys)))


def clamp_fan_pct(phys: float) -> float:
    return max(FAN_PCT_MIN, min(FAN_PCT_MAX, float(phys)))


def clamp_sys_mode(mode: int) -> int:
    return max(SYS_MODE_MIN, min(SYS_MODE_MAX, int(mode)))


def clamp_uaria_sp_c(phys: float) -> float:
    return max(UARIA_SP_MIN, min(UARIA_SP_MAX, float(phys)))


def clamp_uaria_fan_step(raw: int) -> int:
    """Set-menu SF_1: 10 steps. th-Tune three-speed map is Fs05–Fs07, not this register."""
    return max(UARIA_FAN_STEP_MIN, min(UARIA_FAN_STEP_MAX, int(raw)))


def uaria_fan_step_to_pct(step: int, fs03: float) -> float:
    """Analog command ≈ Fs03 + step × (100 − Fs03) / 10 (manual §4.6.2)."""
    s = clamp_uaria_fan_step(step)
    fs = max(0.0, min(100.0, float(fs03)))
    return fs + s * (100.0 - fs) / 10.0


def uaria_unit_status_label(code: int) -> str:
    n = int(code) & 0xFFFF
    return UARIA_UNIT_STATUS.get(n) or ("код %d" % n)


def uaria_season_label(code: int) -> str:
    n = int(code) & 0xFFFF
    return UARIA_SEASON.get(n) or ("код %d" % n)


def decode_uaria_alarm_dis(active_addrs: Sequence[int]) -> List[Dict[str, Union[str, int]]]:
    """Active uAria discrete-input alarms. Unknown set bits → 'DI N'."""
    known = {addr: (code, ru) for addr, code, ru in UARIA_ALARM_DI}
    out: List[Dict[str, Union[str, int]]] = []
    seen = set()
    for raw in active_addrs:
        addr = int(raw)
        if addr in seen:
            continue
        seen.add(addr)
        hit = known.get(addr)
        if hit is None:
            out.append({"code": "DI%d" % addr, "text": "DI %d" % addr, "bit": 0, "reg": addr})
            continue
        code, ru = hit
        out.append({"code": code, "text": ru, "bit": 0, "reg": addr, "text_en": alarm_en_text(code, ru)})
    return out


def alarm_reset_coil(family: str) -> int:
    return COIL_UARIA_ALARM_RESET if family == FAMILY_UARIA else COIL_ALARM_RESET


def uaria_start_writes(
    net_enable: Optional[int],
    on: bool,
) -> Tuple[List[CarelWrite], Optional[str]]:
    """BMS start is coil 0, gated by Gs04 (coil 13). Never writes local-terminal coil 30."""
    if on:
        if net_enable is None:
            return [], "gs04_unknown"
        writes: List[CarelWrite] = []
        if int(net_enable) != 1:
            writes.append(CarelWrite(KIND_COIL, COIL_UARIA_NET_ENABLE, 1))
        writes.append(CarelWrite(KIND_COIL, COIL_UARIA_NET_ON_OFF, 1))
        return writes, None
    return [CarelWrite(KIND_COIL, COIL_UARIA_NET_ON_OFF, 0)], None


def format_uaria_ir100(raw: int) -> str:
    """IR100 integer firmware: 1048 → 1.0.48 (manual §12.1.2.1)."""
    n = int(raw) & 0xFFFF
    if n <= 0:
        return "—"
    return "%d.%d.%d" % (n // 1000, (n % 1000) // 100, n % 100)


def alarm_en_text(code: str, ru: str) -> str:
    gist = _ALARM_EN_GIST.get(code)
    if gist:
        return "%s %s" % (code, gist)
    return "%s alarm" % code


def decode_alarm_packs(
    packs: Union[Mapping[int, int], Sequence[int]],
) -> List[Dict[str, Union[str, int]]]:
    """Active alarms from IR 301–310 + 316. Unknown set bits → 'bit N register M'."""
    words: List[int] = [0] * len(IR_ALARM_PACKS)
    if isinstance(packs, Mapping):
        for i, addr in enumerate(IR_ALARM_PACKS):
            if addr in packs:
                words[i] = int(packs[addr]) & 0xFFFF
    else:
        for i, w in enumerate(packs):
            if i >= len(words):
                break
            words[i] = int(w) & 0xFFFF
    known = {(p, b): (code, ru) for p, b, code, ru in ALARM_BITS}
    out: List[Dict[str, Union[str, int]]] = []
    for pi, word in enumerate(words):
        addr = IR_ALARM_PACKS[pi]
        for bit in range(16):
            if not (word & (1 << bit)):
                continue
            hit = known.get((pi, bit))
            if hit is None:
                out.append(
                    {
                        "code": "bit%d" % bit,
                        "text": "бит %d регистра %d" % (bit, addr),
                        "bit": bit,
                        "reg": addr,
                    }
                )
                continue
            code, ru = hit
            out.append({"code": code, "text": ru, "bit": bit, "reg": addr, "text_en": alarm_en_text(code, ru)})
    return out


def decode_distat_packs(
    packs: Union[Mapping[int, int], Sequence[int]],
) -> List[Dict[str, Union[str, int]]]:
    words: List[int] = [0] * len(IR_DISTAT_PACKS)
    if isinstance(packs, Mapping):
        for i, addr in enumerate(IR_DISTAT_PACKS):
            if addr in packs:
                words[i] = int(packs[addr]) & 0xFFFF
    else:
        for i, w in enumerate(packs):
            if i >= len(words):
                break
            words[i] = int(w) & 0xFFFF
    known = {(p, b): (code, ru) for p, b, code, ru in DISTAT_BITS}
    out: List[Dict[str, Union[str, int]]] = []
    for pi, word in enumerate(words):
        addr = IR_DISTAT_PACKS[pi]
        for bit in range(16):
            if not (word & (1 << bit)):
                continue
            hit = known.get((pi, bit))
            if hit is None:
                out.append(
                    {
                        "code": "bit%d" % bit,
                        "text": "бит %d регистра %d" % (bit, addr),
                        "bit": bit,
                        "reg": addr,
                    }
                )
                continue
            code, ru = hit
            out.append({"code": code, "text": ru, "bit": bit, "reg": addr})
    return out


def _phys_from_raw(raw: int, scale: int) -> float:
    if int(scale) <= 1:
        return float(_s16(raw))
    return _s16(raw) / float(scale)


def analog_io_rows(
    family: str,
    ir_by_addr: Mapping[int, object],
    *,
    analog_out: bool,
) -> List[Dict[str, object]]:
    table = (UARIA_AO_IR if family == FAMILY_UARIA else CRST_AO_IR) if analog_out else (
        UARIA_U_IR if family == FAMILY_UARIA else CRST_U_IR
    )
    out: List[Dict[str, object]] = []
    for addr, tag, text, unit, scale in table:
        if addr not in ir_by_addr:
            out.append({"tag": tag, "text": text, "unit": unit, "value": None})
            continue
        if family == FAMILY_UARIA:
            phys = float(ir_by_addr[addr])
        else:
            phys = _phys_from_raw(int(ir_by_addr[addr]), int(scale))
        out.append({"tag": tag, "text": text, "unit": unit, "value": phys})
    return out


def no_io_rows(family: str, bits_by_addr: Mapping[int, bool]) -> List[Dict[str, object]]:
    table = UARIA_NO_COIL if family == FAMILY_UARIA else CRST_NO_DI
    out: List[Dict[str, object]] = []
    for addr, tag, text in table:
        on = bits_by_addr.get(int(addr))
        out.append({"tag": tag, "text": text, "on": bool(on) if on is not None else None})
    return out


def start_write_plan(
    mam18: Optional[int],
    sys_mode_target: Optional[int],
    bms_on: Optional[bool],
) -> Tuple[List[CarelWrite], Optional[str]]:
    """Network run: coil 130 (Ma18)=1, then coil 65 (Sys_BmsOffOn).

    Keyboard On (DI95) must already be true — BMS cannot replace that source.
    Sys_Mode is written only when the caller asks 1..5 (mode combobox).
    Stop is coil 65=0 (Ma18 and keyboard stay). ``mam18`` unused on start.
    """
    del mam18
    starting_mode = sys_mode_target is not None and 1 <= int(sys_mode_target) <= 5
    starting_bms = bms_on is True
    stopping_mode = sys_mode_target is not None and int(sys_mode_target) == 0
    stopping_bms = bms_on is False
    writes: List[CarelWrite] = []
    if starting_mode or starting_bms:
        writes.append(CarelWrite(KIND_COIL, COIL_MA18, 1))
        if starting_mode:
            writes.append(CarelWrite(KIND_HOLDING, HR_SYS_MODE, clamp_sys_mode(int(sys_mode_target))))
        writes.append(CarelWrite(KIND_COIL, COIL_BMS_OFF_ON, 1))
        return writes, None
    if stopping_bms:
        writes.append(CarelWrite(KIND_COIL, COIL_BMS_OFF_ON, 0))
    if stopping_mode:
        writes.append(CarelWrite(KIND_HOLDING, HR_SYS_MODE, 0))
    return writes, None


def distat_code_active(snap: Mapping[str, object], code: str) -> bool:
    for row in snap.get("distat") or []:
        if isinstance(row, Mapping) and row.get("code") == code:
            return True
    return False


def crst_start_writes(
    snap: Mapping[str, object],
    sys_mode_target: Optional[int] = 1,
    bms_on: Optional[bool] = True,
) -> Tuple[List[CarelWrite], Optional[str]]:
    """Ma18=1, then Sys_BmsOffOn (coil 65). ``snap`` unused for the plan."""
    del snap
    return start_write_plan(0, sys_mode_target, True if bms_on is None else bms_on)


def crst_start_block_reason(snap: Mapping[str, object]) -> Optional[str]:
    """None when Sys_Mode accepted (1..5) or Sys_On. Else not_running."""
    mode = snap.get("mode")
    if mode is not None and 1 <= int(mode) <= 5:
        return None
    if snap.get("sys_on") is True:
        return None
    return "not_running"


START_BLOCK_I18N = {
    "keyboard_off": "carel_block_keyboard",
    "fire_di": "carel_block_fire_di",
    "frost_di": "carel_block_frost_di",
    "not_running": "carel_block_not_running",
    "ma18_unknown": "carel_need_ma18",
}


def be_float32(reg_hi: int, reg_lo: int) -> float:
    """ABCD (big-endian) float32 from two holding/input words."""
    import struct

    raw = ((int(reg_hi) & 0xFFFF) << 16) | (int(reg_lo) & 0xFFFF)
    return struct.unpack(">f", struct.pack(">I", raw))[0]


def float32_to_be_words(value: float) -> Tuple[int, int]:
    import struct

    raw = struct.unpack(">I", struct.pack(">f", float(value)))[0]
    return (raw >> 16) & 0xFFFF, raw & 0xFFFF


def identity_complete_for_carel(signature: str, app_version: str) -> bool:
    """FC17 app id is enough; version may be «—» (CRSTDm_AHU has no duplicate version bytes)."""
    return signature_looks_like_carel(signature)

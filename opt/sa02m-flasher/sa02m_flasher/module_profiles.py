# -*- coding: utf-8 -*-
"""
Типы модулей MP-02m (Input reg 0), сигнатуры (Holding 290), коды датчиков AI (Core/Inc/ai_channel_base_c.h).

Изменения v1.0.1:
  - Добавлен MP02_CE02M3 = 100 — CE-02m-3, автономный трёхфазный анализатор сети (ATM90E32).
    Тип код 100 возвращается из Input reg 0 прошивкой CE-02m-3.
  - Добавлен DTV = 17 — CYNTRON DTV-RS-45, датчик микросреды (RTU_SENSOR).
    Тип код 17 возвращается из Input reg 0 прошивкой cyntron-dtv.
  - Добавлен SPECIAL_SIG_CODES и функция code_from_signature() — резервная идентификация
    по строке сигнатуры рег. 290 (CE02M3 → MP02_CE02M3, SENSOR/SENS. → DTV).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

# mp02_t как в MODBUS_VARIABLES.txt / get_type_module
MP02_DO6DI8 = 1
MP02_DO16 = 2
MP02_AO12 = 3
MP02_DO6 = 4
MP02_DI14 = 5
MP02_AO6AI6 = 6
MP02_AI12 = 7
MP02_DO4DI6 = 8
MP02_DI10CON = 10
MP02_DO5DI2AO = 11
MP02_EN_METER = 14    # мезонин EN_METER в составе MR-02m (Input reg 0 = 14)
MP02_TO4DI6 = 15
MP02_CE02M3  = 100   # CE-02m-3: автономный анализатор сети ATM90E32 (Input reg 0 = 100)
DTV          = 17    # CYNTRON DTV-RS-45: RTU_SENSOR (Input reg 0 = 17), идентификация по type_code или сигнатуре "Sens."

MP02_TYPE_NAMES: Dict[int, str] = {
    MP02_DO6DI8:  "DO6DI8",
    MP02_DO16:    "DO16",
    MP02_AO12:    "AO12",
    MP02_DO6:     "DO6",
    MP02_DI14:    "DI14",
    MP02_DI10CON: "10DIcon",
    MP02_DO5DI2AO:"6DO5DI2AO",
    MP02_AO6AI6:  "6AO6AI",
    MP02_AI12:    "12AI",
    MP02_DO4DI6:  "DO4DI6",
    MP02_EN_METER:"EN_METER",
    MP02_TO4DI6:  "TO4DI6",
    MP02_CE02M3:  "CE-02m-3",
    DTV:          "DTV-RS-45",
}

# (max_do, max_di, max_ao, max_ai) — грубо по карте Modbus
TYPE_IO_CAPS: Dict[int, Tuple[int, int, int, int]] = {
    MP02_DO6DI8:  (6, 8, 0, 0),
    MP02_DO16:    (16, 0, 0, 0),
    MP02_AO12:    (0, 0, 12, 0),
    MP02_DO6:     (6, 0, 0, 0),
    MP02_DI14:    (0, 14, 0, 0),
    MP02_DI10CON: (0, 10, 0, 0),
    MP02_DO5DI2AO:(6, 5, 2, 0),
    MP02_AO6AI6:  (0, 0, 6, 6),
    MP02_AI12:    (0, 0, 0, 12),
    MP02_DO4DI6:  (4, 6, 0, 0),
    MP02_TO4DI6:  (4, 6, 4, 0),
    MP02_EN_METER:(0, 0, 0, 0),
    MP02_CE02M3:  (0, 0, 0, 0),
    DTV:          (0, 0, 0, 0),
}

AI_ADC_SAMPLE_RATES_SPS: Tuple[int, ...] = (20, 45, 90, 175, 330, 600, 1000)
MODBUS_AI_WB_FILTER_BASE = 533
MODBUS_AI_KALMAN_PER_STOR_BASE = 491
REG_AO_SAFE_BASE_MAIN = 503
REG_AO_SAFE_BASE_6AO6AI = 503


@dataclass
class ModuleKind:
    code: int
    name: str
    max_do: int
    max_di: int
    max_ao: int
    max_ai: int


def kind_from_type_code(type_code: int) -> ModuleKind:
    name = MP02_TYPE_NAMES.get(type_code, f"тип {type_code}")
    caps = TYPE_IO_CAPS.get(type_code, (0, 0, 0, 0))
    return ModuleKind(type_code, name, caps[0], caps[1], caps[2], caps[3])


def normalize_signature(sig: str) -> str:
    s = (sig or "").strip().upper().replace(" ", "")
    return s


def strip_bootloader_signature_suffix(signature: str) -> str:
    """Убрать суффикс _bl у сигнатуры из EEPROM загрузчика (рег. 290)."""
    s = (signature or "").strip()
    if s.upper().endswith("_BL"):
        return s[: -3].strip()
    return s


# Как в прошивке / именах модулей (DO6DI8 рядом с 6DO8DI в подсказках).
_EXTRA_SIG_TOKENS_FOR_BATCH = (
    "DO6DI8",
    "6DO5DI2AO",
    "DO4DI6",
    "TO4DI6",
    "4TO6DI",
)


def device_allowed_for_mr_firmware_flash(signature: str, *, allow_unlisted: bool = False) -> bool:
    """
    Разрешена ли прошивка MR-02м «общим» образом для данной сигнатуры устройства.

    Один файл прошивки на всю линейку: проверяем только принадлежность к нашим
    модулям расширения (MR/MP-02м и совместимые), а не совпадение сигнатуры с полем в .fw.

    ``allow_unlisted=True`` — только для внутренних/тестовых сценариев (не API/UI).
    """
    if allow_unlisted:
        return True
    return is_mp_module_signature_for_batch_flash(signature)


def is_mp_module_signature_for_batch_flash(signature: str) -> bool:
    """
    Линейка MP-02m / MR-02m: для «Обновить все» прошиваем только устройства с этой сигнатурой.
    Пустая, NONE или неизвестная — пропуск (не пакетная цель).
    """
    s = strip_bootloader_signature_suffix(signature)
    n = normalize_signature(s)
    if not n or n in ("NONE", "—", "?"):
        return False
    if caps_from_signature(s) is not None:
        return True
    for tok in _EXTRA_SIG_TOKENS_FOR_BATCH:
        if tok in n:
            return True
    c_sig = code_from_signature(s)
    if c_sig in (MP02_CE02M3, DTV):
        return True
    # Сигнатуры с дефисами (MR-02m-DI16): сравниваем без - и _
    n_compact = n.replace("-", "").replace("_", "")
    for token in ("MP02M", "MR02M", "ENMETER", "EN_METER"):
        compact = token.replace("_", "")
        if compact in n_compact:
            return True
    return False


# Подсказка по строке сигнатуры (если рег. 0 недоступен)
_SIGNATURE_HINTS: Dict[str, Tuple[int, int, int, int]] = {
    "6DO8DI":    (6, 8, 0, 0),
    "16DO":      (16, 0, 0, 0),
    "12AO":      (0, 0, 12, 0),
    "6DO":       (6, 0, 0, 0),
    "14DI":      (0, 14, 0, 0),
    "10DICON":   (0, 10, 0, 0),
    "6DO5DI2AO": (6, 5, 2, 0),
    "6AO6AI":    (0, 0, 6, 6),
    "12AI":      (0, 0, 0, 12),
    "4DO6DI":    (4, 6, 0, 0),
    "4TO6DI":    (4, 6, 4, 0),
    "TO4DI6":    (4, 6, 4, 0),
    "CE02M3":    (0, 0, 0, 0),
}

# Сигнатуры специальных устройств → code (для _resolve_kind, если type_code не распознан)
# DTV: тип код 17 (RTU_SENSOR), сигнатура из EEPROM — заводская; дефолт "Sens."
SPECIAL_SIG_CODES: Dict[str, int] = {
    "CE02M3":  MP02_CE02M3,
    "CE-02M3": MP02_CE02M3,
    "CE-02M-3": MP02_CE02M3,
    "EN_METER": MP02_CE02M3,
    "ENMETER": MP02_CE02M3,
    "SENSOR":  DTV,     # модельная строка рег. 200 у DTV
    "SENS.":   DTV,     # дефолтная сигнатура EEPROM при пустом/несфабрикованном приборе
    "SENS":    DTV,
}

# --- RS-485 line profiles (application mode / reg 129 → bootloader) ---
# Источники: MR-02m/CE-02m-3/cyntron-dtv shared/bootloader (115200 8N1);
# Wiren Board / gw-lwip defaults (19200 8N2 app, 9600 8N2 bootloader — см. flash_protocol).

@dataclass(frozen=True)
class Rs485LineProfile:
    baudrate: int
    parity: str
    stopbits: int

    def as_tuple(self) -> Tuple[int, str, int]:
        return self.baudrate, self.parity, self.stopbits


PROFILE_MP_MR = Rs485LineProfile(115200, "N", 1)
PROFILE_WB_APP = Rs485LineProfile(19200, "N", 2)

# bl_module_sig.c (MR-02m) + module_profiles hints + serial_ranges
MP_MR_SIGNATURE_TOKENS: Tuple[str, ...] = (
    "6DO8DI", "16DO", "12AO", "6DO", "14DI", "10DICON", "6DO5DI2AO",
    "6AO6AI", "6AI6AO", "12AI", "4DO6DI", "4TO6DI", "TO4DI6",
    "DO6DI8", "DO4DI6", "TENZO2", "CE02M3", "ENMETER", "EN_METER",
    "MP02M", "MR02M", "SENSOR", "SENS.",
)

_WB_RELAY_SIG_PREFIXES: Tuple[str, ...] = (
    "MR2M", "MR3", "MR6", "MRPS", "MRWL", "MRWM", "MRM2",
)
_WB_MAO4_SIG_PREFIXES: Tuple[str, ...] = ("MAO4",)


def _norm_parity(p: str, default: str = "N") -> str:
    pr = (p or default).upper()
    return pr if pr in ("N", "E", "O") else default


def _norm_stopbits(v: int, default: int = 1) -> int:
    sb = int(v or default)
    return sb if sb in (1, 2) else default


def _profile_from_device_scan(
    device: Optional[Mapping[str, Any]],
    default: Rs485LineProfile,
) -> Rs485LineProfile:
    if not device:
        return default
    baud = int(device.get("baudrate") or 0) or default.baudrate
    parity = _norm_parity(str(device.get("parity") or default.parity), default.parity)
    stopbits = _norm_stopbits(int(device.get("stopbits") or 0), default.stopbits)
    return Rs485LineProfile(baud, parity, stopbits)


def is_wirenboard_module_signature(signature: str) -> bool:
    """
    Сторонний Modbus-модуль Wiren Board (.wbfw), не MP/MR/CE/DTV.
    Логика согласована с MR-02m-flasher module_profiles.is_wirenboard_modbus_remote_firmware_signature.
    """
    s = strip_bootloader_signature_suffix((signature or "").strip())
    if not s or s.upper() in ("NONE", "—", "?", "UNKNOWN"):
        return False
    if is_mp_module_signature_for_batch_flash(s):
        return False
    if code_from_signature(s) is not None:
        return False
    n = normalize_signature(s)
    for prefix in _WB_MAO4_SIG_PREFIXES:
        if n.startswith(prefix):
            return True
    for prefix in _WB_RELAY_SIG_PREFIXES:
        if n.startswith(prefix):
            return True
    if len(s) < 2 or len(s) > 32:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]+", s):
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    return True


def device_flash_route(signature: str) -> str:
    """
    Маршрут прошивки по сигнатуре устройства (рег. 290).

    ``mp_mr`` — наши MR/MP-02m, CE-02m-3, DTV (.fw, 115200 8N1, fast Modbus).
    ``wb`` — сторонние Wiren Board и прочие не-MR сигнатуры (.wbfw, 19200 8N2, WB algorithm).
    ``unknown`` — пустая или нераспознанная сигнатура.
    """
    s = strip_bootloader_signature_suffix(signature)
    if is_mp_module_signature_for_batch_flash(s):
        return "mp_mr"
    if is_wirenboard_module_signature(s):
        return "wb"
    n = normalize_signature(s)
    if not n or n in ("NONE", "—", "?", "UNKNOWN"):
        return "unknown"
    return "unknown"


def validate_firmware_device_route(
    signature: str,
    *,
    firmware_is_wb: bool,
    is_bootloader_firmware: bool = False,
) -> Optional[str]:
    """
    Проверка соответствия типа прошивки и сигнатуры устройства.
    Возвращает текст ошибки или None, если маршрут согласован.
    """
    dev_sig = strip_bootloader_signature_suffix(str(signature or "").strip()) or "?"
    route = device_flash_route(dev_sig)

    if route == "unknown":
        return (
            f"Сигнатура «{dev_sig}» не распознана. Выполните сканирование и выберите устройство "
            "с известной сигнатурой."
        )

    if is_bootloader_firmware:
        if route != "mp_mr":
            return (
                f"Прошивка bootloader (.fw) поддерживается только для модулей MR/MP-02m; "
                f"сигнатура «{dev_sig}» относится к стороннему устройству."
            )
        if firmware_is_wb:
            return "Образ bootloader должен быть в формате .fw, не .wbfw."
        return None

    if route == "mp_mr":
        if firmware_is_wb:
            return (
                f"Для модуля MR/MP-02m (сигнатура «{dev_sig}») выберите прошивку .fw, "
                "не .wbfw (Wiren Board)."
            )
        return None

    if route == "wb":
        if not firmware_is_wb:
            return (
                f"Для устройства «{dev_sig}» (сторонний Modbus / Wiren Board) выберите прошивку .wbfw, "
                "не .fw MR/MP-02m."
            )
        return None

    return f"Сигнатура «{dev_sig}» не поддерживается для прошивки."


def validate_batch_flash_targets(targets: List[Mapping[str, Any]]) -> Optional[str]:
    """
    Проверка согласованности маршрута для пакетной прошивки нескольких устройств.

    Все цели должны быть одного семейства: MR/MP (mp_mr) или WB (wb), без смешения.
    """
    if not targets:
        return "Список устройств для пакетной прошивки пуст"
    routes = [
        device_flash_route(str(t.get("signature") or ""))
        for t in targets
    ]
    unknown_sigs: List[str] = []
    for t, route in zip(targets, routes):
        if route != "unknown":
            continue
        sig = strip_bootloader_signature_suffix(str(t.get("signature") or "").strip()) or "?"
        unknown_sigs.append(sig)
    if unknown_sigs:
        shown = ", ".join(unknown_sigs[:4])
        if len(unknown_sigs) > 4:
            shown += ", …"
        return f"Сигнатура не распознана: {shown}. Выполните сканирование."
    has_mp = any(r == "mp_mr" for r in routes)
    has_wb = any(r == "wb" for r in routes)
    if has_mp and has_wb:
        return (
            "Нельзя прошивать вместе модули MR/MP и Wiren Board. "
            "Выберите устройства одного типа."
        )
    return None


def line_profile_family(signature: str, *, is_wb_firmware: bool = False) -> str:
    """'mp_mr' | 'wb' | 'unknown' — для логов и UI."""
    if is_wb_firmware:
        return "wb"
    if is_mp_module_signature_for_batch_flash(signature):
        return "mp_mr"
    if is_wirenboard_module_signature(signature):
        return "wb"
    return "unknown"


def application_line_profile(
    signature: str,
    *,
    device: Optional[Mapping[str, Any]] = None,
    is_wb_firmware: bool = False,
) -> Rs485LineProfile:
    """
    UART-параметры для обмена с приложением (reg 129 → bootloader).

    MP/MR-02m, CE-02m-3, DTV: всегда 115200 8N1 (DEFAULT_BAUD_RATE=1152 в прошивке;
    scan на шлюзе часто ложно находит 19200 N2).
    Wiren Board (.wbfw или сигнатура WB-MR*): baud/stop из скана или 19200 8N2.
    """
    if device:
        ab = device.get("app_line_baud")
        if ab is not None and int(ab or 0) > 0:
            return Rs485LineProfile(
                int(ab),
                _norm_parity(str(device.get("app_line_parity") or "N")),
                _norm_stopbits(int(device.get("app_line_stopbits") or 1)),
            )

    if is_wb_firmware or is_wirenboard_module_signature(signature):
        return _profile_from_device_scan(device, PROFILE_WB_APP)
    if is_mp_module_signature_for_batch_flash(signature):
        return PROFILE_MP_MR
    if not is_wb_firmware:
        return PROFILE_MP_MR
    return _profile_from_device_scan(device, PROFILE_WB_APP)


def code_from_signature(signature: str) -> Optional[int]:
    """Определить тип устройства по строке сигнатуры (рег. 290)."""
    n = normalize_signature(signature)
    for key, code in SPECIAL_SIG_CODES.items():
        if key in n or n.startswith(key):
            return code
    return None


def caps_from_signature(signature: str) -> Optional[Tuple[int, int, int, int]]:
    n = normalize_signature(signature)
    for key, caps in _SIGNATURE_HINTS.items():
        if key in n or n.startswith(key[:4]):
            return caps
    return None


# AI sensor enum (прошивка) — подписи как в desktop UI
AI_SENSOR_CHOICES: List[Tuple[int, str]] = [
    (0x0000, "Выключен"),
    (0x0001, "NTC 10k (B3950)"),
    (0x0002, "Pt1000 (α385)"),
    (0x0003, "Pt100 (α385)"),
    (0x0004, "Напряжение 0–10 В"),
    (0x0005, "Ток 4–20 мА"),
    (0x0006, "Термопара K (ТХА)"),
    (0x0007, "Сухой контакт"),
    (0x0008, "Pt50 (α385)"),
    (0x0009, "Pt500 (α385)"),
    (0x000A, "NTC 100k (B3950)"),
    (0x000B, "NTC 10k (B3988)"),
    (0x000C, "NTC 10k (B3435)"),
    (0x000D, "NTC 10k (B3470)"),
    (0x000E, "Pt100 (α391), 100П"),
    (0x000F, "Pt1000 (α391), 1000П"),
    (0x0010, "Pt100 (α428), 100М"),
    (0x0011, "Pt1000 (α428), 1000М"),
    (0x0012, "Ni100 (α617)"),
    (0x0013, "Ni500 (α617)"),
    (0x0014, "Ni1000 (α617)"),
    (0x0015, "Ток 0–5 мА"),
    (0x0016, "Ток 0–20 мА"),
    (0x0017, "Дифф. напряжение ±50 мВ"),
    (0x0018, "Дифф. напряжение ±2 В"),
    (0x0019, "NTC 5k (B3470)"),
    (0x001A, "NTC 1.8k (B3380)"),
    (0x001B, "Pt50 (α385), 3-пров."),
    (0x001C, "Pt100 (α385), 3-пров."),
    (0x001D, "Pt500 (α385), 3-пров."),
    (0x001E, "Pt1000 (α385), 3-пров."),
    (0x001F, "Pt100 (α391), 100П, 3-пров."),
    (0x0020, "Pt1000 (α391), 1000П, 3-пров."),
    (0x0021, "Pt100 (α428), 100М, 3-пров."),
    (0x0022, "Pt1000 (α428), 1000М, 3-пров."),
    (0x0023, "Ni100 (α617), 3-пров."),
    (0x0024, "Ni500 (α617), 3-пров."),
    (0x0025, "Ni1000 (α617), 3-пров."),
    (0x0026, "Напряжение 0–30 В"),
]


def ai_sensor_label(code: int) -> str:
    for c, lbl in AI_SENSOR_CHOICES:
        if c == code:
            return lbl
    return f"код 0x{code:04X}"


def is_6ao6ai_module(kind: Optional[ModuleKind]) -> bool:
    if kind is None:
        return False
    if kind.code == MP02_AO6AI6:
        return True
    return (
        kind.code == 0
        and kind.max_do == 0
        and kind.max_di == 0
        and kind.max_ao == 6
        and kind.max_ai == 6
    )


def is_12ai_module(kind: Optional[ModuleKind]) -> bool:
    if kind is None:
        return False
    if kind.code == MP02_AI12:
        return True
    return (
        kind.code == 0
        and kind.max_do == 0
        and kind.max_di == 0
        and kind.max_ao == 0
        and kind.max_ai == 12
    )


def kind_has_mp_ai_adc_filters(kind: Optional[ModuleKind]) -> bool:
    return is_6ao6ai_module(kind) or is_12ai_module(kind)


def ao_safe_holding_register(channel_1_based: int, kind: Optional[ModuleKind] = None) -> int:
    ch = max(1, int(channel_1_based))
    if kind is not None and is_6ao6ai_module(kind):
        return REG_AO_SAFE_BASE_6AO6AI + ch - 1
    return REG_AO_SAFE_BASE_MAIN + ch - 1


def ai_channel_stride(
    type_code: Optional[int] = None,
    kind: Optional[ModuleKind] = None,
) -> int:
    if kind is not None and (is_6ao6ai_module(kind) or is_12ai_module(kind)):
        return 7
    if type_code in (MP02_AO6AI6, MP02_AI12):
        return 7
    return 14


def ai_channel_base_register(
    channel_1_based: int,
    type_code: Optional[int] = None,
    kind: Optional[ModuleKind] = None,
) -> int:
    """Первый holding регистра канала AI: канал 1 → 400."""
    if channel_1_based < 1:
        raise ValueError("channel >= 1")
    return 400 + (channel_1_based - 1) * ai_channel_stride(type_code, kind)


def ai_calibration_holding_register(
    channel_1_based: int,
    type_code: Optional[int] = None,
    kind: Optional[ModuleKind] = None,
) -> int:
    return ai_channel_base_register(channel_1_based, type_code, kind) + 4


def ai_wb_filter_holding_regs(stor: int) -> Tuple[int, int, int]:
    s = max(0, int(stor))
    base = MODBUS_AI_WB_FILTER_BASE + 3 * s
    return (base, base + 1, base + 2)


def ai_kalman_holding_reg(stor: int) -> int:
    return MODBUS_AI_KALMAN_PER_STOR_BASE + max(0, int(stor))


def ai_stor_for_12ai_channel(ch_1_based: int) -> int:
    return max(0, min(11, int(ch_1_based) - 1))


def ai_stor_for_6ao6ai_p(ch_1_based: int) -> int:
    """6AO6AI: UI-канал → stor для Kalman/WB (491+stor, 533+3·stor).

    Эталон MR-02m-flasher (module_profiles + tools STOR map), согласован с app_c.c AO6AI6:
      channels[0]: адр=4, storP=6, storN=7  → UI AI1(P), AI2(N)
      channels[1]: адр=5, storP=8, storN=9  → UI AI3(P), AI4(N)
      channels[2]: адр=6, storP=10, storN=11 → UI AI5(P), AI6(N)
    Для вкладки по логическому каналу k=1..6 используется stor = 5+k (слоты верхнего ряда).
    """
    m = {1: 6, 2: 7, 3: 8, 4: 9, 5: 10, 6: 11}
    return m[max(1, min(6, int(ch_1_based)))]


def ai_adc_coerce_sample_rate_sps(v: int) -> int:
    vv = int(v)
    if vv in AI_ADC_SAMPLE_RATES_SPS:
        return vv
    return min(AI_ADC_SAMPLE_RATES_SPS, key=lambda item: abs(item - vv))


# --- AI UI grouping (desktop module_config_window parity) ---
AI_UI_BUCKET_OFF = "off"
AI_UI_BUCKET_NTC = "ntc"
AI_UI_BUCKET_RTD = "rtd"
AI_UI_BUCKET_VOLT = "volt"
AI_UI_BUCKET_CURR = "curr"
AI_UI_BUCKET_TC_K = "tc_k"
AI_UI_BUCKET_DRY = "dry"

AI_UI_BUCKET_LABELS: Dict[str, str] = {
    AI_UI_BUCKET_OFF: "Выключен",
    AI_UI_BUCKET_NTC: "NTC",
    AI_UI_BUCKET_RTD: "Pt / Ni RTD",
    AI_UI_BUCKET_VOLT: "Напряжение",
    AI_UI_BUCKET_CURR: "Ток",
    AI_UI_BUCKET_TC_K: "Термопара K",
    AI_UI_BUCKET_DRY: "Сухой контакт",
}

AI_UI_BUCKET_ORDER: Tuple[str, ...] = (
    AI_UI_BUCKET_OFF,
    AI_UI_BUCKET_NTC,
    AI_UI_BUCKET_RTD,
    AI_UI_BUCKET_VOLT,
    AI_UI_BUCKET_CURR,
    AI_UI_BUCKET_TC_K,
    AI_UI_BUCKET_DRY,
)

_NTC_CODES = frozenset({0x0001, 0x000A, 0x000B, 0x000C, 0x000D, 0x0019, 0x001A})
_RTD_CODES = frozenset(
    {
        0x0002,
        0x0003,
        0x0008,
        0x0009,
        0x000E,
        0x000F,
        0x0010,
        0x0011,
        0x0012,
        0x0013,
        0x0014,
        0x001B,
        0x001C,
        0x001D,
        0x001E,
        0x001F,
        0x0020,
        0x0021,
        0x0022,
        0x0023,
        0x0024,
        0x0025,
    }
)
_VOLT_CODES = frozenset({0x0004, 0x0017, 0x0018, 0x0026})
_CURR_CODES = frozenset({0x0005, 0x0015, 0x0016})
_TC_K_CODES = frozenset({0x0006})
_DRY_CODES = frozenset({0x0007})


def ai_ui_sensor_bucket(sensor_code: int) -> str:
    c = int(sensor_code) & 0xFFFF
    if c == 0:
        return AI_UI_BUCKET_OFF
    if c in _NTC_CODES:
        return AI_UI_BUCKET_NTC
    if c in _RTD_CODES:
        return AI_UI_BUCKET_RTD
    if c in _VOLT_CODES:
        return AI_UI_BUCKET_VOLT
    if c in _CURR_CODES:
        return AI_UI_BUCKET_CURR
    if c in _TC_K_CODES:
        return AI_UI_BUCKET_TC_K
    if c in _DRY_CODES:
        return AI_UI_BUCKET_DRY
    return AI_UI_BUCKET_OFF


def ai_sidebar_nav_mode_tag(sensor_code: int) -> str:
    """Короткая подпись для сайдбара (desktop secondary label)."""
    b = ai_ui_sensor_bucket(sensor_code)
    if b == AI_UI_BUCKET_OFF:
        return "Выкл"
    labels = {
        AI_UI_BUCKET_NTC: "NTC",
        AI_UI_BUCKET_RTD: "RTD",
        AI_UI_BUCKET_VOLT: "U",
        AI_UI_BUCKET_CURR: "I",
        AI_UI_BUCKET_TC_K: "TC-K",
        AI_UI_BUCKET_DRY: "Сух",
    }
    return labels.get(b, "AI")


def ai_ui_subchoices_for_bucket(bucket: str) -> List[Tuple[int, str]]:
    b = str(bucket or AI_UI_BUCKET_OFF)
    if b == AI_UI_BUCKET_OFF:
        return [(0x0000, "Выключен")]
    out: List[Tuple[int, str]] = []
    for code, lbl in AI_SENSOR_CHOICES:
        if int(code) == 0:
            continue
        if ai_ui_sensor_bucket(int(code)) == b:
            out.append((int(code), lbl))
    return out


def ai_ui_temperature_calibration_applicable(sensor_code: int) -> bool:
    """Калибровка смещения температуры (holding base+4): только температурные режимы."""
    b = ai_ui_sensor_bucket(sensor_code)
    return b in (AI_UI_BUCKET_NTC, AI_UI_BUCKET_RTD, AI_UI_BUCKET_TC_K)

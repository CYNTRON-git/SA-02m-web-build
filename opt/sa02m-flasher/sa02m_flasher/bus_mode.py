# -*- coding: utf-8 -*-
"""Полевая шина модулей (bus mode) — единый источник правды по регистру 122 и
значениям для веб-интеграции флешер-демона.

Порт чистой логики из standalone-флешера (`MR-02m-flasher/flasher_windows/
bus_mode.py`, Operator 2026-08-05, план BACnet MS/TP §5.1/§5.4). ОБА семейства
(MR/MP/СЭ и DTV) выбирают шину через ОДИН family-common Holding 122:
  0 = классический Modbus (DMA), 1 = Fast Modbus, 2 = BACnet MS/TP; запись >2 →
  Modbus exception 3. Значения 0/1 применяются сразу; 2 персистится и вызывает
  авто-ребут (WITH_BACNET-сборка). Значения и семантика идентичны у обоих
  семейств; per-family отличается только текст диалога и vendor id проверки.

Авторитетные он-wire контракты прошивок (демон их НЕ дублирует, только следует):
  MR/MP/СЭ — ../MR-02m/docs/contracts/bus-protocol.md (рег.122, vendor 260,
             возврат: кнопка сброса / SWD / MSV:1 PV in-band)
  DTV      — ../cyntron-dtv/docs/contracts/bus-protocol.md (рег.122 с 1.0.1.52,
             vendor 381, объект AV:122, возврат: factory reset / WP AV:122=0)
  UX-зеркало — ../MR-02m-flasher/docs/contracts/bus-mode-selector.md
Web-side контракт: docs/contracts/web-bus-mode-bacnet.md.

Чистая логика: без serial-I/O, без HTTP, без импортов из соседних репозиториев.
"""
from __future__ import annotations

from typing import NamedTuple, Optional, Tuple

# ── Регистр выбора шины (Holding) — общий для MR/MP/СЭ и DTV ──────────────────
# Тот же номер, что device_config.REG_FAST_MODBUS — исторически известен демону
# как 2-state toggle; здесь расширяется до 3-state (см. device_config).
REG_BUS_MODE = 122  # family-common: 0 classic / 1 Fast / 2 BACnet

# ── Значения шины (Holding 122) — общие для обоих семейств ────────────────────
BUS_CLASSIC = 0  # классический Modbus (DMA)
BUS_FAST = 1     # Fast Modbus
BUS_BACNET = 2   # BACnet MS/TP
BUS_MODE_MIN = 0
BUS_MODE_MAX = 2
BUS_MODE_DEFAULT = BUS_FAST  # заводской дефолт (санитайзер кривого чтения)

# ── Идентификаторы семейств (только для диалогов и vendor id проверки) ────────
FAMILY_MR = "mr"    # MR / MP / СЭ (vendor 260)
FAMILY_DTV = "dtv"  # DTV (vendor 381)

# ── Список вариантов селектора: (значение, ключ i18n) — общий для семейств ────
BUS_MODE_CHOICES: Tuple[Tuple[int, str], ...] = (
    (BUS_CLASSIC, "bus_mode_classic"),
    (BUS_FAST, "bus_mode_fast"),
    (BUS_BACNET, "bus_mode_bacnet"),
)


def _coerce(v: object) -> Optional[int]:
    """int(v) или None, если значение не приводится к целому (мусорное чтение)."""
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def bus_mode_valid(v: object) -> bool:
    """Значение рег.122 в допустимом множестве {0,1,2}."""
    iv = _coerce(v)
    return iv is not None and BUS_MODE_MIN <= iv <= BUS_MODE_MAX


def sanitize(v: object) -> int:
    """рег.122: вне диапазона / мусор → заводской дефолт (1, Fast Modbus).

    Fail-closed: UI никогда не показывает фантомное состояние по кривому чтению.
    """
    iv = _coerce(v)
    if iv is None or not (BUS_MODE_MIN <= iv <= BUS_MODE_MAX):
        return BUS_MODE_DEFAULT
    return iv


def bus_mode_label(family: str, value: object) -> str:
    """Ключ i18n для значения шины (перевод делает вызывающий).

    Оба семейства делят 3-state пространство рег.122, поэтому family на выбор
    ключа не влияет; параметр сохранён для стабильности сигнатуры. Значение
    санитизируется, поэтому кривое чтение не даёт отсутствующего ключа.
    """
    _ = family
    safe = sanitize(value)
    for val, key in BUS_MODE_CHOICES:
        if val == safe:
            return key
    return BUS_MODE_CHOICES[0][1]  # недостижимо: дефолт всегда в списке


def selector_supported(family: object) -> bool:
    """Селектор показывается ТОЛЬКО для семейств с реальным 3-state рег.122
    (MR/MP/СЭ и DTV). CE-02m-3, WB, bootloader и прочее — скрыт (нет регистра).

    Fail-closed: неизвестное/None семейство → селектор скрыт.
    """
    return family in (FAMILY_MR, FAMILY_DTV)


# ── Возврат в Modbus (§5.4) ─────────────────────────────────────────────────
# Прямая запись возврата возможна только пока устройство ещё отвечает по Modbus
# (окно pending, до перезагрузки/латча BACnet). Залатченное устройство с шины
# Modbus не отвечает — тогда только in-band WP (MS/TP) или физическая кнопка.
# Регистр возврата — общий 122 для обоих семейств (значение 0 = classic).
RECOVERY_WRITE = "write"      # ещё отвечает по Modbus → пишем возвратное значение
RECOVERY_LATCHED = "latched"  # не отвечает по Modbus → in-band WP / физика


class RecoveryPlan(NamedTuple):
    action: str            # RECOVERY_WRITE | RECOVERY_LATCHED
    reg: int               # регистр возврата (122, общий для обоих семейств)
    value: Optional[int]   # значение для RECOVERY_WRITE (иначе None)


def force_modbus_return_value(family: str, prior_value: object = None) -> int:
    """Значение возврата в Modbus по рег.122: MR — предыдущее не-BACnet значение,
    если валидно, иначе classic 0; DTV → classic 0."""
    if family == FAMILY_MR:
        pv = _coerce(prior_value)
        if pv in (BUS_CLASSIC, BUS_FAST):
            return int(pv)  # type: ignore[arg-type]
    return BUS_CLASSIC


def force_modbus_recovery_plan(
    family: str, answered: bool, prior_value: object = None
) -> RecoveryPlan:
    """Решение «Вернуть Modbus» по факту отклика устройства по Modbus.

    answered=True (устройство ещё в окне pending, отвечает по Modbus) → прямая
    запись возвратного значения (рег.122=0/prior). answered=False (залатчено в
    BACnet, вне шины Modbus) → in-band WP по MS/TP либо физика (см. runner).
    """
    if answered:
        return RecoveryPlan(
            RECOVERY_WRITE, REG_BUS_MODE, force_modbus_return_value(family, prior_value)
        )
    return RecoveryPlan(RECOVERY_LATCHED, REG_BUS_MODE, None)


# ── PV↔mode map для in-band WP-восстановления MR (объект MSV:1) ──────────────
# Прошивка MR отдаёт «Bus Protocol» как Multistate Value, present-value = mode+1
# (1 classic / 2 Fast / 3 BACnet). Запись PV 1/2 взводит deferred reset →
# устройство возвращается на Modbus. (../MR-02m/scripts/hw/bacnet_recover.py.)
def mr_msv_pv_for_mode(mode: object) -> int:
    """present-value MSV:1 для целевого режима шины: sanitize(mode) + 1 ∈ {1,2,3}."""
    return sanitize(mode) + 1


def mode_from_mr_msv_pv(pv: object) -> int:
    """Обратное: режим шины из present-value MSV:1 (PV-1), санитизировано."""
    iv = _coerce(pv)
    if iv is None:
        return BUS_MODE_DEFAULT
    return sanitize(iv - 1)


# ── Inert-firmware honesty (§5.3) ───────────────────────────────────────────
SWITCH_APPLIED = "applied"        # ушёл в BACnet (не отвечает по Modbus) — успех
SWITCH_INERT = "inert"            # ещё отвечает по Modbus И рег.122 == 2 — прошивка без BACnet
SWITCH_NOT_APPLIED = "not_applied"  # ещё отвечает по Modbus, но рег.122 != 2 — запись не легла


def bacnet_switch_verdict(answered_modbus: bool, bus_mode_read: object) -> str:
    """Вердикт после окна применения записи 122=2 (§5.3).

    После deferred-reboot WITH_BACNET-модуль перестаёт отвечать по Modbus
    (ушёл на MS/TP) — это успех. Если модуль ЕЩЁ отвечает по Modbus:
      рег.122 == 2 → прошивка без поддержки BACnet (inert-сборка) либо переход
                     не применился как ожидалось;
      рег.122 != 2 → запись вообще не легла.
    Никогда не выдаём ложный успех, если устройство осталось на Modbus."""
    if not answered_modbus:
        return SWITCH_APPLIED
    if sanitize(bus_mode_read) == BUS_BACNET:
        return SWITCH_INERT
    return SWITCH_NOT_APPLIED

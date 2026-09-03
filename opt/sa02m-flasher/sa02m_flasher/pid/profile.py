# -*- coding: utf-8 -*-
"""
Профиль карты регистров контроллера.

Встроенные профили:
- «cpco-mini-crkrfahaq-2.3» — Carel c.pCO mini, программа CRKRFAHAQ v2.03.06.06
  («Список переменных cpCO mini MODBUS v.2.3.06.06.xlsx»);
- «uaria-1.0» — Carel uAria (REAL/float32).
Для иного контроллера адреса задаются пользовательским JSON-профилем.

**Один дом карты.** Адреса, которыми уже владеет общий пакет `sa02m_carel`
(PV, действующая уставка, клапан нагрева, наружная температура, обратная вода,
записываемая уставка, режим, статус включения, состояние, зима/лето, тревоги
uAria), читаются отсюда через шов `module_profiles.carel_ahu()` —
`docs/contracts/carel-ahu.md` §2, гейт `carel-shared-home`. Здесь остаются
только регистры ПИД-контура, которых общая карта не называет: Xp, Ti, Td,
тип регулирования, уставка защиты от замерзания, обратная связь привода,
скорость приточного вентилятора и адреса DI-тревог c.pCO.

Общий пакет обязателен: без него профиля нет (ProfileError). Демон
прошивальщика и так его импортирует (`carel_poll.py`), а тихий откат на
собственную копию адресов был бы второй картой — ровно то, что запрещено.

Обязательный минимум для настройки (REQUIRED_KEYS): pv, cv, sp_write, xp, ti.
Остальные регистры необязательны — соответствующие проверки супервизора
пропускаются, если регистра нет в профиле.

Все значения контроллера — int16; дробные передаются целыми с множителем
scale (обычно 10). Адреса хранятся как в документации производителя;
address_offset прибавляется к ним при обращении на линию (уточняется на
объекте: некоторые BMS-карты Carel нумеруют регистры с 1).
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..module_profiles import carel_ahu

# Типы данных регистра:
#   int16   — 1 регистр, знаковое, физ. = raw / scale (Carel c.pCO)
#   uint16  — 1 регистр, беззнаковое, физ. = raw / scale
#   float32 — 2 регистра, IEEE754 (Carel uAria «REAL»); scale не применяется
DT_INT16 = "int16"
DT_UINT16 = "uint16"
DT_FLOAT32 = "float32"

# Порядок слов для 32-битных значений:
#   big    — старшее слово первым (ABCD)
#   little — младшее слово первым (CDAB), встречается у ряда Carel-шлюзов
WORD_BIG = "big"
WORD_LITTLE = "little"

# Тип применения профиля (влияет на правила безопасности супервизора).
APP_AHU = "ahu"          # приточная / приточно-вытяжная вентиляция (водяной калорифер)
APP_GENERIC = "generic"  # прочее (водоснабжение, давление, …) — без правила прогрева

# Таблицы Modbus
T_COIL = "coil"    # FC01/FC05
T_DI = "di"        # FC02
T_IR = "ir"        # FC04
T_HR = "hr"        # FC03/FC06


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class RegisterDef:
    table: str            # coil / di / ir / hr
    addr: Optional[int]   # адрес по документации; None — не заполнен (шаблон)
    scale: float = 1.0    # физическое = raw / scale (только int16/uint16)
    signed: bool = True   # int16 (True) или uint16 — синоним dtype для 1 регистра
    name: str = ""        # имя переменной в контроллере
    desc: str = ""
    units: str = ""
    raw_min: Optional[int] = None   # допустимый диапазон raw при записи (int16/uint16)
    raw_max: Optional[int] = None
    dtype: str = ""       # "" → выбирается по signed; иначе int16/uint16/float32
    word_order: str = WORD_BIG      # для float32: big (ABCD) / little (CDAB)
    phys_min: Optional[float] = None  # физ. границы записи (float32)
    phys_max: Optional[float] = None

    @property
    def data_type(self) -> str:
        if self.dtype:
            return self.dtype
        return DT_INT16 if self.signed else DT_UINT16

    @property
    def count(self) -> int:
        """Число Modbus-регистров на значение."""
        return 2 if self.data_type == DT_FLOAT32 else 1

    # --- многорегистровое кодирование ---

    def decode(self, words: List[int]) -> float:
        """Слова Modbus (big-endian на линии) → физическое значение."""
        dt = self.data_type
        if dt == DT_FLOAT32:
            if len(words) < 2:
                raise ValueError("%s: float32 требует 2 слова" % (self.name or "reg"))
            hi, lo = (words[0], words[1]) if self.word_order == WORD_BIG \
                else (words[1], words[0])
            raw32 = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
            return struct.unpack(">f", struct.pack(">I", raw32))[0]
        v = int(words[0]) & 0xFFFF
        if dt == DT_INT16 and v >= 0x8000:
            v -= 0x10000
        return v / self.scale

    def encode(self, phys: float) -> List[int]:
        """Физическое значение → слова Modbus (для записи), с проверкой границ."""
        dt = self.data_type
        if dt == DT_FLOAT32:
            if self.phys_min is not None and phys < self.phys_min:
                raise ValueError("%s: %.3g ниже минимума %.3g"
                                 % (self.name or "reg", phys, self.phys_min))
            if self.phys_max is not None and phys > self.phys_max:
                raise ValueError("%s: %.3g выше максимума %.3g"
                                 % (self.name or "reg", phys, self.phys_max))
            raw32 = struct.unpack(">I", struct.pack(">f", float(phys)))[0]
            hi, lo = (raw32 >> 16) & 0xFFFF, raw32 & 0xFFFF
            return [hi, lo] if self.word_order == WORD_BIG else [lo, hi]
        raw = int(round(float(phys) * self.scale))
        if self.raw_min is not None and raw < self.raw_min:
            raise ValueError(
                "%s: raw %d ниже минимума %d" % (self.name or "reg", raw, self.raw_min))
        if self.raw_max is not None and raw > self.raw_max:
            raise ValueError(
                "%s: raw %d выше максимума %d" % (self.name or "reg", raw, self.raw_max))
        if raw < 0:
            raw += 0x10000
        return [raw & 0xFFFF]

    # --- совместимость: одно-регистровый int16/uint16 ---

    def to_phys(self, raw: int) -> float:
        return self.decode([raw])

    def to_raw(self, phys: float) -> int:
        words = self.encode(phys)
        if len(words) != 1:
            raise ValueError("%s: multi-register тип, используйте encode()"
                             % (self.name or "reg"))
        return words[0]


@dataclass
class Profile:
    name: str
    description: str = ""
    address_offset: int = 0
    regs: Dict[str, RegisterDef] = field(default_factory=dict)
    # Тип применения: "ahu" — приточная/приточно-вытяжная установка с водяным
    # калорифером (действует правило защиты теплообменника от разморозки зимой);
    # "generic" — прочие контуры (водоснабжение, давление и т. п.), где правило
    # прогрева теплообменника неприменимо.
    application: str = APP_AHU

    def reg(self, key: str) -> RegisterDef:
        try:
            return self.regs[key]
        except KeyError:
            raise KeyError("В профиле '%s' нет регистра '%s'" % (self.name, key))

    def is_ahu(self) -> bool:
        return self.application == APP_AHU

    def has(self, key: str) -> bool:
        r = self.regs.get(key)
        return r is not None and r.addr is not None

    def wire_addr(self, key: str) -> int:
        r = self.reg(key)
        if r.addr is None:
            raise ProfileError(
                "Профиль '%s': адрес регистра '%s' не заполнен" % (self.name, key))
        return r.addr + self.address_offset

    def unresolved_keys(self) -> List[str]:
        """Ключи с незаполненным адресом (шаблонный профиль)."""
        return [k for k, r in self.regs.items() if r.addr is None]

    def missing_required(self) -> List[str]:
        """Каких обязательных регистров не хватает для настройки."""
        return [k for k in REQUIRED_KEYS if not self.has(k)]

    # --- JSON ---

    def to_json(self) -> str:
        d = {
            "name": self.name,
            "description": self.description,
            "address_offset": self.address_offset,
            "application": self.application,
            "regs": {
                k: {
                    "table": r.table, "addr": r.addr, "dtype": r.data_type,
                    "scale": r.scale, "signed": r.signed,
                    "word_order": r.word_order,
                    "name": r.name, "desc": r.desc, "units": r.units,
                    "raw_min": r.raw_min, "raw_max": r.raw_max,
                    "phys_min": r.phys_min, "phys_max": r.phys_max,
                }
                for k, r in self.regs.items()
            },
        }
        return json.dumps(d, ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(text: str) -> "Profile":
        d = json.loads(text)
        regs = {}
        for k, r in d.get("regs", {}).items():
            regs[k] = RegisterDef(
                table=r["table"],
                addr=int(r["addr"]) if r.get("addr") is not None else None,
                scale=float(r.get("scale", 1.0)), signed=bool(r.get("signed", True)),
                name=r.get("name", ""), desc=r.get("desc", ""), units=r.get("units", ""),
                raw_min=r.get("raw_min"), raw_max=r.get("raw_max"),
                dtype=r.get("dtype", "") or "",
                word_order=r.get("word_order", WORD_BIG),
                phys_min=r.get("phys_min"), phys_max=r.get("phys_max"),
            )
        return Profile(
            name=d.get("name", "custom"),
            description=d.get("description", ""),
            address_offset=int(d.get("address_offset", 0)),
            application=d.get("application", APP_AHU),
            regs=regs,
        )

    @staticmethod
    def load(path: str) -> "Profile":
        with open(path, "r", encoding="utf-8") as f:
            return Profile.from_json(f.read())


# Ключи регистров, которые использует утилита.
KEY_PV = "pv"                      # температура канала (IR 2)
KEY_SP_ACTIVE = "sp_active"        # действующая уставка (IR 50)
KEY_CV = "cv"                      # клапан нагревателя, выход регулятора (IR 21)
KEY_VALVE_FB = "valve_fb"          # обратная связь привода (IR 73)
KEY_OAT = "oat"                    # наружная температура (IR 1)
KEY_RWT = "rwt"                    # температура обратной воды (IR 4)
KEY_FAN = "fan"                    # скорость приточного вентилятора (IR 36)
KEY_SP_WRITE = "sp_write"          # уставка зимняя (HR 51)
KEY_XP = "xp"                      # П-диапазон нагрева (HR 118)
KEY_TI = "ti"                      # время интегрирования нагрева (HR 119)
# Необязательный регистр времени дифференцирования (ПИД). У встроенных профилей
# Carel (c.pCO mini, uAria) его нет — задаётся в пользовательском JSON-профиле
# или в «Ручном профиле» GUI; запись Td выполняется только при его наличии.
KEY_TD = "td"
KEY_SYS_MODE = "sys_mode"          # режим работы — источник команды (HR 49)
KEY_SYS_ON = "sys_on"              # статус включения установки (DI 96 / uAria DI 0)
KEY_UNIT_STATUS = "unit_status"    # состояние установки (IR 116 / uAria IR 26)
KEY_REG_TYPE = "reg_type"          # тип регулирования зима (HR 123)
KEY_FROST_SP = "frost_sp"          # уставка защиты от замерзания (HR 195)
KEY_WIN_SUM = "win_sum"            # coil 67: зима(0)/лето(1)
KEY_AL_SAT_SENSOR = "al_sat_sensor"    # DI 104: E04 датчик канала
KEY_AL_RWT_SENSOR = "al_rwt_sensor"    # DI 105: E05 датчик обратной воды
KEY_AL_FROST_PRE = "al_frost_pre"      # DI 122: E22 предварительная защита
KEY_AL_FROST_MAIN = "al_frost_main"    # DI 123: E23 основная защита
KEY_AL_MANUAL_AO = "al_manual_ao"      # DI 114: E14 AO под ручным управлением

MONITOR_KEYS = [
    KEY_PV, KEY_SP_ACTIVE, KEY_CV, KEY_OAT, KEY_RWT, KEY_FAN, KEY_XP, KEY_TI,
    KEY_SYS_ON, KEY_UNIT_STATUS,
]
ALARM_KEYS = [
    KEY_AL_SAT_SENSOR, KEY_AL_RWT_SENSOR, KEY_AL_FROST_PRE, KEY_AL_FROST_MAIN,
    KEY_AL_MANUAL_AO,
]

# Без этих регистров настройка невозможна (чтение PV/CV, запись SP/Xp/Ti).
REQUIRED_KEYS = [KEY_PV, KEY_CV, KEY_SP_WRITE, KEY_XP, KEY_TI]


def _shared_map() -> Any:
    """Общая карта регистров Carel (`sa02m_carel.carel_ahu`) — единственный дом адресов.

    Отсутствие пакета — ошибка, а не повод развернуть здесь вторую копию карты:
    ровно эта копия и запрещена контрактом (`docs/contracts/carel-ahu.md` §2).
    """
    ca = carel_ahu()
    if ca is None:
        raise ProfileError(
            "Общий пакет sa02m_carel не найден: встроенные профили Carel берут "
            "адреса только из него (см. docs/contracts/carel-ahu.md §2). "
            "Установите /opt/sa02m-carel (scripts/lib.sh: sa02m_install_carel_pkg) "
            "или задайте JSON-профиль вручную.")
    return ca


def _uaria_alarm_di(ca: Any, code: str) -> int:
    """Адрес DI-тревоги uAria по её коду из общей таблицы UARIA_ALARM_DI."""
    for addr, c, _desc in ca.UARIA_ALARM_DI:
        if c == code:
            return int(addr)
    raise ProfileError(
        "Тревога %s отсутствует в общей карте uAria (UARIA_ALARM_DI)" % code)


def builtin_cpco_mini() -> Profile:
    """Профиль cpco-mini-crkrfahaq-2.3.

    Адреса общей карты — из `sa02m_carel`; здесь только регистры ПИД-контура,
    которых та карта не называет (см. модульную строку документации).
    """
    ca = _shared_map()
    r = {
        KEY_PV: RegisterDef(T_IR, ca.IR_SAT, 10, True, "Ca_Sat.Val",
                            "Температура воздуха в приточном воздуховоде", "°C"),
        KEY_SP_ACTIVE: RegisterDef(T_IR, ca.IR_DISP_SP, 10, True, "Rt_DispTSetp",
                                   "Выбранная (действующая) уставка температуры", "°C"),
        KEY_CV: RegisterDef(T_IR, ca.IR_HEAT_VALVE, 1, True, "Cy_Htv.Sv",
                            "Управление клапаном водяного нагревателя", "%"),
        KEY_VALVE_FB: RegisterDef(T_IR, 73, 10, True, "Ca_FbHtv.Val",
                                  "Обратная связь привода клапана", "%"),
        KEY_OAT: RegisterDef(T_IR, ca.IR_OAT, 10, True, "Ca_Oat.Val",
                             "Наружная температура", "°C"),
        KEY_RWT: RegisterDef(T_IR, ca.IR_RWT, 10, True, "Ca_Rwt.Val",
                             "Температура обратного теплоносителя", "°C"),
        KEY_FAN: RegisterDef(T_IR, 36, 1, True, "Cy_Sfsc.Sv",
                             "Управление скоростью приточного вентилятора", "%"),
        # raw 0…500 — собственная граница записи автоподбора (0…50 °C), уже, чем
        # диапазон ПЛК SP_C_MIN/SP_C_MAX (0…99 °C): опыт со ступенькой не имеет
        # причин уводить уставку выше 50 °C. Не сужать до общей карты и не
        # расширять до неё — это лимит утилиты, а не адрес.
        KEY_SP_WRITE: RegisterDef(T_HR, ca.HR_SP_WINTER, 10, True, "Rt_Setpoint",
                                  "Уставка регулятора температуры (зима)", "°C",
                                  raw_min=0, raw_max=500),
        KEY_XP: RegisterDef(T_HR, 118, 10, True, "Rtm02_HtSatXp",
                            "П-диапазон регулятора темп. приточного воздуха (нагрев)", "K",
                            raw_min=0, raw_max=120),
        KEY_TI: RegisterDef(T_HR, 119, 1, True, "Rtm03_HtSatTi",
                            "Время интегрирования регулятора (нагрев)", "s",
                            raw_min=0, raw_max=999),
        KEY_SYS_MODE: RegisterDef(T_HR, ca.HR_SYS_MODE, 1, True, "Sys_Mode",
                                  "Режим работы: 0 выкл, 1 вкл, 2 расписание, "
                                  "3 цифр.вход, 4 расп.+ЦВ, 5 th-Tune"),
        # Установку можно вести по сети при Sys_Mode=0, поэтому «работает ли она»
        # определяют статус DI Sys_On и состояние IR Sys_UnitStatus, а не режим.
        KEY_SYS_ON: RegisterDef(T_DI, ca.DI_SYS_ON, 1, False, "Sys_On",
                                "Статус включения установки"),
        KEY_UNIT_STATUS: RegisterDef(T_IR, ca.IR_UNIT_STATUS, 1, True, "Sys_UnitStatus",
                                     "Состояние установки"),
        KEY_REG_TYPE: RegisterDef(T_HR, 123, 1, True, "Rtm07_RegType",
                                  "Тип регулирования (зима): 0 прямое по каналу, "
                                  "1 каскад, 2 прямое по помещению"),
        KEY_FROST_SP: RegisterDef(T_HR, 195, 10, True, "Wam04_RwtFPSp",
                                  "Уставка защиты от замерзания по обратной воде", "°C"),
        KEY_WIN_SUM: RegisterDef(T_COIL, ca.COIL_WIN_SUM, 1, False, "Set_WinSum",
                                 "Зима(0)/лето(1)"),
        # DI-тревоги c.pCO (E-код + 100). Общая карта их не называет: там тревоги
        # c.pCO читаются битами упакованных IR 301…316 (ALARM_BITS), а не по DI,
        # — другой способ доступа к тем же сигналам, не тот же адрес. Опрос по DI
        # оставлен как в десктопной утилите: одно битовое чтение на тревогу
        # вместо разбора всех пакетов на каждом цикле опыта.
        KEY_AL_SAT_SENSOR: RegisterDef(T_DI, 104, 1, False, "AL_E04SnSat.Active",
                                       "E04 неисправен датчик температуры канала"),
        KEY_AL_RWT_SENSOR: RegisterDef(T_DI, 105, 1, False, "AL_E05SnRwt.Active",
                                       "E05 неисправен датчик обратной воды"),
        KEY_AL_FROST_PRE: RegisterDef(T_DI, 122, 1, False, "AL_E22PreWhFrz.Active",
                                      "E22 защита от замерзания (предварительная)"),
        KEY_AL_FROST_MAIN: RegisterDef(T_DI, 123, 1, False, "AL_E23WhFrz.Active",
                                       "E23 защита от замерзания (основная)"),
        KEY_AL_MANUAL_AO: RegisterDef(T_DI, 114, 1, False, "AL_E14ManuAo.Active",
                                      "E14 аналоговый выход под ручным управлением"),
    }
    return Profile(
        name="cpco-mini-crkrfahaq-2.3",
        description="Carel c.pCO mini, программа CRKRFAHAQ v2.03.06.06",
        address_offset=0,
        regs=r,
    )


def builtin_uaria() -> Profile:
    """
    Профиль Carel uAria (программа uARIA v.1.0.56, док. 1.0.18).

    Ключевое отличие от c.pCO: переменные типа REAL (float32, 2 регистра);
    Ti — UINT (1 регистр, секунды). Адреса — из таблицы переменных BMS
    руководства (раздел 12.1.2). Порт BMS по умолчанию: 19200 8N1
    (Hd02=19200, Hd03=0), адрес Hd01.

    Порядок слов float32 (word_order) на разных шлюзах Carel может отличаться
    (ABCD/CDAB) — при неверных значениях чтения PV поменяйте word_order на
    "little" в JSON-профиле. По умолчанию big (ABCD).

    Диапазоны Xp/Ti (Rt02/Rt03) в таблице BMS не приведены; заданы разумные
    инженерные границы (Xp 0.1–20 K, Ti 0–999 c) — при необходимости уточните
    по меню параметров контроллера.
    """
    ca = _shared_map()

    def real(table, addr, name, desc, units="°C", **kw):
        return RegisterDef(table, addr, name=name, desc=desc, units=units,
                           dtype=DT_FLOAT32, word_order=WORD_BIG, **kw)

    r = {
        # --- чтение (Input Register, REAL) ---
        KEY_PV: real(T_IR, ca.IR_UARIA_SAT, "SupplyTemp",
                     "Датчик температуры возд. в приточном воздуховоде"),
        KEY_CV: real(T_IR, ca.IR_UARIA_VALVE, "MainHeatValve",
                     "Упр. клапаном в конт. осн. водяного нагревателя", units="%"),
        KEY_OAT: real(T_IR, ca.IR_UARIA_OAT, "OutdoorTemp",
                      "Датчик температуры наружн. воздуха"),
        KEY_RWT: real(T_IR, ca.IR_UARIA_RWT, "ReturnWaterTemp",
                      "Датчик температуры обратной воды в контуре вод. нагревателя"),
        KEY_FAN: real(T_IR, ca.IR_UARIA_FAN, "FanSpeed",
                      "Упр. скоростью вентиляторов", units="%"),
        # --- запись (Holding Register) ---
        KEY_SP_WRITE: real(T_HR, ca.HR_UARIA_SP, "TempSetpoint", "Уставка температуры",
                           phys_min=ca.UARIA_SP_MIN, phys_max=ca.UARIA_SP_MAX),
        # Отдельной «действующей уставки» в короткой карте uAria нет: на графике
        # показываем ту же уставку, иначе линия уставки пустая (SP=— в журнале).
        KEY_SP_ACTIVE: real(T_HR, ca.HR_UARIA_SP, "TempSetpoint",
                            "Уставка температуры (действующая)"),
        KEY_XP: real(T_HR, 44, "Rt02_HtSatXp",
                     "Rt02. П-диапазон регулятора темп. приточн. воздуха (нагрев)",
                     units="K", phys_min=0.1, phys_max=20.0),
        KEY_TI: RegisterDef(T_HR, 46, scale=1, signed=False, name="Rt03_HtSatTi",
                            desc="Rt03. Время интегр. регулятора темп. приточн. "
                                 "воздуха (нагрев)", units="s",
                            dtype=DT_UINT16, raw_min=0, raw_max=999),
        KEY_SYS_ON: RegisterDef(T_DI, ca.DI_UARIA_RUN, scale=1, signed=False,
                                name="UnitOn",
                                desc="Статус работы установки", dtype=DT_UINT16),
        KEY_UNIT_STATUS: RegisterDef(T_IR, ca.IR_UARIA_STATUS, scale=1, signed=False,
                                     name="UnitStatus",
                                     desc="Состояние установки", dtype=DT_UINT16),
        KEY_REG_TYPE: RegisterDef(T_HR, 52, scale=1, signed=False, name="Rt07_RegType",
                                  desc="Rt07. Тип рег.темп.: 0 приточн.возд., "
                                       "1 каскад, 2 в помещении", dtype=DT_UINT16),
        KEY_FROST_SP: real(T_HR, 121, "Ua04_FrostSp",
                           "Ua04. Уставка темп. обр. теплонос. для защиты от замерзания"),
        # --- тревоги (Discrete Input) ---
        # Адреса A04/A08/A12 берутся из общей таблицы UARIA_ALARM_DI по коду;
        # DI 19 (термостат защиты от замерзания) — не тревога из той таблицы,
        # а отдельный дискретный вход, общая карта его не называет.
        KEY_AL_SAT_SENSOR: RegisterDef(T_DI, _uaria_alarm_di(ca, "A04"), signed=False,
                                       name="A04",
                                       desc="A04. Неисправен датчик темп. приточного воздуха"),
        KEY_AL_RWT_SENSOR: RegisterDef(T_DI, _uaria_alarm_di(ca, "A08"), signed=False,
                                       name="A08",
                                       desc="A08. Неисправен датчик темп. обратного теплоносителя"),
        KEY_AL_FROST_PRE: RegisterDef(T_DI, _uaria_alarm_di(ca, "A12"), signed=False,
                                      name="A12",
                                      desc="A12. Контролируемая температура ниже заданного предела"),
        KEY_AL_FROST_MAIN: RegisterDef(T_DI, 19, signed=False, name="FrostThermostat",
                                       desc="Термостат защиты от замерзания осн. водяного нагревателя"),
    }
    return Profile(
        name="uaria-1.0",
        description="Carel uAria (uARIA v.1.0.56, док. 1.0.18); REAL=float32",
        address_offset=0,
        regs=r,
    )


_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

BUILTIN_PROFILES = {
    "cpco-mini-crkrfahaq-2.3": builtin_cpco_mini,
    "cpco-mini": builtin_cpco_mini,  # алиас
    "uaria-1.0": builtin_uaria,
    "uaria": builtin_uaria,          # алиас
}


def list_profiles() -> Dict[str, str]:
    """Доступные профили: имя → описание (встроенные + <пакет>/profiles/*.json)."""
    out: Dict[str, str] = {}
    for name, factory in BUILTIN_PROFILES.items():
        out[name] = factory().description
    if _PROFILES_DIR.is_dir():
        for f in sorted(_PROFILES_DIR.glob("*.json")):
            try:
                p = Profile.from_json(f.read_text(encoding="utf-8"))
                out[f.stem] = p.description or p.name
            except (OSError, ValueError, KeyError):
                out[f.stem] = "(повреждённый файл профиля: %s)" % f
    return out


def get_profile(name_or_path: Optional[str] = None,
                require_resolved: bool = True) -> Profile:
    """
    Профиль по имени (встроенный или из <пакет>/profiles/) либо по пути
    к JSON-файлу. Без аргумента — cpco-mini (совместимость).

    require_resolved: отвергать профили с незаполненными адресами (шаблоны) —
    сообщение перечисляет, что осталось заполнить.
    """
    if not name_or_path:
        return builtin_cpco_mini()

    prof: Optional[Profile] = None
    if name_or_path in BUILTIN_PROFILES:
        prof = BUILTIN_PROFILES[name_or_path]()
    else:
        candidate = Path(name_or_path)
        if candidate.is_file():
            prof = Profile.load(str(candidate))
        else:
            packaged = _PROFILES_DIR / (name_or_path + ".json")
            if packaged.is_file():
                prof = Profile.load(str(packaged))
    if prof is None:
        raise ProfileError(
            "Профиль '%s' не найден. Доступны: %s (или путь к JSON-файлу)"
            % (name_or_path, ", ".join(sorted(list_profiles()))))

    if require_resolved:
        unresolved = prof.unresolved_keys()
        if unresolved:
            raise ProfileError(
                "Профиль '%s' — шаблон: не заполнены адреса регистров %s. "
                "Впишите адреса из таблицы BMS руководства контроллера."
                % (prof.name, ", ".join(unresolved)))
        missing = prof.missing_required()
        if missing:
            raise ProfileError(
                "Профиль '%s': отсутствуют обязательные регистры %s "
                "(минимум для настройки: %s)"
                % (prof.name, ", ".join(missing), ", ".join(REQUIRED_KEYS)))
    return prof

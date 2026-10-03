"""SPODES live OBIS codes and the scaler-unit conversion.

The catalogue is the Incotex instantaneous and energy tables (3.3–3.5):
class 3 registers, tariff group E = 0 only. Tariffs 1..4 are not published
on the live path. A missing object is a skip, not a failed poll.

IEC 62056-6-2 unit 30 is Wh. 1 234 567 with scaler −2 is 12 345.67 Wh.
The Devices card divides that by 1000 and shows kWh, the same way it does
for a CE-02m-3.
"""

from __future__ import annotations

from sa02m_spodes.axdr import TAG_STRUCTURE, AxdrError, Data, as_int

CLASS_REGISTER = 3
CLASS_DATA = 1
CLASS_PROFILE = 7
CLASS_CLOCK = 8
CLASS_DISCONNECT = 70

CLOCK_OBIS = (0, 0, 1, 0, 0, 255)
LOAD_PROFILE_OBIS = (1, 0, 99, 1, 0, 255)
EVENT_LOG_OBIS = (0, 0, 99, 98, 0, 255)
# Disconnect control, class 70. Methods confirmed against the object list
# of a given meter; these are the SPODES remote_disconnect / reconnect ids.
DISCONNECT_OBIS = (0, 0, 96, 3, 10, 255)
DISCONNECT_METHOD = 1
RECONNECT_METHOD = 2
# Single-phase voltage. Fills voltage_a only when the three-phase object
# is absent.
SINGLE_PHASE_VOLTAGE = (1, 0, 12, 7, 0, 255)

UNIT_W = 27
UNIT_VA = 28
UNIT_VAR = 29
UNIT_WH = 30
UNIT_VAH = 31
UNIT_VARH = 32
UNIT_A = 33
UNIT_V = 35
UNIT_HZ = 44

_UNIT_SYMBOL = {
    UNIT_W: "W",
    UNIT_VA: "VA",
    UNIT_VAR: "var",
    UNIT_WH: "Wh",
    UNIT_VAH: "VAh",
    UNIT_VARH: "varh",
    UNIT_A: "A",
    UNIT_V: "V",
    UNIT_HZ: "Hz",
}

# Instantaneous values — the fast poll.
POWER: dict[str, tuple[int, int, int, int, int, int]] = {
    "voltage_a": (1, 0, 32, 7, 0, 255),
    "voltage_b": (1, 0, 52, 7, 0, 255),
    "voltage_c": (1, 0, 72, 7, 0, 255),
    "voltage_ab": (1, 0, 12, 7, 1, 255),
    "voltage_bc": (1, 0, 12, 7, 2, 255),
    "voltage_ca": (1, 0, 12, 7, 3, 255),
    "current_a": (1, 0, 31, 7, 0, 255),
    "current_b": (1, 0, 51, 7, 0, 255),
    "current_c": (1, 0, 71, 7, 0, 255),
    "power_a": (1, 0, 21, 7, 0, 255),
    "power_b": (1, 0, 41, 7, 0, 255),
    "power_c": (1, 0, 61, 7, 0, 255),
    "power_total": (1, 0, 1, 7, 0, 255),
    "reactive_a": (1, 0, 23, 7, 0, 255),
    "reactive_b": (1, 0, 43, 7, 0, 255),
    "reactive_c": (1, 0, 63, 7, 0, 255),
    "reactive_total": (1, 0, 3, 7, 0, 255),
    "apparent_a": (1, 0, 29, 7, 0, 255),
    "apparent_b": (1, 0, 49, 7, 0, 255),
    "apparent_c": (1, 0, 69, 7, 0, 255),
    "apparent_total": (1, 0, 9, 7, 0, 255),
    "pf_a": (1, 0, 33, 7, 0, 255),
    "pf_b": (1, 0, 53, 7, 0, 255),
    "pf_c": (1, 0, 73, 7, 0, 255),
    "pf_total": (1, 0, 13, 7, 0, 255),
    "frequency": (1, 0, 14, 7, 0, 255),
}

# Cumulative energy, tariff sum (E = 0). Published in Wh / varh / VAh.
ENERGY: dict[str, tuple[int, int, int, int, int, int]] = {
    "energy_active_import": (1, 0, 1, 8, 0, 255),
    "energy_active_export": (1, 0, 2, 8, 0, 255),
    "energy_reactive_import": (1, 0, 3, 8, 0, 255),
    "energy_reactive_export": (1, 0, 4, 8, 0, 255),
    "energy_apparent": (1, 0, 9, 8, 0, 255),
}

LIVE = {**POWER, **ENERGY}

SAP = {"public": 16, "reader": 32, "configurator": 48}


def obis_bytes(code: tuple) -> bytes:
    return bytes(int(x) & 0xFF for x in code)


def live_name(code) -> str | None:
    """MQTT control name for a live OBIS, or None when it is not live."""
    want = tuple(int(x) for x in code)
    for name, obis in LIVE.items():
        if obis == want:
            return name
    return None


def physical(raw: int, scaler: int, unit: int) -> tuple[float, str]:
    """Register value × 10^scaler, and the IEC unit symbol ('' if unknown)."""
    mag = float(int(raw)) * (10.0 ** int(scaler))
    return mag, _UNIT_SYMBOL.get(int(unit), "")


def parse_scaler_unit(data: Data) -> tuple[int, int]:
    """Attribute 3 of a class 3 register: structure {scaler, unit}."""
    if (data.tag != TAG_STRUCTURE or not isinstance(data.value, list)
            or len(data.value) < 2):
        raise AxdrError("scaler-unit")
    return as_int(data.value[0]), as_int(data.value[1])

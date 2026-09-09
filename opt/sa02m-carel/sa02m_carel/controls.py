# -*- coding: utf-8 -*-
"""MQTT control inventory for a Carel AHU device (`type: carel` in the bridge).

One home for the control names, their Wiren-Board meta and which family carries
them: the bridge publishes from this table, the Alice topic picker offers the
same names, and `docs/contracts/carel-ahu.md` documents them. A name added here
without a producer in `bridge_carel.py` is caught by the bridge's own test.
"""
from __future__ import annotations

from typing import Tuple

FAMILY_CRST = "crst"
FAMILY_UARIA = "uaria"
BOTH = (FAMILY_CRST, FAMILY_UARIA)

# (name, wb_type, units, readonly, families)
#   wb_type — Wiren Board control type published to /meta/type
#   readonly — False means the bridge subscribes to <control>/on
CONTROLS: Tuple[Tuple[str, str, str, bool, Tuple[str, ...]], ...] = (
    ("unit_on",          "switch",      "",   False, BOTH),
    ("unit_status",      "value",       "",   True,  BOTH),
    ("unit_status_text", "text",        "",   True,  BOTH),
    ("plant_state",      "text",        "",   True,  BOTH),
    ("supply_temp",      "temperature", "°C", True,  BOTH),
    ("return_water_temp","temperature", "°C", True,  BOTH),
    ("room_temp",        "temperature", "°C", True,  (FAMILY_CRST,)),
    ("outdoor_temp",     "temperature", "°C", True,  BOTH),
    ("heat_valve",       "value",       "%",  True,  BOTH),
    ("setpoint",         "temperature", "°C", False, BOTH),
    ("setpoint_summer",  "temperature", "°C", False, BOTH),
    ("net_enable",       "switch",      "",   False, BOTH),
    ("sys_mode",         "value",       "",   False, (FAMILY_CRST,)),
    ("fan_supply",       "value",       "%",  False, (FAMILY_CRST,)),
    ("fan_exhaust",      "value",       "%",  False, (FAMILY_CRST,)),
    ("fan_step",         "value",       "",   False, (FAMILY_UARIA,)),
    ("pump",             "switch",      "",   True,  BOTH),
    ("alarm",            "switch",      "",   True,  BOTH),
    ("alarm_count",      "value",       "",   True,  BOTH),
    ("alarm_text",       "text",        "",   True,  BOTH),
)

# Writable setpoint limits per family (°C) — the same clamps carel_ahu applies.
SETPOINT_RANGE = {
    FAMILY_CRST: (0.0, 99.0),
    FAMILY_UARIA: (0.0, 50.0),
}
SETPOINT_PRECISION = 0.5


def controls_for(family: str) -> Tuple[Tuple[str, str, str, bool, Tuple[str, ...]], ...]:
    """Rows this family publishes."""
    fam = FAMILY_UARIA if str(family) == FAMILY_UARIA else FAMILY_CRST
    return tuple(row for row in CONTROLS if fam in row[4])


def control_names(family: str = "") -> Tuple[str, ...]:
    rows = CONTROLS if not family else controls_for(family)
    return tuple(row[0] for row in rows)


def writable_names(family: str) -> Tuple[str, ...]:
    return tuple(row[0] for row in controls_for(family) if not row[3])

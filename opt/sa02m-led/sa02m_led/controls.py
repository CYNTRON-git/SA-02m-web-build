# -*- coding: utf-8 -*-
"""MQTT control inventory for an LED strip device (`type: led` in the bridge).

One home for the control names, their Wiren-Board meta, and the register each
one reads or drives: the bridge publishes from this table, the Alice topic
picker offers the same names, and `docs/contracts/led-mb2ws.md` documents them.
A name added here without a producer in the bridge is caught by the bridge's own
test.

The `reg` column is the map address the control maps onto, so a reader can go
from a control name to `led_mb2ws` without guessing. It is deliberately the
address and not a copy of the semantics — the semantics stay in `led_mb2ws`.
"""
from __future__ import annotations

from typing import Optional, Tuple

from . import led_mb2ws as lm

FAMILY_LED = "led"

# (name, wb_type, units, readonly, reg)
#   wb_type  — Wiren Board control type published to /meta/type
#   readonly — False means the bridge subscribes to <control>/on
#   reg      — the primary holding register, or None for a composite/derived one
CONTROLS: Tuple[Tuple[str, str, str, bool, Optional[int]], ...] = (
    # --- scene / playback -----------------------------------------------------
    # `power` is PlayCtrl (416), not a supply relay: the strip has no mains
    # contactor on the bus side. 1 = PLAY, 0 = STOP. 416 is lock-gated, so the
    # bridge writes it through rgbw_lock_bracket_writes() like any 400..419
    # register — a plain write on a locked device is refused and the strip keeps
    # playing while MQTT reports it off.
    ("power",        "switch", "",  False, lm.MB2WS_PLAY_CTRL),
    ("play_state",   "value",  "",  True,  lm.MB2WS_PLAY_CTRL),
    ("brightness",   "range",  "",  False, lm.MB2WS_FX_PARAM),
    ("effect",       "value",  "",  False, lm.MB2WS_FX_ID),
    ("effect_group", "text",   "",  True,  None),
    ("speed",        "range",  "",  False, lm.MB2WS_FX_SPEED),
    ("scene_source", "value",  "",  False, lm.MB2WS_RENDER_SOURCE),
    # --- power PWM channels (product low map, permille 0..1000) ---------------
    # `color` carries the R/G/B triple of holdings 33..35 as `#RRGGBB`, the form
    # the Alice bridge's mqtt_to_color_setting() already accepts
    # (opt/sa02m-alice/.../converters.py). The W channel is NOT part of an RGB
    # triple, so it keeps its own control rather than a fourth hex byte nothing
    # downstream would parse.
    ("color",        "rgb",    "",  False, lm.RGBW_PWM_HOLDING_BASE),
    ("white",        "range",  "",  False, lm.RGBW_PWM_HOLDING_BASE + 3),
    ("pwm_mode",     "value",  "",  True,  lm.RGBW_PWM_STRIP_MODE_HOLDING),
    # --- marquee text ---------------------------------------------------------
    # Volatile on the device (516..579 is pixel-data class, never persisted), so
    # the read-back after a power-cycle is legitimately empty — the bridge must
    # not treat that as a lost write.
    ("text",         "text",   "",  False, lm.MB2WS_TEXT_BASE),
    # --- read-backs -----------------------------------------------------------
    ("led_count",    "value",  "",  True,  lm.MB2WS_LED_COUNT0),
    ("led_type",     "value",  "",  True,  lm.MB2WS_LED_TYPE),
    ("line_mode",    "value",  "",  True,  lm.MB2WS_LINE_MODE),
    # TextLines 494: 0/1 = one line, 2 = two 5×7 lines (FX 62/64). Not lock-gated.
    ("text_lines",   "value",  "",  True,  lm.MB2WS_TEXT_LINES),
    ("vled",         "voltage", "V", True, lm.RGBW_IREG_VLED),
    ("temperature",  "temperature", "°C", True, lm.RGBW_IREG_NTC),
    # --- 4 dry-contact inputs (TYPE_IO_CAPS RGBW_WS2812 = (0, 4, 0, 0)) -------
    ("di_1",         "switch", "",  True,  None),
    ("di_2",         "switch", "",  True,  None),
    ("di_3",         "switch", "",  True,  None),
    ("di_4",         "switch", "",  True,  None),
)

# Writable ranges the bridge clamps to before it touches the bus. Each mirrors
# the clamp the map itself applies, so a value that survives here survives the
# register write unchanged.
RANGE_LIMITS = {
    "brightness": (0, 255),          # FxParam 407
    "speed": (0, 255),               # FxSpeed 406
    "effect": (0, lm.RGBW_FX_MODE_COUNT - 1),  # FxId 405, 0..81
    "white": (0, lm.RGBW_PWM_PERMILLE_MAX),    # PWM W permille
    "scene_source": (lm.MB2WS_RENDER_POOL, lm.MB2WS_RENDER_FLASH),
}

TEXT_MAX_CHARS = lm.MB2WS_TEXT_MAX_CHARS


def control_names() -> Tuple[str, ...]:
    return tuple(row[0] for row in CONTROLS)


def writable_names() -> Tuple[str, ...]:
    return tuple(row[0] for row in CONTROLS if not row[3])


def control_row(name: str) -> Optional[Tuple[str, str, str, bool, Optional[int]]]:
    for row in CONTROLS:
        if row[0] == str(name):
            return row
    return None


def register_for(name: str) -> Optional[int]:
    """Primary holding register of ``name``, or None for a derived control."""
    row = control_row(name)
    return None if row is None else row[4]

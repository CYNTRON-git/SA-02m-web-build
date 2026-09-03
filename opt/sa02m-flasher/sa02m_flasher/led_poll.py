# -*- coding: utf-8 -*-
"""LED strip (RGBW_WS2812, type 120) snapshot reads for the web config window.

Ported from the desktop flasher (`MR-02m-flasher`, branch `feat/led-spy-and-picker`,
`flasher_windows/module_config_rgbw_tk.py` `_rgbw_strip_load_from_device` /
`_rgbw_scene_load_from_device`, plus the Qt page's conditional Spy read) with one
structural difference: the desktop loads a panel when the user opens it, this
module is called from a POLLED endpoint. So the read set is scoped BY TAB, not by
poll — every block below costs a full RS-485 transaction on a line shared with
MPLC4 and the MQTT bridge.

What is read where, and why that split:

  * ALWAYS — one FC03 of 20 registers at 400. This is the settings block, and it
    is the only read both the «Адресная лента» and «Сцена» tabs need: it carries
    the play state, the effect id, the brightness and the counts the window
    header shows on every tab. One transaction, so it does not need a tab.
  * «RGBW каналы» — the four PWM levels (33..36), the strip mode (49), and the
    live input registers (currents 37..40, NTC 131, VLED 132).
  * «Входы DI» — the DI configuration block 135..186 in one FC03 of 52.
  * «Сцена» — the expensive set: mode data 453..467, TextLines 494, the marquee
    window 516..579, and — ONLY when the selected effect is the line indicator
    (81) — the Spy/Master block 640..709. That last one is 70 registers for a
    card no other effect renders.

The mode-data read is exactly ONE FC03 of 15 registers at 453 and must never be
widened: 453..467 is contiguously mapped, an unmapped address anywhere in a block
fails the WHOLE read, and the panel then shows nothing rather than partial data.
The span comes from ``rgbw_mode_data_read_span()`` so it cannot drift here.

The register map is not restated by a single number: every address arrives from
the shared package through the flasher's one import seam,
``module_profiles.led_mb2ws()``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .modbus_io import (
    SendRtuFn,
    parse_regs_be_u16,
    read_holding,
    read_input_regs,
)
from . import module_profiles

# Tab ids the web window sends as `active_tab`. They gate the expensive reads
# above; an unknown tab reads the base block only (fail-safe to CHEAP — an
# unrecognised tab must never cost a 70-register Spy read).
LED_TAB_PWM = "led_pwm"
LED_TAB_DI = "led_di"
LED_TAB_STRIP = "led_strip"
LED_TAB_SCENE = "led_scene"

LED_TABS = (LED_TAB_PWM, LED_TAB_DI, LED_TAB_STRIP, LED_TAB_SCENE)

# Base settings block: 400..419 in one FC03 (the desktop strip panel's read).
STRIP_BLOCK_COUNT = 20


def _lm() -> Any:
    """The shared LED map through the package seam; without it there is no read."""
    lm = module_profiles.led_mb2ws()
    if lm is None:
        raise RuntimeError(
            "Пакет sa02m_led не установлен (/opt/sa02m-led) — "
            "карта регистров ленты недоступна"
        )
    return lm


def _hold(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int = 800,
) -> Optional[List[int]]:
    payload, err = read_holding(send, slave, start, count, timeout_ms)
    if err or not payload:
        return None
    regs = parse_regs_be_u16(payload)
    return regs if len(regs) >= count else None


def _inp(
    send: SendRtuFn,
    slave: int,
    start: int,
    count: int,
    timeout_ms: int = 800,
) -> Optional[List[int]]:
    payload, err = read_input_regs(send, slave, start, count, timeout_ms)
    if err or not payload:
        return None
    regs = parse_regs_be_u16(payload)
    return regs if len(regs) >= count else None


def read_led_snapshot(
    send: SendRtuFn,
    slave: int,
    *,
    active_tab: Optional[str] = None,
) -> Dict[str, Any]:
    """Live values of the strip, scoped to ``active_tab`` (see the module docstring).

    Always returns a dict; ``answered`` is False when the base block did not come
    back, so the window can say "нет связи" instead of rendering zeros.
    """
    lm = _lm()
    tab = str(active_tab or "").strip().lower()
    if tab not in LED_TABS:
        # Fail-safe to CHEAP: a tab id this build does not know (an older bundle,
        # a typo) reads the base block only. It must never fall through into the
        # 70-register Spy read by accident.
        tab = ""
    out: Dict[str, Any] = {"answered": False, "active_tab": tab or None}

    base = _hold(send, slave, lm.MB2WS_REG_BASE, STRIP_BLOCK_COUNT, 800)
    if base is None:
        return out
    out["answered"] = True
    out["strip"] = _decode_strip_block(lm, base)
    out["scene"] = _decode_scene_block(lm, base)

    if tab == LED_TAB_PWM:
        out["pwm"] = _read_pwm(lm, send, slave)
    elif tab == LED_TAB_DI:
        out["di"] = _read_di(lm, send, slave)
    elif tab == LED_TAB_SCENE:
        out.update(_read_scene_extras(lm, send, slave, out["scene"]["fx_id"]))
    return out


def _decode_strip_block(lm: Any, regs: List[int]) -> Dict[str, Any]:
    """400..419 → the «Адресная лента» tab's fields.

    Offsets are computed from the map's own constants, never counted by hand: a
    literal index here is how a donor offset survives a map change.
    """
    def at(reg: int) -> int:
        return int(regs[reg - lm.MB2WS_REG_BASE]) & 0xFFFF

    line_mode = at(lm.MB2WS_LINE_MODE)
    led_type = at(lm.MB2WS_LED_TYPE)
    layout = at(lm.MB2WS_MATRIX_LAYOUT)
    width, height = lm.rgbw_matrix_unpack(at(lm.MB2WS_MATRIX_WIDTH))
    return {
        "led_count0": at(lm.MB2WS_LED_COUNT0),
        "led_count1": at(lm.MB2WS_LED_COUNT1),
        "led_type": led_type,
        "pixel_format": at(lm.MB2WS_PIXEL_FORMAT),
        "auto_refresh": at(lm.MB2WS_AUTO_REFRESH),
        "byte_order": at(lm.MB2WS_BYTE_ORDER),
        "line_mode": line_mode,
        "line_ui": lm.rgbw_regs_to_line_ui(line_mode, led_type),
        "gamma": at(lm.MB2WS_GAMMA),
        "startup_mode": at(lm.MB2WS_STARTUP_MODE),
        "ch2_mode": at(lm.MB2WS_CH2_MODE),
        "locked": at(lm.MB2WS_LOCK) != lm.MB2WS_UNLOCK_KEY,
        "matrix_width": width,
        "matrix_height": height,
        "matrix_layout": layout,
        "matrix_tile_mode": lm.rgbw_matrix_layout_tilemode(layout),
        "matrix_tile_count": lm.rgbw_matrix_layout_tilecount(layout),
        "matrix_geometry": lm.rgbw_matrix_geometry_label(width, height),
    }


def _decode_scene_block(lm: Any, regs: List[int]) -> Dict[str, Any]:
    """400..419 → the «Сцена» tab's fields.

    ``fx_id`` is the CANONICAL id (``rgbw_resolve_fx_id``), not the wire word: the
    firmware also accepts the Gyver aliases 128..209, and every per-effect
    decision below — which cards the window shows, whether the Spy block is worth
    reading — must be taken on the resolved id or a strip left on an alias would
    read as effect «Статика».
    """
    def at(reg: int) -> int:
        return int(regs[reg - lm.MB2WS_REG_BASE]) & 0xFFFF

    fx_raw = at(lm.MB2WS_FX_ID)
    fx_id = lm.rgbw_resolve_fx_id(fx_raw)
    render_source = at(lm.MB2WS_RENDER_SOURCE)
    options = at(lm.MB2WS_OPTIONS)
    return {
        "render_source": render_source,
        "scene_ui": lm.rgbw_regs_to_scene_ui(render_source),
        "fx_id_raw": fx_raw,
        "fx_id": fx_id,
        "fx_group": lm.rgbw_fx_group_of(fx_id),
        "fx_speed": at(lm.MB2WS_FX_SPEED),
        "fx_param": at(lm.MB2WS_FX_PARAM),
        "flash_slot": at(lm.MB2WS_FLASH_SLOT),
        "play_ctrl": at(lm.MB2WS_PLAY_CTRL),
        "options": options,
        "loop": bool(options & lm.MB2WS_OPT_LOOP),
        "visibility": lm.rgbw_fx_control_visibility(fx_id),
    }


def _read_pwm(lm: Any, send: SendRtuFn, slave: int) -> Dict[str, Any]:
    """«RGBW каналы»: levels + strip mode (holding) and the live analog inputs.

    Three reads, not one: 33..36 and 49 are separated by addresses this product's
    low map does not document as mapped, and an unmapped address inside a block
    fails the whole block.
    """
    out: Dict[str, Any] = {}
    levels = _hold(send, slave, lm.RGBW_PWM_HOLDING_BASE, lm.RGBW_PWM_CHANNELS, 800)
    if levels is not None:
        out["levels"] = [int(v) & 0xFFFF for v in levels[: lm.RGBW_PWM_CHANNELS]]
    mode = _hold(send, slave, lm.RGBW_PWM_STRIP_MODE_HOLDING, 1, 700)
    if mode is not None:
        code = int(mode[0]) & 0xFFFF
        out["mode"] = code
        out["mode_labels"] = list(lm.rgbw_pwm_channel_label_keys(code))
        out["mode_visible"] = list(lm.rgbw_pwm_channel_visible(code))
    currents = _inp(send, slave, lm.RGBW_IREG_CURRENT_BASE, lm.RGBW_PWM_CHANNELS, 800)
    if currents is not None:
        out["currents_ma"] = [int(v) & 0xFFFF for v in currents[: lm.RGBW_PWM_CHANNELS]]
    # NTC (131) and VLED (132) are adjacent — one FC04 of two.
    supply = _inp(send, slave, lm.RGBW_IREG_NTC, 2, 700)
    if supply is not None:
        out["ntc_raw"] = int(supply[0]) & 0xFFFF
        out["vled_raw"] = int(supply[1]) & 0xFFFF
    return out


def _read_di(lm: Any, send: SendRtuFn, slave: int) -> Dict[str, Any]:
    """«Входы DI»: the configuration block 135..186 in ONE FC03 of 52.

    The press counters (220..237) are deliberately NOT read here: nothing in L4
    renders them, and their function code is a product-low-map claim this branch
    could not confirm against hardware. Adding a read whose reply shape is
    unverified would cost a timeout per poll on the tab for a field nobody shows.
    """
    count = (lm.RGBW_DI_HOLD_LAST - lm.RGBW_DI_MODE_BASE) + 1
    regs = _hold(send, slave, lm.RGBW_DI_MODE_BASE, count, 1000)
    if regs is None:
        return {}
    channels: List[Dict[str, Any]] = []
    for i in range(lm.RGBW_DI_COUNT):
        def at(base: int, idx: int = i) -> int:
            return int(regs[(base + idx) - lm.RGBW_DI_MODE_BASE]) & 0xFFFF

        channels.append(
            {
                "channel": i + 1,
                "mode": at(lm.RGBW_DI_MODE_BASE),
                "act_short": at(lm.RGBW_DI_ACT_SHORT_BASE),
                "act_long": at(lm.RGBW_DI_ACT_LONG_BASE),
                "act_double": at(lm.RGBW_DI_ACT_DOUBLE_BASE),
                "act_shortlong": at(lm.RGBW_DI_ACT_SHORTLONG_BASE),
                "long_ms": at(lm.RGBW_DI_LONG_MS_BASE),
                "double_ms": at(lm.RGBW_DI_DOUBLE_MS_BASE),
                "debounce_ms": at(lm.RGBW_DI_DEBOUNCE_BASE),
                "rate": at(lm.RGBW_DI_RATE_BASE),
                "step_short": at(lm.RGBW_DI_STEP_SHORT_BASE),
                "step_double": at(lm.RGBW_DI_STEP_DBL_BASE),
            }
        )
    encoders: List[Dict[str, Any]] = []
    # Two encoders, not four: 179..186 is four PAIRS of two registers.
    for n in range(2):
        def enc(base: int, idx: int = n) -> int:
            return int(regs[(base + idx) - lm.RGBW_DI_MODE_BASE]) & 0xFFFF

        encoders.append(
            {
                "encoder": n + 1,
                "type": enc(lm.RGBW_DI_ENC_TYPE_BASE),
                "target": enc(lm.RGBW_DI_ENC_TARGET_BASE),
                "step": enc(lm.RGBW_DI_ENC_STEP_BASE),
                "pulses": enc(lm.RGBW_DI_ENC_PULSES_BASE),
            }
        )
    return {"channels": channels, "encoders": encoders}


def _read_scene_extras(
    lm: Any,
    send: SendRtuFn,
    slave: int,
    fx_id: int,
) -> Dict[str, Any]:
    """Mode data (453×15), TextLines (494×1), the marquee window (516×64), and the
    Spy block (640×70) ONLY for the line-indicator effect.

    The Spy condition is the map's own predicate on the RESOLVED effect id, not a
    literal 81 here — the id lives in one home and the desktop Qt page takes the
    same decision the same way.
    """
    out: Dict[str, Any] = {}
    base, count = lm.rgbw_mode_data_read_span()
    mode_regs = _hold(send, slave, base, count, 800)
    if mode_regs is not None:
        decoded = lm.rgbw_mode_data_decode(mode_regs)
        if decoded is not None:
            out["mode_data"] = dict(decoded._asdict())
            out["mode_data"]["text_colors"] = list(decoded.text_colors)

    lines = _hold(send, slave, lm.MB2WS_TEXT_LINES, 1, 700)
    if lines is not None:
        raw = int(lines[0]) & 0xFFFF
        out["text_lines_raw"] = raw
        out["text_lines"] = lm.rgbw_text_lines_ui_code(raw)

    text_regs = _hold(send, slave, lm.MB2WS_TEXT_BASE, lm.MB2WS_TEXT_REG_COUNT, 1000)
    if text_regs is not None:
        out["text"] = lm.rgbw_unpack_text_cp1251(text_regs)

    if lm.rgbw_fx_uses_mb_spy(fx_id):
        spy_regs = _hold(
            send, slave, lm.MB2WS_SPY_WORK_PORT, lm.MB2WS_SPY_REG_COUNT, 1000
        )
        if spy_regs is not None:
            out["spy"] = _decode_spy(lm, spy_regs)
    return out


def _decode_spy(lm: Any, regs: List[int]) -> Dict[str, Any]:
    """640..709 → the Spy/Master card. Addresses come from the map's helpers."""
    def at(reg: int) -> int:
        return int(regs[reg - lm.MB2WS_SPY_WORK_PORT]) & 0xFFFF

    mode_word = at(lm.MB2WS_SPY_WORK_MODE)
    line_word = at(lm.MB2WS_SPY_LINE)
    slots: List[Dict[str, Any]] = []
    for n in range(lm.MB2WS_SPY_SLOT_COUNT):
        uid = at(lm.rgbw_spy_slot_reg(n, 0))
        slots.append(
            {
                "slot": n + 1,
                "uid": uid,
                "fc": at(lm.rgbw_spy_slot_reg(n, 1)),
                "reg": at(lm.rgbw_spy_slot_reg(n, 2)),
                "type": at(lm.rgbw_spy_slot_reg(n, 3)),
                "decimals": at(lm.rgbw_spy_slot_reg(n, 4)),
                "unit": lm.rgbw_spy_unpack_unit(
                    at(lm.rgbw_spy_slot_reg(n, 5)), at(lm.rgbw_spy_slot_reg(n, 6))
                ),
                "lo": lm.rgbw_u16_to_i16(at(lm.rgbw_spy_lim_reg(n, 0))),
                "hi": lm.rgbw_u16_to_i16(at(lm.rgbw_spy_lim_reg(n, 1))),
                # An empty slot (uid 0) polls nothing, so its live word is stale
                # noise — reported as None rather than as a reading.
                "live": (
                    lm.rgbw_spy_live_i32(
                        at(lm.MB2WS_SPY_LIVE_BASE + n * 2),
                        at(lm.MB2WS_SPY_LIVE_BASE + n * 2 + 1),
                    )
                    if uid
                    else None
                ),
            }
        )
    weather: List[Dict[str, Any]] = []
    for n in range(lm.MB2WS_WX_SPY_COUNT):
        uid, fc = lm.rgbw_wx_spy_unpack_uid_fc(at(lm.rgbw_wx_spy_reg(n, 0)))
        weather.append(
            {
                "field": n,
                "uid": uid,
                "fc": fc,
                "reg": at(lm.rgbw_wx_spy_reg(n, 1)),
            }
        )
    return {
        "port": at(lm.MB2WS_SPY_WORK_PORT),
        "mode": mode_word & 0xFF,
        "tap": bool(mode_word & lm.MB2WS_SPY_TAP_BIT),
        "baud": at(lm.MB2WS_SPY_BAUD),
        "parity": line_word & 0xFF,
        "stopbits": 2 if ((line_word >> 8) & 0xFF) == 2 else 1,
        "poll_ms": at(lm.MB2WS_SPY_POLL_MS),
        "timeout_ms": at(lm.MB2WS_SPY_TIMEOUT_MS),
        "stale_ms": at(lm.MB2WS_SPY_STALE_MS),
        "status": at(lm.MB2WS_SPY_STATUS),
        "slots": slots,
        "weather": weather,
    }

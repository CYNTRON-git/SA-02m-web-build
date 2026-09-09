# -*- coding: utf-8 -*-
"""MB2WS lookup tables (FX catalog, per-mode register sets, choice lists).

Imported only by led_mb2ws. The split mirrors sa02m_carel: the addresses and the
logic live in the sibling module, the bulk data lives here so neither file grows
past reading size.

Every table below is ported verbatim from the desktop flasher
(MR-02m-flasher, branch `feat/led-spy-and-picker`,
flasher_windows/module_profiles.py). Where the desktop's durable note
(.ai-dev/notes/led-mb2ws-map-0x0400.md in that repo) disagrees with its code, the
CODE is authoritative: the note says 81 effects, the firmware and the code have
82 — id 81 «Индикатор линии» (MB_SPY).
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# --- FX engine catalog (svc_fx_engine.h fx_mode_t) — Modbus FxId @405 ---------
# 82 modes, contiguous ids 0..81; Gyver aliases 128..209 → 0..81.
RGBW_FX_MODE_STATIC = 0
RGBW_FX_MODE_COUNT = 82
RGBW_FX_MODE_GYVER_BASE = 128  # 128..209 → 0..81 (firmware alias)

# The ids the window has to know by name — the modes with extra register surfaces.
RGBW_FX_MODE_CIRCADIAN = 32  # reads the soft clock 453/454
RGBW_FX_MODE_MATRIX_TEXT = 62  # marquee window 516..579 + TextLines 494
RGBW_FX_MODE_CLOCK = 63  # soft clock 453/454
RGBW_FX_MODE_WEATHER = 64  # soft clock + weather block 457..462
RGBW_FX_MODE_MB_SPY = 81  # line Spy/Master, holdings 640..695; TextLines 494

# FX group ids — the firmware's own 10 categories, adopted verbatim so the
# grouping has one home (svc_fx_engine.h block comments / MODBUS_VARIABLES
# reg 405 headers). Each is a CONTIGUOUS id block, so membership is a range
# lookup and cannot drift from firmware the way a per-id column could.
RGBW_FX_GROUP_BASE = "base"
RGBW_FX_GROUP_COLOR = "color"
RGBW_FX_GROUP_WATER = "water"
RGBW_FX_GROUP_FIRE = "fire"
RGBW_FX_GROUP_SKY = "sky"
RGBW_FX_GROUP_PHYS = "phys"
RGBW_FX_GROUP_NATURE = "nature"
RGBW_FX_GROUP_TEXT = "text"
RGBW_FX_GROUP_SIGN = "sign"
RGBW_FX_GROUP_SPECIAL = "special"

# Single source of truth: (group_id, i18n_key, first_id, last_id) in display
# order. The blocks tile 0..81 exactly (asserted in tests).
RGBW_FX_GROUP_RANGES: List[Tuple[str, str, int, int]] = [
    (RGBW_FX_GROUP_BASE, "rgbw_fx_grp_base", 0, 2),
    (RGBW_FX_GROUP_COLOR, "rgbw_fx_grp_color", 3, 12),
    (RGBW_FX_GROUP_WATER, "rgbw_fx_grp_water", 13, 25),
    (RGBW_FX_GROUP_FIRE, "rgbw_fx_grp_fire", 26, 29),
    (RGBW_FX_GROUP_SKY, "rgbw_fx_grp_sky", 30, 36),
    (RGBW_FX_GROUP_PHYS, "rgbw_fx_grp_phys", 37, 54),
    (RGBW_FX_GROUP_NATURE, "rgbw_fx_grp_nature", 55, 61),
    (RGBW_FX_GROUP_TEXT, "rgbw_fx_grp_text", 62, 65),
    (RGBW_FX_GROUP_SIGN, "rgbw_fx_grp_sign", 66, 75),
    (RGBW_FX_GROUP_SPECIAL, "rgbw_fx_grp_special", 76, 81),
]


def fx_group_for_id(fx_id: int) -> str:
    """Group id owning ``fx_id`` by range; out-of-range → the base group."""
    v = int(fx_id)
    for (gid, _key, first, last) in RGBW_FX_GROUP_RANGES:
        if first <= v <= last:
            return gid
    return RGBW_FX_GROUP_BASE


# Ordered catalog (fx_id, i18n_key, group_id) — derived from the ranges above so
# the id → group column can never disagree with them.
RGBW_FX_CATALOG: List[Tuple[int, str, str]] = [
    (i, "rgbw_fx_%02d" % i, fx_group_for_id(i)) for i in range(RGBW_FX_MODE_COUNT)
]

# Group display order + i18n label keys.
RGBW_FX_GROUP_ORDER: List[Tuple[str, str]] = [
    (gid, key) for (gid, key, _first, _last) in RGBW_FX_GROUP_RANGES
]

# Modes whose high byte firmware honours as a colour override. Derived from the
# SOURCE, not the doc: every call site of `fx_aux_color()` in svc_fx_engine.c
# (lines 537/687/1985/2524/3602/4462/5936/6010/6366-6368), each of which sits in
# a top-level `fx_run_*` renderer bound to these ids by the dispatch switch. The
# doc's "одноцветные режимы" sentence lists only 8 and MISSES 13 (Матрица: дождь)
# and 44 (Узоры) — trusting it would hide a control the device honours. Same
# lesson as CMD 3: read the handler, not the prose.
RGBW_FX_AUX_COLOR_MODES: Tuple[int, ...] = (13, 18, 40, 44, 62, 63, 64, 66, 67, 75)

# Modes whose renderer actually reads FxDensity (456). DERIVED FROM THE
# RENDERERS, not the doc: every function reaching `fx_density_count/gate/hue/s256`
# or the backing field `g_fx.scale` (svc_fx_engine.c:78 — FxDensity is stored
# there, `svc_fx_engine_set_density` at :7284), closed transitively over the call
# graph, then mapped to ids through the `fx_render` dispatch switch.
#
# The doc's sentence (MODBUS_VARIABLES reg 456) claims only Static 0, the signs
# 66..75 and the equalizer 43 ignore it and "everything else" reads it — that is
# WRONG in both directions: 55 of the 81 ids never touch the knob, and the doc's
# own example "6 колесо" does not (fx_run_sc_color_wheel, svc_fx_engine.c:4422,
# is 24 lines with no density path). Read the handler, not the prose.
RGBW_FX_DENSITY_MODES: Tuple[int, ...] = (
    3, 4, 5, 7, 8, 9, 11, 13, 14, 15, 16, 17, 18, 23,
    24, 33, 35, 36, 45, 52, 54, 55, 59, 60, 78, 80,
)

# Modes whose renderer reads FxAux (455) at all — same derivation. The union of
# the spec's non-plain low bytes and its honoured high bytes must equal this set;
# a test pins that, so the spec cannot drift from the renderers.
RGBW_FX_AUX_MODES: Tuple[int, ...] = (
    13, 18, 40, 43, 44, 51, 62, 63, 64, 66, 67, 70, 75,
)

# --- Reg-455 (FxAux) byte layouts --------------------------------------------
# Reg 455 is repacked PER MODE by firmware (MODBUS_VARIABLES.txt reg 455). These
# name the byte layouts; `rgbw_fx_aux_spec()` maps an fx id to one of them and
# the UI builds its widgets from that — no hand-packing at a call site.
RGBW_FX_AUX_LOW_VARIANT = "variant"  # plain 0..255 (firmware gives no per-mode meaning)
RGBW_FX_AUX_LOW_STYLE = "style"  # 62 MatrixText: text animation style 0..17
RGBW_FX_AUX_LOW_DIRECTION = "direction"  # 66/67 arrows: 0 right, 1 left, 2 up, 3 down
RGBW_FX_AUX_LOW_PERCENT = "percent"  # 43 equalizer: bar-field height 0..100 %
RGBW_FX_AUX_LOW_LEVEL = "level"  # 75 Level: bits 0-6 percent, bit 7 orientation
RGBW_FX_AUX_LOW_ESCORT = "escort"  # 51 Escort: bits 0-6 colour code, bit 7 reverse
RGBW_FX_AUX_LOW_FLAG = "flag"  # 70 Hazard: 1 = freeze the stripes

RGBW_FX_AUX_HIGH_COLOR = "color"  # colour override 0..6 (single-colour modes only)
RGBW_FX_AUX_HIGH_LENGTH = "length"  # 51 Escort: light-pool length in px (0 = 60)
RGBW_FX_AUX_HIGH_UNUSED = "unused"  # this mode does not read the high byte

MB2WS_FX_AUX_FLAG_BIT = 0x80  # bit 7 of the low byte for `level` / `escort`
MB2WS_FX_AUX_FIELD_MASK = 0x7F  # bits 0-6 of the low byte for `level` / `escort`
MB2WS_FX_AUX_ESCORT_LEN_DEFAULT = 60  # firmware substitutes 60 px when the high byte is 0
MB2WS_FX_AUX_STYLE_MAX = 17
MB2WS_FX_AUX_STYLE_MORPH = 15  # needs a '|' separator in the text, else it degrades to 11
MB2WS_FX_AUX_STYLE_CREDITS = 16  # the only style that also works with TextLines = 2

# FxAux colour-override enum (reg 455 high byte)
MB2WS_FX_AUX_COLOR_DEFAULT = 0
MB2WS_FX_AUX_COLOR_RED = 1
MB2WS_FX_AUX_COLOR_GREEN = 2
MB2WS_FX_AUX_COLOR_BLUE = 3
MB2WS_FX_AUX_COLOR_WHITE = 4
MB2WS_FX_AUX_COLOR_YELLOW = 5
MB2WS_FX_AUX_COLOR_ORANGE = 6
MB2WS_FX_AUX_COLOR_MAX = 6

RGBW_FX_AUX_STYLE_CHOICES: Tuple[Tuple[int, str], ...] = tuple(
    (i, "rgbw_fx_aux_style_%02d" % i) for i in range(MB2WS_FX_AUX_STYLE_MAX + 1)
)

RGBW_FX_AUX_DIRECTION_CHOICES: Tuple[Tuple[int, str], ...] = (
    (0, "rgbw_fx_aux_dir_right"),
    (1, "rgbw_fx_aux_dir_left"),
    (2, "rgbw_fx_aux_dir_up"),
    (3, "rgbw_fx_aux_dir_down"),
)

# Escort's own colour list: code 0 is WARM WHITE here, not "mode default".
RGBW_FX_AUX_ESCORT_COLOR_CHOICES: Tuple[Tuple[int, str], ...] = (
    (0, "rgbw_fx_aux_color_warm_white"),
    (1, "rgbw_fx_aux_color_red"),
    (2, "rgbw_fx_aux_color_green"),
    (3, "rgbw_fx_aux_color_blue"),
    (4, "rgbw_fx_aux_color_white"),
    (5, "rgbw_fx_aux_color_yellow"),
    (6, "rgbw_fx_aux_color_orange"),
)

RGBW_FX_AUX_COLOR_CHOICES: Tuple[Tuple[int, str], ...] = (
    (MB2WS_FX_AUX_COLOR_DEFAULT, "rgbw_fx_aux_color_default"),
    (MB2WS_FX_AUX_COLOR_RED, "rgbw_fx_aux_color_red"),
    (MB2WS_FX_AUX_COLOR_GREEN, "rgbw_fx_aux_color_green"),
    (MB2WS_FX_AUX_COLOR_BLUE, "rgbw_fx_aux_color_blue"),
    (MB2WS_FX_AUX_COLOR_WHITE, "rgbw_fx_aux_color_white"),
    (MB2WS_FX_AUX_COLOR_YELLOW, "rgbw_fx_aux_color_yellow"),
    (MB2WS_FX_AUX_COLOR_ORANGE, "rgbw_fx_aux_color_orange"),
)

# --- LedType (401) / ByteOrder (412) choice lists ----------------------------
MB2WS_LED_TYPE_WS2812_0 = 0
MB2WS_LED_TYPE_WS2812_1 = 1
MB2WS_LED_TYPE_SM16703 = 2
MB2WS_LED_TYPE_WS2811 = 3
MB2WS_LED_TYPE_WS2815 = 5
MB2WS_LED_TYPE_SK6812 = 6
MB2WS_LED_TYPE_APA102 = 7

# (LedType code, label) — firmware svc_mb_mb2ws.h / MODBUS_VARIABLES. Labels are
# part numbers, not prose: no i18n key, they read the same in either language.
RGBW_LED_TYPE_CHOICES: Tuple[Tuple[int, str], ...] = (
    (MB2WS_LED_TYPE_WS2812_0, "WS2812"),
    (MB2WS_LED_TYPE_WS2812_1, "WS2812 (timing 1)"),
    (MB2WS_LED_TYPE_SM16703, "SM16703"),
    (MB2WS_LED_TYPE_WS2811, "WS2811"),
    (MB2WS_LED_TYPE_WS2815, "WS2815"),
    (MB2WS_LED_TYPE_SK6812, "SK6812 RGBW"),
    (MB2WS_LED_TYPE_APA102, "APA102 / SK9822"),
)

# ByteOrder / LedColor (reg 412) — classic MB2WS LedColor table (doc §3)
MB2WS_BYTE_ORDER_RGB = 0
MB2WS_BYTE_ORDER_RBG = 1
MB2WS_BYTE_ORDER_GRB = 2
MB2WS_BYTE_ORDER_GBR = 3
MB2WS_BYTE_ORDER_BRG = 4
MB2WS_BYTE_ORDER_BGR = 5
MB2WS_BYTE_ORDER_RGBW = 6
MB2WS_BYTE_ORDER_RBGW = 7
MB2WS_BYTE_ORDER_GRBW = 8
MB2WS_BYTE_ORDER_GBRW = 9
MB2WS_BYTE_ORDER_BRGW = 10
MB2WS_BYTE_ORDER_BGRW = 11
MB2WS_BYTE_ORDER_MAX = 11

RGBW_BYTE_ORDER_CHOICES: Tuple[Tuple[int, str], ...] = (
    (MB2WS_BYTE_ORDER_RGB, "RGB"),
    (MB2WS_BYTE_ORDER_RBG, "RBG"),
    (MB2WS_BYTE_ORDER_GRB, "GRB"),
    (MB2WS_BYTE_ORDER_GBR, "GBR"),
    (MB2WS_BYTE_ORDER_BRG, "BRG"),
    (MB2WS_BYTE_ORDER_BGR, "BGR"),
    (MB2WS_BYTE_ORDER_RGBW, "RGBW"),
    (MB2WS_BYTE_ORDER_RBGW, "RBGW"),
    (MB2WS_BYTE_ORDER_GRBW, "GRBW"),
    (MB2WS_BYTE_ORDER_GBRW, "GBRW"),
    (MB2WS_BYTE_ORDER_BRGW, "BRGW"),
    (MB2WS_BYTE_ORDER_BGRW, "BGRW"),
)

# --- Power RGBW PWM strip modes — sequential 0…11 (svc_rgbw_pwm.h / holding 49)
RGBW_PWM_MODE_RGBW = 0
RGBW_PWM_MODE_WWWW = 1
RGBW_PWM_MODE_CCT_CCT = 2
RGBW_PWM_MODE_CCT_WW = 3
RGBW_PWM_MODE_2W_2W = 4
RGBW_PWM_MODE_4W = 5
RGBW_PWM_MODE_2W_WW = 6
RGBW_PWM_MODE_2W_CCT = 7
RGBW_PWM_MODE_CCT_2W = 8
RGBW_PWM_MODE_WW_CCT = 9
RGBW_PWM_MODE_WW_2W = 10
RGBW_PWM_MODE_2CCT = 11
# Legacy aliases
RGBW_PWM_MODE_4XW = RGBW_PWM_MODE_4W
RGBW_PWM_MODE_CCT = RGBW_PWM_MODE_CCT_WW

RGBW_PWM_MODE_CODES: Tuple[int, ...] = (
    RGBW_PWM_MODE_RGBW,
    RGBW_PWM_MODE_WWWW,
    RGBW_PWM_MODE_CCT_CCT,
    RGBW_PWM_MODE_CCT_WW,
    RGBW_PWM_MODE_2W_2W,
    RGBW_PWM_MODE_4W,
    RGBW_PWM_MODE_2W_WW,
    RGBW_PWM_MODE_2W_CCT,
    RGBW_PWM_MODE_CCT_2W,
    RGBW_PWM_MODE_WW_CCT,
    RGBW_PWM_MODE_WW_2W,
    RGBW_PWM_MODE_2CCT,
)

# (PWM strip_mode 0…11, i18n key) — holding 49 / svc_rgbw_pwm.h.
RGBW_PWM_MODE_CHOICES: Tuple[Tuple[int, str], ...] = (
    (RGBW_PWM_MODE_RGBW, "rgbw_pwm_mode_rgbw"),
    (RGBW_PWM_MODE_WWWW, "rgbw_pwm_mode_wwww"),
    (RGBW_PWM_MODE_CCT_CCT, "rgbw_pwm_mode_cct_cct"),
    (RGBW_PWM_MODE_CCT_WW, "rgbw_pwm_mode_cct_ww"),
    (RGBW_PWM_MODE_2W_2W, "rgbw_pwm_mode_2w_2w"),
    (RGBW_PWM_MODE_4W, "rgbw_pwm_mode_4w"),
    (RGBW_PWM_MODE_2W_WW, "rgbw_pwm_mode_2w_ww"),
    (RGBW_PWM_MODE_2W_CCT, "rgbw_pwm_mode_2w_cct"),
    (RGBW_PWM_MODE_CCT_2W, "rgbw_pwm_mode_cct_2w"),
    (RGBW_PWM_MODE_WW_CCT, "rgbw_pwm_mode_ww_cct"),
    (RGBW_PWM_MODE_WW_2W, "rgbw_pwm_mode_ww_2w"),
    (RGBW_PWM_MODE_2CCT, "rgbw_pwm_mode_2cct"),
)

# i18n keys for the silk R/G/B/W labels per strip mode. A mode absent here uses
# the RGBW row (`rgbw_pwm_channel_label_keys` falls back to it).
RGBW_PWM_CHANNEL_LABEL_KEYS: Dict[int, Tuple[str, str, str, str]] = {
    RGBW_PWM_MODE_RGBW: ("rgbw_pwm_ch_r", "rgbw_pwm_ch_g", "rgbw_pwm_ch_b", "rgbw_pwm_ch_w"),
    RGBW_PWM_MODE_WWWW: ("rgbw_pwm_ch_w1", "rgbw_pwm_ch_w2", "rgbw_pwm_ch_w3", "rgbw_pwm_ch_w4"),
    RGBW_PWM_MODE_4W: (
        "rgbw_pwm_ch_4w", "rgbw_pwm_ch_4w_par", "rgbw_pwm_ch_4w_par", "rgbw_pwm_ch_4w_par",
    ),
    RGBW_PWM_MODE_CCT_CCT: (
        "rgbw_pwm_ch_ww1", "rgbw_pwm_ch_cw1", "rgbw_pwm_ch_ww2", "rgbw_pwm_ch_cw2",
    ),
    RGBW_PWM_MODE_2CCT: (
        "rgbw_pwm_ch_ww1", "rgbw_pwm_ch_cw1", "rgbw_pwm_ch_ww2", "rgbw_pwm_ch_cw2",
    ),
    RGBW_PWM_MODE_CCT_WW: ("rgbw_pwm_ch_ww", "rgbw_pwm_ch_cw", "rgbw_pwm_ch_w3", "rgbw_pwm_ch_w4"),
    RGBW_PWM_MODE_2W_2W: (
        "rgbw_pwm_ch_2w_a", "rgbw_pwm_ch_2w_a_par", "rgbw_pwm_ch_2w_b", "rgbw_pwm_ch_2w_b_par",
    ),
    RGBW_PWM_MODE_2W_WW: (
        "rgbw_pwm_ch_2w_a", "rgbw_pwm_ch_2w_a_par", "rgbw_pwm_ch_w3", "rgbw_pwm_ch_w4",
    ),
    RGBW_PWM_MODE_WW_2W: (
        "rgbw_pwm_ch_w1", "rgbw_pwm_ch_w2", "rgbw_pwm_ch_2w_b", "rgbw_pwm_ch_2w_b_par",
    ),
    RGBW_PWM_MODE_CCT_2W: (
        "rgbw_pwm_ch_ww", "rgbw_pwm_ch_cw", "rgbw_pwm_ch_2w_b", "rgbw_pwm_ch_2w_b_par",
    ),
    RGBW_PWM_MODE_2W_CCT: (
        "rgbw_pwm_ch_2w_a", "rgbw_pwm_ch_2w_a_par", "rgbw_pwm_ch_ww", "rgbw_pwm_ch_cw",
    ),
    RGBW_PWM_MODE_WW_CCT: ("rgbw_pwm_ch_w1", "rgbw_pwm_ch_w2", "rgbw_pwm_ch_ww", "rgbw_pwm_ch_cw"),
}

# Which silk channels show controls (a parallel slave channel is hidden). A mode
# absent here shows all four.
RGBW_PWM_CHANNEL_VISIBLE: Dict[int, Tuple[bool, bool, bool, bool]] = {
    RGBW_PWM_MODE_4W: (True, False, False, False),
    RGBW_PWM_MODE_2W_2W: (True, False, True, False),
    RGBW_PWM_MODE_2W_WW: (True, False, True, True),
    RGBW_PWM_MODE_2W_CCT: (True, False, True, True),
    RGBW_PWM_MODE_WW_2W: (True, True, True, False),
    RGBW_PWM_MODE_CCT_2W: (True, True, True, False),
}

# --- UI taxonomy (radios / combos) — not raw register codes ------------------
RGBW_LINE_UI_SINGLE = "single_ws"
RGBW_LINE_UI_DUAL = "dual_ws"
RGBW_LINE_UI_APA102 = "apa102"

RGBW_LINE_UI_CHOICES: Tuple[Tuple[str, str], ...] = (
    (RGBW_LINE_UI_SINGLE, "rgbw_line_single"),
    (RGBW_LINE_UI_DUAL, "rgbw_line_dual"),
    (RGBW_LINE_UI_APA102, "rgbw_line_apa102"),
)

RGBW_SCENE_UI_POOL = "pool"
RGBW_SCENE_UI_FX = "fx"
RGBW_SCENE_UI_FLASH = "flash"

RGBW_SCENE_UI_CHOICES: Tuple[Tuple[str, str], ...] = (
    (RGBW_SCENE_UI_POOL, "rgbw_scene_pool"),
    (RGBW_SCENE_UI_FX, "rgbw_scene_fx"),
    (RGBW_SCENE_UI_FLASH, "rgbw_scene_flash"),
)

# --- Matrix panel types (flasher policy, not a firmware limit) ---------------
# Firmware accepts ANY width 1..64 and height 0 (=16) or 8..32
# (svc_mb_mb2ws.c:263-274, led_matrix_core.h:28-36): 7×9 is ACCEPTED, no
# exception, and the engine renders it, degrading gracefully (2× text and the
# 10×14 clock glyphs need height ≥ 14). So this list is the FLASHER's policy:
# real panel types only, no free numeric entry (Operator ruling 2026-08-26).
#
# **Type and count are two homes.** The cascade count lives in the TileCount
# control (reg 418 bits 8-11), so "16×16 ×2" is a type plus a count and never a
# row here; `rgbw_matrix_type_writes()` joins them into the register writes.
RGBW_MATRIX_TYPES: Tuple[Tuple[str, int, int], ...] = (
    ("rgbw_mx_8x8", 8, 8),
    ("rgbw_mx_8x16", 8, 16),
    ("rgbw_mx_16x8", 16, 8),
    ("rgbw_mx_16x16", 16, 16),
    ("rgbw_mx_8x32", 8, 32),
    ("rgbw_mx_32x8", 32, 8),
    ("rgbw_mx_16x32", 16, 32),
    ("rgbw_mx_32x16", 32, 16),
    ("rgbw_mx_32x32", 32, 32),
    ("rgbw_mx_64x16", 64, 16),
)

RGBW_TILE_MODE_CHOICES: Tuple[Tuple[int, str], ...] = (
    (0, "rgbw_tile_mode_span"),
    (1, "rgbw_tile_mode_replicate"),
    (2, "rgbw_tile_mode_mirror"),
)

# --- Spy / Master line indicator (FX 81) choice lists ------------------------
MB2WS_SPY_FC_HOLDING = 3
MB2WS_SPY_FC_INPUT = 4
MB2WS_SPY_FC_WRITE_SINGLE = 6
MB2WS_SPY_FC_WRITE_MULTI = 16

MB2WS_SPY_TYPE_INT16 = 0
MB2WS_SPY_TYPE_UINT16 = 1
MB2WS_SPY_TYPE_INT32_AB = 2
MB2WS_SPY_TYPE_UINT32_AB = 3
MB2WS_SPY_TYPE_INT32_CDAB = 4
MB2WS_SPY_TYPE_UINT32_CDAB = 5
MB2WS_SPY_TYPE_FLOAT_AB = 6
MB2WS_SPY_TYPE_FLOAT_CDAB = 7
MB2WS_SPY_TYPE_FLOAT_BADC = 8
MB2WS_SPY_TYPE_FLOAT_DCBA = 9
MB2WS_SPY_TYPE_TIME_HH = 10
MB2WS_SPY_TYPE_TIME_MM = 11
MB2WS_SPY_TYPE_TIME_SS = 12
MB2WS_SPY_TYPE_MAX = 12

# (holding-110 code, label) — WorkPort baud, same codes as family reg 110.
RGBW_SPY_BAUD_CHOICES: Tuple[Tuple[int, str], ...] = (
    (12, "1200"),
    (24, "2400"),
    (48, "4800"),
    (96, "9600"),
    (192, "19200"),
    (384, "38400"),
    (576, "57600"),
    (1152, "115200"),
)

RGBW_SPY_FC_CHOICES: Tuple[Tuple[int, str], ...] = (
    (MB2WS_SPY_FC_HOLDING, "rgbw_spy_fc_03"),
    (MB2WS_SPY_FC_INPUT, "rgbw_spy_fc_04"),
    (MB2WS_SPY_FC_WRITE_SINGLE, "rgbw_spy_fc_06"),
    (MB2WS_SPY_FC_WRITE_MULTI, "rgbw_spy_fc_16"),
)

RGBW_SPY_TYPE_CHOICES: Tuple[Tuple[int, str], ...] = (
    (MB2WS_SPY_TYPE_INT16, "rgbw_spy_type_int16"),
    (MB2WS_SPY_TYPE_UINT16, "rgbw_spy_type_uint16"),
    (MB2WS_SPY_TYPE_INT32_AB, "rgbw_spy_type_int32_ab"),
    (MB2WS_SPY_TYPE_UINT32_AB, "rgbw_spy_type_uint32_ab"),
    (MB2WS_SPY_TYPE_INT32_CDAB, "rgbw_spy_type_int32_cdab"),
    (MB2WS_SPY_TYPE_UINT32_CDAB, "rgbw_spy_type_uint32_cdab"),
    (MB2WS_SPY_TYPE_FLOAT_AB, "rgbw_spy_type_float_ab"),
    (MB2WS_SPY_TYPE_FLOAT_CDAB, "rgbw_spy_type_float_cdab"),
    (MB2WS_SPY_TYPE_FLOAT_BADC, "rgbw_spy_type_float_badc"),
    (MB2WS_SPY_TYPE_FLOAT_DCBA, "rgbw_spy_type_float_dcba"),
    (MB2WS_SPY_TYPE_TIME_HH, "rgbw_spy_type_time_hh"),
    (MB2WS_SPY_TYPE_TIME_MM, "rgbw_spy_type_time_mm"),
    (MB2WS_SPY_TYPE_TIME_SS, "rgbw_spy_type_time_ss"),
)

RGBW_SPY_UNIT_PRESETS: Tuple[str, ...] = ("", "°C", "%", "V", "A", "W", "Hz", "bar")

# i18n keys for the weather-listen fields 0..6 (HH..PRESS), in wire order.
RGBW_WX_SPY_FIELD_KEYS: Tuple[str, ...] = (
    "rgbw_wx_spy_hh",
    "rgbw_wx_spy_mm",
    "rgbw_wx_spy_ss",
    "rgbw_wx_spy_date",
    "rgbw_wx_spy_temp",
    "rgbw_wx_spy_hum",
    "rgbw_wx_spy_press",
)

# --- Signature aliases -------------------------------------------------------
# Holding 290 / scan label forms an LED strip answers with. The ONE home of the
# token list: the flasher's module_profiles delegates here rather than restating
# it, the same seam sa02m_carel uses for the Carel app ids.
LED_SIGNATURE_ALIASES: Tuple[str, ...] = ("RGBW_WS2812", "RGBWWS2812", "RGBW", "LED")

# Only these two may also match as a PREFIX (a firmware suffix like
# «RGBW_WS2812_v2»). "RGBW" and "LED" are exact-match only.
#
# DELIBERATE DEVIATION from the desktop, which prefix-matches "LED" as well
# (`module_profiles.code_from_signature`: `n.startswith("LED")`). On this board
# that rule is wrong and measurably so: `tests/test_flash_route.py` carries the
# Wiren Board signature «ledGe», which normalises to "LEDGE" and would be typed
# as a strip — losing a real WB device its .wbfw flash route. Three letters is
# simply too short a prefix for a signature field that also holds foreign model
# strings. The desktop never hit it because it has no such route test; the
# regression is pinned here by `test_a_foreign_signature_containing_led_is_not_a_strip`.
LED_SIGNATURE_PREFIXES: Tuple[str, ...] = ("RGBW_WS2812", "RGBWWS2812")

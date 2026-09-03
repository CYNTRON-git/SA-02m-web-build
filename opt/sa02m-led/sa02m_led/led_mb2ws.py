# -*- coding: utf-8 -*-
"""MR-02m LED strip (RGBW_WS2812, type 120) — MB2WS register map, base 400.

One home for the strip's register semantics on the board: the flasher daemon
(scan + config window) and the Modbus-MQTT bridge both import this package,
neither copies the map. Ported from the desktop flasher (MR-02m-flasher, branch
`feat/led-spy-and-picker`, flasher_windows/module_profiles.py, the MB2WS block
and the RGBW_WS2812 product low map) with three edits:

  * the desktop's i18n calls are dropped — this package returns i18n KEYS, the
    web UI resolves them (`www/.../i18n.js`), so nothing here imports a toolkit;
  * `rgbw_pwm_wiring_svgs()` is dropped — it names files under the desktop's own
    `Подключения/` asset directory, which does not exist on the board;
  * the lock bracket, duplicated in both desktop panels
    (`module_config_rgbw_tk.py` / `qt_app/.../specialized_pages_rgbw.py`), is
    hoisted here as `rgbw_lock_bracket_writes()` so the unlock→ordered→lock rule
    has one home on this side.

Firmware pin: `MB2WS_MAP_VERSION = 0x0400`, RGBW_WS2812 1.0.2.3 (`5bd897c`).
Where the desktop's durable note disagrees with its code the CODE wins — the
note says 81 effects, firmware and code have 82 (id 81 «Индикатор линии»).
"""
from __future__ import annotations

from colorsys import hsv_to_rgb, rgb_to_hsv
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

# The lookup tables are re-exported deliberately: `led_mb2ws` is the ONE
# import a consumer needs (`from sa02m_led import led_mb2ws as lm`), and a
# hand-maintained mirror list here would be a second home for every table
# name — it would silently go stale the day the map gains a row.
from .led_mb2ws_map import *  # noqa: F401,F403

# ---------------------------------------------------------------------------
# RGBW_WS2812 / MB2WS (unified map base 400+; firmware MB2WS_MAP_VERSION 0x0400)
# Offsets match the former Port-B-only 0.. map shifted by MB2WS_REG_BASE.
# ---------------------------------------------------------------------------

RGBW_WS2812_TYPE_CODE = 120  # Input / Holding reg 0

MB2WS_REG_BASE = 400

MB2WS_LED_COUNT0 = 400
MB2WS_LED_TYPE = 401
MB2WS_RENDER_SOURCE = 402
MB2WS_PIXEL_FORMAT = 403
MB2WS_AUTO_REFRESH = 404
MB2WS_FX_ID = 405
MB2WS_FX_SPEED = 406
MB2WS_FX_PARAM = 407  # Fx brightness 0…255 → svc_fx_engine_set_brightness
MB2WS_STARTUP_MODE = 408
MB2WS_OPTIONS = 409
MB2WS_LOCK = 410
MB2WS_UNLOCK_KEY = 0x10C8
MB2WS_GAMMA = 411  # range UNVERIFIED in firmware ("гамма / curve") — UI keeps the 0..255 superset.
MB2WS_BYTE_ORDER = 412
MB2WS_LINE_MODE = 413
MB2WS_LED_COUNT1 = 414
MB2WS_FLASH_SLOT = 415
MB2WS_PLAY_CTRL = 416

# New-register block (0x0400 map). Lock (410) gates 400–419 only; the always-
# writable set (453–466, 494, 516–579) is NOT lock-gated — see per-reg notes.
# EEPROM persistence (svc_mb_mb2ws_addr_is_persisted_config, svc_mb_mb2ws.c:1061-1091):
# 455/456/463–466/494 ARE persisted since firmware 1.0.2.2 — their values survive a
# power-cycle. The text window 516..579 is NOT: it is pixel-data class, explicitly
# never persisted (firmware docs/contracts/modbus-rgbw.md:99).
MB2WS_MATRIX_WIDTH = 417  # PACKED (1.0.2.3): low=width 1..64, high=height 0→16/8..32.
MB2WS_MATRIX_LAYOUT = 418  # bits 0-3 wiring, 4-6 TileMode, 8-11 TileCount (see masks below).
MB2WS_CH2_MODE = 419  # 2nd output, valid only when LineMode=1 (DUAL_WS). Lock-gated.

# The lock-gated settings block, as one span. Reg 410 itself is the one settings
# register writable while locked (it IS the lock), so it is excluded by
# `rgbw_reg_is_lock_gated`. PlayCtrl 416 sits INSIDE this span — that is the
# whole point of the span being a range and not a hand-listed set (see
# `rgbw_lock_bracket_writes`).
MB2WS_LOCK_GATED_FIRST = MB2WS_LED_COUNT0  # 400
MB2WS_LOCK_GATED_LAST = MB2WS_CH2_MODE  # 419

MB2WS_TOD_HOURS = 453  # soft clock hours (modes 32/63/64). Always writable.
MB2WS_TOD_MINUTES = 454  # soft clock minutes. Always writable.
MB2WS_FX_AUX = 455  # (color<<8)|variant; low=variant/direction, high=color override. Persisted.
MB2WS_FX_DENSITY = 456  # 0..255, def 128. Always writable, persisted.
MB2WS_WX_DATE = 457  # (day<<8)|month, 0=unset (mode 64).
MB2WS_WX_TEMP = 458  # int16 0.1 °C, 0x8000=unset.
MB2WS_WX_HUM = 459  # 0.1 %RH 0..1000, 0xFFFF=unset.
MB2WS_WX_PRESS = 461  # 0.1 mmHg 4000..8500, 0=unset.
MB2WS_WX_YEAR = 462  # 2000..2199, 0=unset.
MB2WS_TEXT_COLOR1 = 463  # RGB565 glyph color 1 (modes 62/64), 0=default. Persisted.
MB2WS_TEXT_COLOR2 = 464  # RGB565 glyph color 2 (modes 62/64), 0=default. Persisted.
MB2WS_TEXT_BG1 = 465  # RGB565 background band 1 (modes 62/64), 0=default. Persisted.
MB2WS_TEXT_BG2 = 466  # RGB565 background band 2 (modes 62/64), 0=default. Persisted.
MB2WS_WX_TEMP_COLOR = 467  # RGB565 temperature colour (mode 64), 0=amber default. Volatile.
MB2WS_TEXT_LINES = 494  # 0/1 = one line, 2 = two 5×7 lines (modes 62 and 64). Always writable, persisted.

# Mode-data read-back block: 453..467 is CONTIGUOUSLY mapped (mb2ws_is_mapped_addr,
# svc_mb_mb2ws.c:391-406) — Scale 460 sits inside it — so ONE FC03 of 15 regs is
# legal. An unmapped address anywhere in a block fails the WHOLE read (:944-948):
# never widen past 467; 494 and 516..579 need their own reads.
MB2WS_MODE_DATA_BASE = MB2WS_TOD_HOURS
MB2WS_MODE_DATA_COUNT = (MB2WS_WX_TEMP_COLOR - MB2WS_TOD_HOURS) + 1

# Marquee window 516..579 (svc_mb_mb2ws.h:110-126). It was 495..510 before
# firmware 1.0.2.2 — that span OVERLAPPED the family safe-AO block 503..506, so a
# text write silently rewrote the module's safe-state PWM values (drove them to
# 100 %). Never write text below 516.
MB2WS_TEXT_BASE = 516  # 64 regs 516..579, 2 chars/reg, high byte = first char. Volatile.
MB2WS_TEXT_REG_COUNT = 64
MB2WS_TEXT_MAX_CHARS = 128  # = MB2WS_TEXT_REG_COUNT * 2 (firmware MB2WS_TEXT_MAX_CHARS)
MB2WS_TEXT_LEGACY_BASE = 495  # the destructive pre-1.0.2.2 base — kept only to be refused

# Spy/Master line indicator (FX 81) — holdings 640..695. Weather listen
# (FX 64) 696..709. Lock-class like 413 except status 647 and live 680..687
# (always readable). Weather binds are RAM-only.
MB2WS_SPY_WORK_PORT = 640  # 0 off, 1 Port A (USART2), 2 Port B (USART1)
MB2WS_SPY_WORK_MODE = 641  # low: 0 off / 1 Spy / 2 Master; bit8 = tap-on-slave
MB2WS_SPY_BAUD = 642  # same codes as holding 110 (96 = 9600)
MB2WS_SPY_LINE = 643  # low = parity 0/1/2, high = stop 1/2
MB2WS_SPY_POLL_MS = 644
MB2WS_SPY_TIMEOUT_MS = 645
MB2WS_SPY_STALE_MS = 646
MB2WS_SPY_STATUS = 647  # RO
MB2WS_SPY_SLOT_BASE = 648
MB2WS_SPY_SLOT_STRIDE = 8
MB2WS_SPY_SLOT_COUNT = 4
MB2WS_SPY_SLOT_LAST = 679
MB2WS_SPY_LIVE_BASE = 680
MB2WS_SPY_LIVE_LAST = 687
MB2WS_SPY_LIM_BASE = 688  # slot n: lo at 688+n*2, hi at +1 (int16)
MB2WS_SPY_LIM_LAST = 695
MB2WS_WX_SPY_BASE = 696
MB2WS_WX_SPY_FIELD_REGS = 2
MB2WS_WX_SPY_COUNT = 7  # HH, MM, SS, DATE, TEMP, HUM, PRESS
MB2WS_WX_SPY_LAST = 709
MB2WS_SPY_LAST = MB2WS_WX_SPY_LAST
MB2WS_SPY_REG_COUNT = (MB2WS_SPY_LAST - MB2WS_SPY_WORK_PORT) + 1
MB2WS_SPY_MODE_OFF = 0
MB2WS_SPY_MODE_SPY = 1
MB2WS_SPY_MODE_MASTER = 2
MB2WS_SPY_TAP_BIT = 0x0100

# Ch2Mode enum (reg 419)
MB2WS_CH2_OFF = 0
MB2WS_CH2_SYNC = 1
MB2WS_CH2_INDEPENDENT = 2
MB2WS_CH2_MIRROR = 3
MB2WS_CH2_CONTINUATION = 4

MB2WS_FX_DENSITY_DEFAULT = 128
MB2WS_WX_TEMP_UNSET = 0x8000
MB2WS_WX_HUM_UNSET = 0xFFFF
# Soft clock 453/454: 0xFFFF in BOTH = "time not set" — the factory state and the
# state after every power-cycle (453/454 are not in EEPROM). The clock runs only
# while both are set, writing 0xFFFF to EITHER resets both, and any other
# out-of-range value answers exception 3. Reading returns the RUNNING time.
MB2WS_TOD_UNSET = 0xFFFF
MB2WS_WX_DATE_UNSET = 0
MB2WS_WX_PRESS_UNSET = 0
MB2WS_WX_YEAR_UNSET = 0
MB2WS_WX_YEAR_MIN = 2000
MB2WS_WX_YEAR_MAX = 2199

# MatrixLayout (reg 418) wiring bits 0-3 — firmware led_matrix_core.h, mask 0x000F.
MB2WS_MATRIX_LAYOUT_PROGRESSIVE = 0x01  # 0=serpentine / 1=progressive
MB2WS_MATRIX_LAYOUT_ORIGIN_BOTTOM = 0x02  # 0=top-start / 1=bottom-start
MB2WS_MATRIX_LAYOUT_MIRROR_X = 0x04  # canvas x right-to-left
MB2WS_MATRIX_LAYOUT_SWAP_XY = 0x08  # column-major wiring (runs along Y)
MB2WS_MATRIX_LAYOUT_WIRING_MASK = 0x000F
# Reg 418 also carries the tiling fields (svc_mb_mb2ws.h:294-304). A caller
# WITHOUT their UI state still preserves them via the prior-based path — see
# rgbw_matrix_layout_compose() / rgbw_matrix_layout_write_value_ex().
MB2WS_MATRIX_LAYOUT_TILEMODE_MASK = 0x0070  # bits 4-6: 0 span / 1 replicate / 2 mirror
MB2WS_MATRIX_LAYOUT_TILEMODE_SHIFT = 4
MB2WS_MATRIX_LAYOUT_TILEMODE_MAX = 2  # write 3..7 → firmware exception 3
MB2WS_MATRIX_LAYOUT_TILECOUNT_MASK = 0x0F00  # bits 8-11: 0 auto / 1..4 explicit
MB2WS_MATRIX_LAYOUT_TILECOUNT_SHIFT = 8
MB2WS_MATRIX_LAYOUT_TILECOUNT_MAX = 4  # write 5..15 → firmware exception 3
MB2WS_MATRIX_LAYOUT_PRESERVE_MASK = (
    MB2WS_MATRIX_LAYOUT_TILEMODE_MASK | MB2WS_MATRIX_LAYOUT_TILECOUNT_MASK
)

MB2WS_CMD = 430
MB2WS_CMD_STATUS = 431
# Scale: accepted and read back, but NEVER consumed by rendering — the UI control
# is hidden for that reason. See rgbw_scale_is_honoured() for the evidence.
MB2WS_SCALE = 460
MB2WS_PIXEL_POOL_BASE = 1100

# Firmware pixel-count caps (contract modbus-rgbw.md; shared 3072-byte pool).
# LedCount0 (400): 1..1024 RGB / 1..400 RGBW. LedCount1 (414, DUAL_WS):
# 0..512 RGB / 0..384 RGBW. Combined pool: (LedCount0+LedCount1)*bpp <= 3072.
MB2WS_MAX_PX0_RGB = 1024
MB2WS_MAX_PX0_RGBW = 400
MB2WS_MAX_PX1_RGB = 512
MB2WS_MAX_PX1_RGBW = 384
MB2WS_POOL_BYTES = 3072

MB2WS_CMD_NONE = 0
MB2WS_CMD_REFRESH = 1
MB2WS_CMD_CLEAR_POOL = 2
# CMD 3 is accepted by the range check but REFUSED by the handler: firmware sets
# last_error = 5 / CMD_ST_ERROR and stores nothing ("SAVE_FLASH deprecated",
# svc_mb_mb2ws.c:897-901). The upload path is holding 4000+ (EXFX protocol).
# Listed so the map reads complete — NEVER issue it, and never offer it in a UI.
# `rgbw_cmd_is_allowed()` is the gate; RGBW_CMD_ALLOWED is the allow-list.
MB2WS_CMD_SAVE_FLASH_DEPRECATED = 3
MB2WS_CMD_LOAD_FLASH = 4

RGBW_CMD_ALLOWED: Tuple[int, ...] = (
    MB2WS_CMD_NONE,
    MB2WS_CMD_REFRESH,
    MB2WS_CMD_CLEAR_POOL,
    MB2WS_CMD_LOAD_FLASH,
)

MB2WS_PLAY_STOP = 0
MB2WS_PLAY_PLAY = 1
MB2WS_PLAY_PAUSE = 2

MB2WS_LINE_SINGLE_WS = 0
MB2WS_LINE_DUAL_WS = 1
MB2WS_LINE_SPI_CLOCKED = 2

MB2WS_RENDER_POOL = 0
MB2WS_RENDER_FX = 1
MB2WS_RENDER_FLASH = 2

MB2WS_OPT_LOOP = 0x0001

# --- TextLines (reg 494) -----------------------------------------------------
# Firmware branches on `text_lines == 2` ALONE (svc_fx_engine.c:551), so 0 and 1
# are the same single-line layout. The window therefore offers TWO named options,
# not the three raw values the register accepts.
MB2WS_TEXT_LINES_SINGLE = 1
MB2WS_TEXT_LINES_DOUBLE = 2
# 2x text metrics (svc_fx_engine.c:606-609): glyph 10 px wide (FONT_W 5 × scale 2)
# plus a 3 px inter-letter gap = 13 px advance per character.
MB2WS_TEXT_FONT_W_PX = 5
MB2WS_TEXT_GAP_2X_PX = 3
MB2WS_TEXT_ADVANCE_2X_PX = (MB2WS_TEXT_FONT_W_PX * 2) + MB2WS_TEXT_GAP_2X_PX
# The canvas width the user-facing 2x hint quotes: the default 4×16×16 chain.
MB2WS_TEXT_REF_CANVAS_W_PX = 64

# --- Port-A PWM / DI-ctrl / logical dimmer — product low map -----------------
# (RGBW_WS2812 MODBUS_VARIABLES). Relocated off legacy holes: dimmer
# 1500–1579→50–101, DI cfg 1400–1499→135–186, press counters 364/464…→220–237
# (svc_rgbw_dimmer.h / svc_rgbw_di_ctrl.h).
RGBW_PWM_HOLDING_BASE = 33  # R,G,B,W permille 0..1000
RGBW_PWM_CHANNELS = 4
RGBW_PWM_MIRROR_BASE = 1  # write 0=off, >0 enable+brightness
RGBW_PWM_SAFE_BASE = 503  # family safe-state AO block 503..506 — see MB2WS_TEXT_BASE
RGBW_PWM_SAFE_LAST = 506
RGBW_COIL_SAFE_BASE = 600
RGBW_PWM_STRIP_MODE_HOLDING = 49  # sequential strip mode 0…11
RGBW_PWM_PERMILLE_MAX = 1000
RGBW_DIMMER_HOLD_FIRST = 50
RGBW_DIMMER_HOLD_LAST = 101
# Dimmer named regs (svc_rgbw_dimmer.h) — contiguous 50…101
RGBW_DIM_FADE_MS = 50
RGBW_DIM_PWM_FREQ_HZ = 51
RGBW_DIM_CCT_NORM = 52
RGBW_DIM_SAFE_DI = 53
RGBW_DIM_ENC_BELOW_MIN = 54
RGBW_DIM_POWER_ON0 = 55  # ..58
RGBW_DIM_POWER_LVL0 = 59  # ..62
RGBW_DIM_MIN0 = 63  # ..66
RGBW_DIM_MAX0 = 67  # ..70
RGBW_DIM_HUE = 71
RGBW_DIM_SAT = 72
RGBW_DIM_VAL = 73
RGBW_DIM_CCT1_TEMP = 74
RGBW_DIM_CCT1_BRI = 75
RGBW_DIM_CCT2_TEMP = 76
RGBW_DIM_CCT2_BRI = 77
RGBW_DIM_FADE_UP_MS = 78
RGBW_DIM_FADE_DN_MS = 79
RGBW_DIM_AC_FIRST = 80  # Auto-Change pairs → 101
RGBW_DI_COUNT = 4
RGBW_DI_MODE_BASE = 135
RGBW_DI_ACT_SHORT_BASE = 139
RGBW_DI_ACT_LONG_BASE = 143
RGBW_DI_ACT_DOUBLE_BASE = 147
RGBW_DI_ACT_SHORTLONG_BASE = 151
RGBW_DI_LONG_MS_BASE = 155
RGBW_DI_DOUBLE_MS_BASE = 159
RGBW_DI_DEBOUNCE_BASE = 163
RGBW_DI_RATE_BASE = 167
RGBW_DI_STEP_SHORT_BASE = 171
RGBW_DI_STEP_DBL_BASE = 175
RGBW_DI_ENC_TYPE_BASE = 179
RGBW_DI_ENC_TARGET_BASE = 181
RGBW_DI_ENC_STEP_BASE = 183
RGBW_DI_ENC_PULSES_BASE = 185
RGBW_DI_HOLD_LAST = 186
RGBW_DI_CNT_SHORT_BASE = 220
RGBW_DI_CNT_LONG_BASE = 224
RGBW_DI_CNT_DOUBLE_BASE = 228
RGBW_DI_CNT_SHORTLONG_BASE = 232
RGBW_DI_ENC_STEPS_BASE = 236
RGBW_DI_CNT_LAST = 237
RGBW_IREG_CURRENT_BASE = 37  # mA R/G/B/W
RGBW_IREG_NTC = 131
RGBW_IREG_VLED = 132


def mb2ws_reg(offset: int) -> int:
    """Legacy Port-B offset 0.. → unified holding address (base 400)."""
    return int(MB2WS_REG_BASE) + int(offset)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def normalize_sig(signature: str) -> str:
    """Signature comparison form: trimmed, upper-cased, spaces removed."""
    return (signature or "").strip().upper().replace(" ", "")


def signature_looks_like_led(signature: str) -> bool:
    """True for an LED-strip signature (RGBW_WS2812 / RGBWWS2812 / RGBW / LED).

    The ONE home of the alias list (`LED_SIGNATURE_ALIASES`); the flasher's
    module_profiles delegates here rather than restating it, the same seam
    sa02m_carel uses for the Carel app ids.

    EXACT for all four aliases, PREFIX only for the two long ones
    (``LED_SIGNATURE_PREFIXES``) — never a substring, and never a three-letter
    prefix. The alias list and the prefix subset both live in
    ``led_mb2ws_map``, which records why "LED" is exact-only: the board's own
    Wiren Board route test carries the signature «ledGe».
    """
    n = normalize_sig(signature)
    if not n or n in ("—", "-", "NONE", "?"):
        return False
    if n in LED_SIGNATURE_ALIASES:
        return True
    for prefix in LED_SIGNATURE_PREFIXES:
        if n.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# Lock (reg 410) — the settings-block bracket
# ---------------------------------------------------------------------------


def rgbw_reg_is_lock_gated(reg: int) -> bool:
    """True when writing ``reg`` needs the 410 unlock first.

    The lock gates the settings block 400..419 IN FULL. Reg 410 itself is the one
    settings register writable while locked — it is the lock.

    **PlayCtrl 416 is inside that block.** A plain write to 416 on a locked device
    is rejected, so a "Stop" that does not unlock first leaves the strip PLAYING
    while the UI reports it stopped — a fail-open on a stop command. That is why
    this is a range test and not a hand-listed set: a set is exactly the shape
    that lost 416 on the desktop.

    NOT lock-gated (accepted regardless): the pixel pool 1100+, CMD 430, poke
    450–452, TOD/FxAux/Wx/TextColor 453–467, Scale 460, TextLines 494 and the
    text window 516–579.
    """
    r = int(reg)
    return MB2WS_LOCK_GATED_FIRST <= r <= MB2WS_LOCK_GATED_LAST and r != MB2WS_LOCK


def rgbw_lock_bracket_writes(writes: Mapping[int, int]) -> List[Tuple[int, int]]:
    """``{reg: value}`` → the full ordered write list, unlock and lock included.

    The ONE home on this side of the unlock → DEPENDENCY-ordered writes → lock
    sequence both desktop panels duplicate (`module_config_rgbw_tk._unlock_write_lock`
    / `qt_app/module_config/specialized_pages_rgbw._unlock_write_lock`).

    A batch touching NO lock-gated register is returned unbracketed — an unlock
    that is not needed still costs two frames on a shared RS-485 line, and it
    would leave the device unlocked if the caller aborts mid-batch.

    The caller is expected to stop at the first failed write and then write
    ``(MB2WS_LOCK, 0)`` itself: this function is pure, it cannot know a write
    failed. Re-locking after a failure is the desktop's behaviour and the reason
    the lock write is a separate final element rather than an implicit epilogue.
    """
    ordered = rgbw_settings_write_order(writes.keys())
    body = [(int(r), int(writes[r]) & 0xFFFF) for r in ordered]
    if not any(rgbw_reg_is_lock_gated(r) for r, _v in body):
        return body
    return [(MB2WS_LOCK, MB2WS_UNLOCK_KEY)] + body + [(MB2WS_LOCK, 0)]


def rgbw_cmd_is_allowed(cmd: int) -> bool:
    """False for CMD 3 (SAVE_FLASH) — accepted by the range check, refused by the handler.

    Firmware takes anything 0..4 on reg 430 and answers a normal ack, then the
    handler sets ``last_error = 5`` / ``CMD_ST_ERROR`` and stores nothing
    (svc_mb_mb2ws.c:897-901). So a CMD-3 write looks successful on the wire and
    does nothing at all. The effect-upload path is the EXFX protocol over holding
    4000+. Reading the range gate alone is what made the original desktop plan
    record this as a harmless gap — check the handler, not just the validator.
    """
    return int(cmd) in RGBW_CMD_ALLOWED


# ---------------------------------------------------------------------------
# Choice lists (i18n KEYS, resolved by the web layer)
# ---------------------------------------------------------------------------


def rgbw_led_type_choices() -> List[Tuple[int, str]]:
    """(LedType code, label) — firmware svc_mb_mb2ws.h / MODBUS_VARIABLES."""
    return list(RGBW_LED_TYPE_CHOICES)


def rgbw_byte_order_choices() -> List[Tuple[int, str]]:
    """(ByteOrder code, label) — classic MB2WS LedColor 0..11 → reg 412."""
    return list(RGBW_BYTE_ORDER_CHOICES)


def rgbw_pwm_mode_choices() -> List[Tuple[int, str]]:
    """(PWM strip_mode 0…11, i18n key) — holding 49 / svc_rgbw_pwm.h."""
    return list(RGBW_PWM_MODE_CHOICES)


def rgbw_pwm_channel_label_keys(mode: int) -> Tuple[str, str, str, str]:
    """i18n keys for silk R/G/B/W labels in the given strip mode."""
    m = rgbw_clamp_pwm_mode(mode)
    return RGBW_PWM_CHANNEL_LABEL_KEYS.get(
        m, RGBW_PWM_CHANNEL_LABEL_KEYS[RGBW_PWM_MODE_RGBW]
    )


def rgbw_pwm_channel_visible(mode: int) -> Tuple[bool, bool, bool, bool]:
    """Which silk channels show controls (parallel slaves hidden)."""
    m = rgbw_clamp_pwm_mode(mode)
    return RGBW_PWM_CHANNEL_VISIBLE.get(m, (True, True, True, True))


def rgbw_line_ui_choices() -> List[Tuple[str, str]]:
    """(ui_mode, i18n key) — labels resolved in the i18n layer."""
    return list(RGBW_LINE_UI_CHOICES)


def rgbw_scene_ui_choices() -> List[Tuple[str, str]]:
    return list(RGBW_SCENE_UI_CHOICES)


def rgbw_fx_mode_choices() -> List[Tuple[int, str]]:
    """(FxId 0..81, i18n key) in id order — firmware ``fx_mode_t`` / Modbus reg 405."""
    return [(fx_id, key) for (fx_id, key, _group) in RGBW_FX_CATALOG]


def rgbw_fx_group_choices() -> List[Tuple[str, str]]:
    """(group_id, i18n key) for the 10 FX groups in display order."""
    return list(RGBW_FX_GROUP_ORDER)


def rgbw_fx_modes_in_group(group_id: str) -> List[Tuple[int, str]]:
    """(FxId, i18n key) of the modes in ``group_id``, in id order."""
    g = str(group_id)
    return [(fx_id, key) for (fx_id, key, group) in RGBW_FX_CATALOG if group == g]


def rgbw_fx_group_of(fx_id: int) -> str:
    """Group id owning ``fx_id`` (drives radio preselect on device read-back)."""
    return fx_group_for_id(fx_id)


# ---------------------------------------------------------------------------
# Per-mode register surfaces
# ---------------------------------------------------------------------------


def rgbw_fx_uses_text_window(fx_id: int) -> bool:
    """True when the effect renders the marquee window (516..579)."""
    return int(fx_id) == RGBW_FX_MODE_MATRIX_TEXT


def rgbw_fx_uses_text_lines(fx_id: int) -> bool:
    """True when the marquee card's TextLines combo applies (mode 62).

    Weather (64) and the line indicator (81) also honour register 494, but
    through their own cards (see :func:`rgbw_wx_lines_choices`) — not this
    control.
    """
    return int(fx_id) == RGBW_FX_MODE_MATRIX_TEXT


def rgbw_fx_uses_text_colors(fx_id: int) -> bool:
    """True when the effect honours the glyph/background colours 463..466."""
    return int(fx_id) in (
        RGBW_FX_MODE_MATRIX_TEXT,
        RGBW_FX_MODE_WEATHER,
        RGBW_FX_MODE_MB_SPY,
    )


def rgbw_fx_uses_soft_clock(fx_id: int) -> bool:
    """True when the effect reads the soft clock 453/454."""
    return int(fx_id) in (
        RGBW_FX_MODE_CIRCADIAN,
        RGBW_FX_MODE_CLOCK,
        RGBW_FX_MODE_WEATHER,
    )


def rgbw_fx_uses_weather(fx_id: int) -> bool:
    """True when the effect reads the weather block 457..462."""
    return int(fx_id) == RGBW_FX_MODE_WEATHER


def rgbw_fx_uses_mb_spy(fx_id: int) -> bool:
    """True when the effect reads Spy/Master holdings 640..695."""
    return int(fx_id) == RGBW_FX_MODE_MB_SPY


def rgbw_fx_uses_density(fx_id: int) -> bool:
    """True when the effect's renderer reads FxDensity (456)."""
    return int(fx_id) in RGBW_FX_DENSITY_MODES


def rgbw_fx_uses_aux(fx_id: int) -> bool:
    """True when the effect's renderer reads FxAux (455) — either byte."""
    spec = rgbw_fx_aux_spec(fx_id)
    return (
        spec.low_kind != RGBW_FX_AUX_LOW_VARIANT
        or spec.high_kind != RGBW_FX_AUX_HIGH_UNUSED
    )


# Control keys for the per-mode visibility map. The LED window hides — never
# disables, never shows a placeholder — any control the firmware ignores for the
# selected effect; see `rgbw_fx_control_visibility`.
RGBW_CTL_FX_AUX_LOW = "fx_aux_low"
RGBW_CTL_FX_AUX_FLAG = "fx_aux_flag"
RGBW_CTL_FX_AUX_COLOR = "fx_aux_color"
RGBW_CTL_FX_AUX_POOL_LEN = "fx_aux_pool_len"
RGBW_CTL_FX_DENSITY = "fx_density"
RGBW_CTL_TEXT_WINDOW = "text_window"
RGBW_CTL_TEXT_LINES = "text_lines"
RGBW_CTL_TEXT_COLORS = "text_colors"
RGBW_CTL_CLOCK = "clock"
RGBW_CTL_WEATHER = "weather"
RGBW_CTL_MB_SPY = "mb_spy"
RGBW_CTL_WX_TEMP_COLOR = "wx_temp_color"
RGBW_CTL_SCALE = "scale"

RGBW_MODE_SPECIFIC_CONTROLS: Tuple[str, ...] = (
    RGBW_CTL_FX_AUX_LOW,
    RGBW_CTL_FX_AUX_FLAG,
    RGBW_CTL_FX_AUX_COLOR,
    RGBW_CTL_FX_AUX_POOL_LEN,
    RGBW_CTL_FX_DENSITY,
    RGBW_CTL_TEXT_WINDOW,
    RGBW_CTL_TEXT_LINES,
    RGBW_CTL_TEXT_COLORS,
    RGBW_CTL_CLOCK,
    RGBW_CTL_WEATHER,
    RGBW_CTL_MB_SPY,
    RGBW_CTL_WX_TEMP_COLOR,
    RGBW_CTL_SCALE,
)


def rgbw_fx_control_visibility(fx_id: int) -> Dict[str, bool]:
    """Which mode-specific LED-window controls apply to ``fx_id`` — the ONE home.

    Standing rule for this window: a control whose register/field the firmware
    does not honour for the selected effect is **hidden**, never greyed and never
    shown holding a value the device ignores — a visible control implies the
    device obeys it. A new mode-specific control declares its rule here, not
    inline in a widget.

    Registers that apply in every mode (FxSpeed 406, FxParam 407, and the whole
    strip/topology card) are deliberately absent: this map is about surfaces
    whose support varies, not about hiding things arbitrarily.

    ``RGBW_CTL_SCALE`` is the one entry that is False for EVERY id — Scale (460)
    is stored but never consumed by the renderer (see
    :func:`rgbw_scale_is_honoured`), so it is source-independent. It lives here
    rather than in a one-off widget condition so the UI keeps a single
    visibility path.
    """
    spec = rgbw_fx_aux_spec(fx_id)
    return {
        # Shown only where the renderer reads the LOW byte and it is a value (the
        # 0/1 flag modes get the checkbox instead). There is no plain-variant
        # fallback: no renderer reads a generic variant, so on the 68 ids that
        # ignore 455 the whole row hides rather than offering a dead number.
        RGBW_CTL_FX_AUX_LOW: spec.low_kind not in (
            RGBW_FX_AUX_LOW_VARIANT, RGBW_FX_AUX_LOW_FLAG,
        ),
        RGBW_CTL_FX_AUX_FLAG: bool(spec.flag_label_key),
        RGBW_CTL_FX_AUX_COLOR: spec.high_kind == RGBW_FX_AUX_HIGH_COLOR,
        RGBW_CTL_FX_AUX_POOL_LEN: spec.high_kind == RGBW_FX_AUX_HIGH_LENGTH,
        RGBW_CTL_FX_DENSITY: rgbw_fx_uses_density(fx_id),
        RGBW_CTL_TEXT_WINDOW: rgbw_fx_uses_text_window(fx_id),
        RGBW_CTL_TEXT_LINES: rgbw_fx_uses_text_lines(fx_id),
        RGBW_CTL_TEXT_COLORS: rgbw_fx_uses_text_colors(fx_id),
        RGBW_CTL_CLOCK: rgbw_fx_uses_soft_clock(fx_id),
        RGBW_CTL_WEATHER: rgbw_fx_uses_weather(fx_id),
        RGBW_CTL_MB_SPY: rgbw_fx_uses_mb_spy(fx_id),
        # WxTempColor (467) is the weather renderer's own temperature colour —
        # no other mode reads it, so it rides the same predicate as 457..462.
        RGBW_CTL_WX_TEMP_COLOR: rgbw_fx_uses_weather(fx_id),
        RGBW_CTL_SCALE: rgbw_scale_is_honoured(),
    }


def rgbw_scale_is_honoured() -> bool:
    """False: firmware STORES Scale (460) but never renders with it.

    ``svc_fx_engine_set_scale()`` (``svc_fx_engine.c:7280``) and
    ``svc_mb_mb2ws_scale()`` (``svc_mb_mb2ws.c:1137``) are declared and defined
    with **no caller anywhere in Core/**, and ``rgbw_sync_fx_from_mb2ws()``
    (``rgbw_app.c:409-443``) pushes mode · speed · brightness · colour1 ·
    colour2 · aux · density · text_lines to the engine and never scale. The word
    "scale" does not appear in ``rgbw_app.c`` at all, so the register reaches no
    render path — FX, pool or flash. FxDensity (456) took over that struct field
    (``svc_fx_engine_set_density``, ``:7284``).

    So 460 is write-and-read-back storage. The register, its constant and the
    write helper stay (the register really exists and answers reads); only the UI
    promise was wrong, and the control is hidden. **If a future firmware wires
    ``svc_fx_engine_set_scale``, flip this predicate and the control returns
    through the same path** — no widget code changes.
    """
    return False


class RgbwFxAuxSpec(NamedTuple):
    """How firmware reads reg 455 for one effect — the UI builds its widgets here.

    ``low_kind`` / ``high_kind`` are the ``RGBW_FX_AUX_*`` constants.
    ``low_choices`` is ``(code, i18n_key)`` for the enum kinds and empty
    otherwise; ``low_min`` / ``low_max`` bound the numeric kinds.
    ``flag_label_key`` is set only where bit 7 of the low byte is a separate
    toggle.
    """

    low_kind: str
    low_label_key: str
    low_choices: Tuple[Tuple[int, str], ...]
    low_min: int
    low_max: int
    flag_label_key: str
    high_kind: str
    high_label_key: str
    high_min: int
    high_max: int


def rgbw_fx_aux_spec(fx_id: int) -> RgbwFxAuxSpec:
    """The reg-455 layout firmware applies to ``fx_id`` (MODBUS_VARIABLES reg 455)."""
    v = int(fx_id)
    high_kind = (
        RGBW_FX_AUX_HIGH_COLOR if v in RGBW_FX_AUX_COLOR_MODES else RGBW_FX_AUX_HIGH_UNUSED
    )
    high_label = "rgbw_scene_fx_aux_color"
    if v == RGBW_FX_MODE_MATRIX_TEXT:
        return RgbwFxAuxSpec(
            RGBW_FX_AUX_LOW_STYLE, "rgbw_scene_fx_aux_style", RGBW_FX_AUX_STYLE_CHOICES,
            0, MB2WS_FX_AUX_STYLE_MAX, "", high_kind, high_label, 0, MB2WS_FX_AUX_COLOR_MAX,
        )
    if v in (66, 67):
        return RgbwFxAuxSpec(
            RGBW_FX_AUX_LOW_DIRECTION, "rgbw_scene_fx_aux_direction",
            RGBW_FX_AUX_DIRECTION_CHOICES, 0, 3, "",
            high_kind, high_label, 0, MB2WS_FX_AUX_COLOR_MAX,
        )
    if v == 43:
        return RgbwFxAuxSpec(
            RGBW_FX_AUX_LOW_PERCENT, "rgbw_scene_fx_aux_percent", (), 0, 100, "",
            high_kind, high_label, 0, MB2WS_FX_AUX_COLOR_MAX,
        )
    if v == 75:
        return RgbwFxAuxSpec(
            RGBW_FX_AUX_LOW_LEVEL, "rgbw_scene_fx_aux_percent", (), 0, 100,
            "rgbw_scene_fx_aux_orientation", high_kind, high_label, 0, MB2WS_FX_AUX_COLOR_MAX,
        )
    if v == 51:
        # Escort repacks BOTH bytes: the high byte is a pool length, NOT a colour.
        return RgbwFxAuxSpec(
            RGBW_FX_AUX_LOW_ESCORT, "rgbw_scene_fx_aux_escort_color",
            RGBW_FX_AUX_ESCORT_COLOR_CHOICES, 0, MB2WS_FX_AUX_COLOR_MAX,
            "rgbw_scene_fx_aux_reverse",
            RGBW_FX_AUX_HIGH_LENGTH, "rgbw_scene_fx_aux_pool_len", 0, 255,
        )
    if v == 70:
        return RgbwFxAuxSpec(
            RGBW_FX_AUX_LOW_FLAG, "rgbw_scene_fx_aux_stop_stripes", (), 0, 1,
            "rgbw_scene_fx_aux_stop_stripes", high_kind, high_label,
            0, MB2WS_FX_AUX_COLOR_MAX,
        )
    return RgbwFxAuxSpec(
        RGBW_FX_AUX_LOW_VARIANT, "rgbw_scene_fx_aux_variant", (), 0, 255, "",
        high_kind, high_label, 0, MB2WS_FX_AUX_COLOR_MAX,
    )


def rgbw_fx_aux_compose(fx_id: int, low: int = 0, flag: bool = False, high: int = 0) -> int:
    """Pack the UI's fields into reg 455 for ``fx_id``.

    ``low`` is the mode's low-byte field (style / direction / percent / colour
    code / plain variant), ``flag`` bit 7 where the mode has one, ``high`` the
    colour override or Escort's pool length. A mode that ignores the high byte
    always writes 0 there — so the colour combo can never leak into Escort's pool
    length. Never hand-pack 455 at a call site: six layouts share the register.
    """
    spec = rgbw_fx_aux_spec(fx_id)
    v = max(spec.low_min, min(spec.low_max, int(low)))
    if spec.low_kind == RGBW_FX_AUX_LOW_FLAG:
        low_byte = 1 if flag else 0
    elif spec.low_kind in (RGBW_FX_AUX_LOW_LEVEL, RGBW_FX_AUX_LOW_ESCORT):
        low_byte = (v & MB2WS_FX_AUX_FIELD_MASK) | (MB2WS_FX_AUX_FLAG_BIT if flag else 0)
    else:
        low_byte = v & 0xFF
    if spec.high_kind == RGBW_FX_AUX_HIGH_UNUSED:
        high_byte = 0
    else:
        high_byte = max(spec.high_min, min(spec.high_max, int(high))) & 0xFF
    return ((high_byte & 0xFF) << 8) | (low_byte & 0xFF)


def rgbw_fx_aux_decompose(fx_id: int, value: int) -> Tuple[int, bool, int]:
    """Reg-455 value → ``(low_field, flag, high_field)`` for ``fx_id``.

    Inverse of :func:`rgbw_fx_aux_compose`. Out-of-range fields are clamped the
    way firmware clamps them (equalizer percent > 100 → 100, style > 17 → 0).
    """
    spec = rgbw_fx_aux_spec(fx_id)
    x = int(value) & 0xFFFF
    low_byte = x & 0xFF
    high_byte = (x >> 8) & 0xFF
    flag = False
    if spec.low_kind == RGBW_FX_AUX_LOW_FLAG:
        low = 0
        flag = bool(low_byte & 0x01)
    elif spec.low_kind in (RGBW_FX_AUX_LOW_LEVEL, RGBW_FX_AUX_LOW_ESCORT):
        flag = bool(low_byte & MB2WS_FX_AUX_FLAG_BIT)
        low = low_byte & MB2WS_FX_AUX_FIELD_MASK
    else:
        low = low_byte
    if spec.low_choices:
        codes = [code for code, _k in spec.low_choices]
        if low not in codes:
            low = codes[0]
    else:
        low = max(spec.low_min, min(spec.low_max, low))
    if spec.high_kind == RGBW_FX_AUX_HIGH_UNUSED:
        high = 0
    else:
        high = max(spec.high_min, min(spec.high_max, high_byte))
    return low, flag, high


def rgbw_fx_aux_style_needs_two_line_hint(fx_id: int, style: int, text_lines: int) -> bool:
    """True when the picked text style will not render under TextLines = 2.

    Style 16 (credits) is the only one firmware runs in the two-line 494 = 2
    layout. The UI shows a hint; it never rewrites the user's 494.
    """
    if int(fx_id) != RGBW_FX_MODE_MATRIX_TEXT or int(text_lines) != 2:
        return False
    return int(style) != MB2WS_FX_AUX_STYLE_CREDITS


def rgbw_fx_aux_hint_keys(
    fx_id: int, style: int, text: str = "", text_lines: int = 1
) -> Tuple[str, ...]:
    """i18n keys of the advisory hints for the current text-mode selection.

    Advisory only — the UI explains firmware behaviour it cannot override, and
    never rewrites the user's TextLines or text.
    """
    if int(fx_id) != RGBW_FX_MODE_MATRIX_TEXT:
        return ()
    keys: List[str] = []
    if rgbw_fx_aux_style_needs_two_line_hint(fx_id, style, text_lines):
        keys.append("rgbw_fx_aux_style_hint_two_line")
    if int(style) == MB2WS_FX_AUX_STYLE_MORPH and "|" not in str(text):
        keys.append("rgbw_fx_aux_style_hint_morph")
    keys.append("rgbw_fx_aux_style_hint_wide")
    return tuple(keys)


def rgbw_text_lines_choices() -> List[Tuple[int, str]]:
    """(reg-494 value, i18n key) — the two layouts firmware actually distinguishes.

    Marquee (mode 62) labels: large 2x vs two 5×7 lines. Weather (mode 64) uses
    :func:`rgbw_wx_lines_choices` for the same codes with weather wording.
    """
    return [
        (MB2WS_TEXT_LINES_SINGLE, "rgbw_text_lines_single"),
        (MB2WS_TEXT_LINES_DOUBLE, "rgbw_text_lines_double"),
    ]


def rgbw_wx_lines_choices() -> List[Tuple[int, str]]:
    """(reg-494 value, i18n key) — weather station 1-line cycle vs two 5×7 lines."""
    return [
        (MB2WS_TEXT_LINES_SINGLE, "rgbw_wx_lines_single"),
        (MB2WS_TEXT_LINES_DOUBLE, "rgbw_wx_lines_double"),
    ]


def rgbw_text_lines_ui_code(reg_value: int) -> int:
    """Device reg-494 value → the UI option it means.

    Firmware distinguishes ``== 2`` alone (``svc_fx_engine.c:551``), so 0 and 1
    are the same layout and both map to the single-line option the combo offers.
    """
    return (
        MB2WS_TEXT_LINES_DOUBLE
        if int(reg_value) == MB2WS_TEXT_LINES_DOUBLE
        else MB2WS_TEXT_LINES_SINGLE
    )


def rgbw_text_2x_capacity(canvas_width_px: int) -> int:
    """How many characters render at 2x on a canvas this wide.

    Firmware draws the 2x block while ``n*advance - gap <= canvas_w``
    (``svc_fx_engine.c:606-609``): the gap sits BETWEEN glyphs, so the trailing
    one does not count. Hence ``(canvas_w + gap) // advance`` — a plain
    ``canvas_w // advance`` under-counts by one whenever ``canvas_w mod 13 >= 10``,
    which includes the 64 px default chain (fits 5, not 4).

    Beyond the limit firmware auto-renders 1x 5×7 and scrolls; no register forces
    2x, so the window explains the limit rather than pretending to control it.
    """
    w = int(canvas_width_px)
    if w < (MB2WS_TEXT_ADVANCE_2X_PX - MB2WS_TEXT_GAP_2X_PX):
        return 0  # not even one 10 px glyph fits
    return (w + MB2WS_TEXT_GAP_2X_PX) // MB2WS_TEXT_ADVANCE_2X_PX


def rgbw_text_2x_hint_args() -> Tuple[int, int, int]:
    """``%``-args for the 2x hint: (px/char, canvas width, chars).

    The UI formats the hint through this, so the numbers the user reads are the
    firmware metrics rather than hand-typed literals that can drift.
    """
    return (
        MB2WS_TEXT_ADVANCE_2X_PX,
        MB2WS_TEXT_REF_CANVAS_W_PX,
        rgbw_text_2x_capacity(MB2WS_TEXT_REF_CANVAS_W_PX),
    )


def rgbw_resolve_fx_id(fx_id: int) -> int:
    """Map wire FxId to canonical 0..81 (Gyver 128..209 → 0..81; else OOR → Static)."""
    v = int(fx_id) & 0xFFFF
    if RGBW_FX_MODE_GYVER_BASE <= v < (RGBW_FX_MODE_GYVER_BASE + RGBW_FX_MODE_COUNT):
        return v - RGBW_FX_MODE_GYVER_BASE
    if v >= RGBW_FX_MODE_COUNT:
        return RGBW_FX_MODE_STATIC
    return v


def rgbw_clamp_fx_id(fx_id: int) -> int:
    """Clamp UI FxId to writable 0..81 (canonical modes)."""
    return max(0, min(RGBW_FX_MODE_COUNT - 1, int(fx_id)))


# ---------------------------------------------------------------------------
# Spy / Master (FX 81)
# ---------------------------------------------------------------------------


def rgbw_spy_slot_reg(slot: int, offset: int) -> int:
    """Holding address of slot field: n=0..3, offset 0..7 from 648."""
    n = max(0, min(MB2WS_SPY_SLOT_COUNT - 1, int(slot)))
    off = max(0, min(MB2WS_SPY_SLOT_STRIDE - 1, int(offset)))
    return MB2WS_SPY_SLOT_BASE + n * MB2WS_SPY_SLOT_STRIDE + off


def rgbw_spy_lim_reg(slot: int, which: int) -> int:
    """Holding of slot limit: n=0..3, which 0=lo 1=hi, from 688."""
    n = max(0, min(MB2WS_SPY_SLOT_COUNT - 1, int(slot)))
    off = 1 if int(which) else 0
    return MB2WS_SPY_LIM_BASE + n * 2 + off


def rgbw_wx_spy_field_keys() -> List[str]:
    """i18n keys for weather-listen fields 0..6 (HH..PRESS)."""
    return list(RGBW_WX_SPY_FIELD_KEYS)


def rgbw_wx_spy_reg(field: int, off: int) -> int:
    """Holding of weather-listen field n=0..6, off 0=UID|FC 1=register, from 696."""
    n = max(0, min(MB2WS_WX_SPY_COUNT - 1, int(field)))
    o = 1 if int(off) else 0
    return MB2WS_WX_SPY_BASE + n * MB2WS_WX_SPY_FIELD_REGS + o


def rgbw_wx_spy_pack_uid_fc(uid: int, fc: int) -> int:
    """Pack UID (low) and FC (high; 0 becomes FC03) for 696+n×2."""
    u = max(0, min(247, int(uid)))
    f = int(fc) & 0xFF
    if f == 0:
        f = MB2WS_SPY_FC_HOLDING
    return u | (f << 8)


def rgbw_wx_spy_unpack_uid_fc(word: int) -> Tuple[int, int]:
    """Inverse of :func:`rgbw_wx_spy_pack_uid_fc`."""
    w = int(word) & 0xFFFF
    uid = w & 0xFF
    fc = (w >> 8) & 0xFF
    if fc == 0:
        fc = MB2WS_SPY_FC_HOLDING
    return uid, fc


def rgbw_u16_to_i16(value: int) -> int:
    v = int(value) & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def rgbw_i16_to_u16(value: int) -> int:
    v = int(value)
    if v < -32768:
        v = -32768
    if v > 32767:
        v = 32767
    return v & 0xFFFF


def rgbw_spy_pack_unit(text: str) -> Tuple[int, int]:
    """Four cp1251 chars → (unit01, unit23), hi byte first. Truncates to 4."""
    raw = (text or "").encode("cp1251", errors="replace")[:4]
    raw = raw + b"\x00" * (4 - len(raw))
    return ((raw[0] << 8) | raw[1], (raw[2] << 8) | raw[3])


def rgbw_spy_unpack_unit(unit01: int, unit23: int) -> str:
    """Inverse of :func:`rgbw_spy_pack_unit`, stops at the first NUL."""
    buf = bytes((
        (int(unit01) >> 8) & 0xFF,
        int(unit01) & 0xFF,
        (int(unit23) >> 8) & 0xFF,
        int(unit23) & 0xFF,
    ))
    buf = buf.split(b"\x00", 1)[0]
    return buf.decode("cp1251", errors="replace")


def rgbw_spy_live_i32(hi: int, lo: int) -> int:
    """Live 680+n×2 hi/lo words as signed int32."""
    u = ((int(hi) & 0xFFFF) << 16) | (int(lo) & 0xFFFF)
    return u - 0x100000000 if u >= 0x80000000 else u


def rgbw_spy_baud_choices() -> List[Tuple[int, str]]:
    """(holding-110 code, label) — WorkPort baud, same codes as family 110."""
    return list(RGBW_SPY_BAUD_CHOICES)


def rgbw_spy_fc_choices() -> List[Tuple[int, str]]:
    return list(RGBW_SPY_FC_CHOICES)


def rgbw_spy_type_choices() -> List[Tuple[int, str]]:
    return list(RGBW_SPY_TYPE_CHOICES)


def rgbw_spy_type_is_time(typ: int) -> bool:
    """True for TIME_HH/MM/SS (10..12) — composed clock, not a value page."""
    t = int(typ)
    return MB2WS_SPY_TYPE_TIME_HH <= t <= MB2WS_SPY_TYPE_TIME_SS


def rgbw_spy_unit_presets() -> List[str]:
    return list(RGBW_SPY_UNIT_PRESETS)


# ---------------------------------------------------------------------------
# Marquee text window (516..579)
# ---------------------------------------------------------------------------


def rgbw_text_reg_span() -> Tuple[int, int]:
    """(first, last) holding address of the marquee window — 516..579.

    Never below 516: the pre-1.0.2.2 base 495 overlapped the family safe-AO block
    503..506, so writing text silently drove the module's safe-state PWM values
    to 100 %. `rgbw_text_span_clears_safe_ao()` is the assertion form.
    """
    return MB2WS_TEXT_BASE, MB2WS_TEXT_BASE + MB2WS_TEXT_REG_COUNT - 1


def rgbw_text_span_clears_safe_ao() -> bool:
    """True while the marquee window starts ABOVE the safe-state AO block.

    The one predicate a caller (or a test) asks instead of re-deriving the
    overlap: the destructive legacy base 495 spanned 495..510 and covered
    503..506, the module's safe-state PWM values.
    """
    return MB2WS_TEXT_BASE > RGBW_PWM_SAFE_LAST


def rgbw_pack_text_cp1251(s: str) -> List[int]:
    """Pack marquee text into the 64 u16 regs at ``MB2WS_TEXT_BASE`` (516..579).

    Uppercase-fold, encode cp1251 (unmapped → replacement via ``errors='replace'``),
    then pack 2 bytes/reg HIGH-BYTE-FIRST (high = first/left char), zero-pad and
    truncate to ``MB2WS_TEXT_REG_COUNT`` (64) regs = ``MB2WS_TEXT_MAX_CHARS``
    (128) chars max — the firmware's own window size.
    """
    data = str(s).upper().encode("cp1251", "replace")
    regs: List[int] = []
    for i in range(0, len(data), 2):
        hi = data[i]
        lo = data[i + 1] if (i + 1) < len(data) else 0
        regs.append(((hi & 0xFF) << 8) | (lo & 0xFF))
    regs = regs[:MB2WS_TEXT_REG_COUNT]
    while len(regs) < MB2WS_TEXT_REG_COUNT:
        regs.append(0)
    return regs


def rgbw_unpack_text_cp1251(regs: List[int]) -> str:
    """Inverse of :func:`rgbw_pack_text_cp1251` — the marquee window read back.

    High byte = first character (firmware packing), a 0 byte ends the string, and
    cp1251 bytes decode back to text. Trailing zero padding therefore disappears,
    so a device that never had text reads back as ``""`` rather than 128 NULs.
    """
    data = bytearray()
    for r in list(regs)[:MB2WS_TEXT_REG_COUNT]:
        x = int(r) & 0xFFFF
        for b in ((x >> 8) & 0xFF, x & 0xFF):
            if b == 0:
                return data.decode("cp1251", "replace")
            data.append(b)
    return data.decode("cp1251", "replace")


def rgbw_text_within_limit(s: str) -> bool:
    """True when ``s`` fits the marquee window without losing characters."""
    return len(str(s)) <= MB2WS_TEXT_MAX_CHARS


def rgbw_truncate_text(s: str) -> str:
    """``s`` cut to what the window actually holds (``MB2WS_TEXT_MAX_CHARS``).

    The packer truncates silently; the UI uses this to stop the user typing past
    the limit instead of losing the tail without a word.
    """
    return str(s)[:MB2WS_TEXT_MAX_CHARS]


# ---------------------------------------------------------------------------
# Colour conversions (RGB565 text colours 463..466, permille PWM 33..35)
# ---------------------------------------------------------------------------


def rgbw_rgb565_from_rgb8(r: int, g: int, b: int) -> int:
    """8-bit RGB → RGB565 u16 (the text-colour registers 463..466)."""
    return (((int(r) & 0xF8) << 8) | ((int(g) & 0xFC) << 3) | ((int(b) & 0xFF) >> 3)) & 0xFFFF


def rgbw_rgb565_to_rgb8(v: int) -> Tuple[int, int, int]:
    """RGB565 u16 → 8-bit RGB, channels expanded so the round-trip is stable."""
    x = int(v) & 0xFFFF
    r = (x >> 11) & 0x1F
    g = (x >> 5) & 0x3F
    b = x & 0x1F
    return (r * 255) // 31, (g * 255) // 63, (b * 255) // 31


def rgbw_hex_to_rgb565(s: str) -> int:
    """'#RRGGBB' (or 'RRGGBB') → RGB565 u16; malformed → 0 (firmware default)."""
    t = (s or "").strip().lstrip("#")
    if len(t) != 6:
        return 0
    try:
        return rgbw_rgb565_from_rgb8(int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16))
    except ValueError:
        return 0


def rgbw_rgb565_to_hex(v: int) -> str:
    """RGB565 u16 → '#RRGGBB' — the QUANTISED colour the device actually holds."""
    r, g, b = rgbw_rgb565_to_rgb8(v)
    return "#%02X%02X%02X" % (r, g, b)


def rgbw_rgb_permille_to_rgb565(r: int, g: int, b: int) -> int:
    """Colour-wheel permille (0..1000) → RGB565, for the text-colour registers.

    Both permille bridges ROUND rather than truncate: flooring in both directions
    loses a whole 5-bit step, so a colour read off the device and handed straight
    back to the wheel would drift one shade darker on every trip.
    """
    def _to8(x: int) -> int:
        return (max(0, min(1000, int(x))) * 255 + 500) // 1000

    return rgbw_rgb565_from_rgb8(_to8(r), _to8(g), _to8(b))


def rgbw_rgb565_to_rgb_permille(v: int) -> Tuple[int, int, int]:
    """RGB565 → permille (0..1000), to seed the colour wheel from a stored colour."""
    r, g, b = rgbw_rgb565_to_rgb8(v)
    return (
        (r * 1000 + 127) // 255,
        (g * 1000 + 127) // 255,
        (b * 1000 + 127) // 255,
    )


def rgbw_hsv_to_rgb_permille(h: float, s: float, v: float) -> Tuple[int, int, int]:
    """HSV (0..1) → silk R/G/B permille 0..1000 (PWM holdings 33–35)."""
    hh = max(0.0, min(1.0, float(h)))
    ss = max(0.0, min(1.0, float(s)))
    vv = max(0.0, min(1.0, float(v)))
    r, g, b = hsv_to_rgb(hh, ss, vv)
    return (
        max(0, min(1000, int(round(r * 1000.0)))),
        max(0, min(1000, int(round(g * 1000.0)))),
        max(0, min(1000, int(round(b * 1000.0)))),
    )


def rgbw_rgb_permille_to_hsv(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """Silk R/G/B permille 0..1000 → HSV 0..1."""
    rf = max(0.0, min(1.0, float(r) / 1000.0))
    gf = max(0.0, min(1.0, float(g) / 1000.0))
    bf = max(0.0, min(1.0, float(b) / 1000.0))
    return rgb_to_hsv(rf, gf, bf)


# ---------------------------------------------------------------------------
# Soft clock / weather (453..467)
# ---------------------------------------------------------------------------


def rgbw_tod_is_set(hours: int, minutes: int) -> bool:
    """True when the device's soft clock is RUNNING (neither reg holds 0xFFFF).

    Firmware keeps 453/454 at ``0xFFFF`` until a master seeds them — the factory
    state and the state after every power-cycle, since neither register is in
    EEPROM. It ticks only while both are set, so "set" is a property of the pair,
    never of one register.
    """
    return int(hours) != MB2WS_TOD_UNSET and int(minutes) != MB2WS_TOD_UNSET


def rgbw_tod_unset_writes() -> Dict[int, int]:
    """Register writes that put the soft clock back to "not set".

    Firmware resets BOTH regs when either is written 0xFFFF; both are written
    here so the intent is explicit on the wire and the read-back is unambiguous.
    """
    return {MB2WS_TOD_HOURS: MB2WS_TOD_UNSET, MB2WS_TOD_MINUTES: MB2WS_TOD_UNSET}


def rgbw_pc_clock_writes(now: datetime) -> Dict[int, int]:
    """Soft-clock writes (453/454) seeding the device from a host ``datetime``.

    Pure: the caller supplies the moment, so the mapping is unit-testable and the
    UI decides when "now" is.
    """
    return {
        MB2WS_TOD_HOURS: int(now.hour) & 0xFFFF,
        MB2WS_TOD_MINUTES: int(now.minute) & 0xFFFF,
    }


def rgbw_pc_date_writes(now: datetime) -> Dict[int, int]:
    """Weather date writes (457 ``(day<<8)|month`` + 462 year) from a ``datetime``.

    The year is clamped to the firmware range 2000..2199; a host clock outside it
    would answer exception 3 rather than seed anything.
    """
    return {
        MB2WS_WX_DATE: ((int(now.day) & 0xFF) << 8) | (int(now.month) & 0xFF),
        MB2WS_WX_YEAR: max(MB2WS_WX_YEAR_MIN, min(MB2WS_WX_YEAR_MAX, int(now.year))),
    }


class RgbwModeData(NamedTuple):
    """Decoded 453..467 block — ``None`` means the firmware sentinel, never 0.

    A sentinel rendered as 0 would look like a real reading (0 °C, midnight) and
    a stray Enter would then seed it, so the UI shows these as blank.
    """

    tod_hours: Optional[int]
    tod_minutes: Optional[int]
    fx_aux: int
    fx_density: int
    wx_day: Optional[int]
    wx_month: Optional[int]
    wx_temp_c: Optional[float]
    wx_hum_pct: Optional[float]
    wx_press_mmhg: Optional[float]
    wx_year: Optional[int]
    text_colors: Tuple[int, int, int, int]
    wx_temp_color: int


def rgbw_mode_data_read_span() -> Tuple[int, int]:
    """(base, count) of the ONE FC03 that reads the mode-data block: (453, 15).

    453..467 is contiguously mapped (``mb2ws_is_mapped_addr``,
    svc_mb_mb2ws.c:391-406) — Scale 460 sits inside it — so one read of 15
    registers is legal. An unmapped address ANYWHERE in a block fails the WHOLE
    read (:944-948), so widening past 467 does not return partial data: it
    returns an exception and the panel shows nothing. 494 and 516..579 need their
    own reads.
    """
    return MB2WS_MODE_DATA_BASE, MB2WS_MODE_DATA_COUNT


def rgbw_mode_data_decode(regs: List[int]) -> Optional[RgbwModeData]:
    """One FC03 block at 453 (15 regs) → the values the LED window displays.

    The ONE home for the sentinel contract: 453/454 ``0xFFFF`` (both, per
    :func:`rgbw_tod_is_set`), WxDate 0, WxTemp ``0x8000``, WxHum ``0xFFFF``,
    WxPress 0 and WxYear 0 all decode to ``None`` = "not set". Scale (460) sits
    inside the block and is skipped — firmware never renders with it. Returns
    ``None`` when the block is short, so a partial read displays nothing instead
    of garbage.
    """
    vals = [int(r) & 0xFFFF for r in list(regs)]
    if len(vals) < MB2WS_MODE_DATA_COUNT:
        return None

    def at(reg: int) -> int:
        return vals[reg - MB2WS_MODE_DATA_BASE]

    hh, mm = at(MB2WS_TOD_HOURS), at(MB2WS_TOD_MINUTES)
    clock_set = rgbw_tod_is_set(hh, mm)
    date = at(MB2WS_WX_DATE)
    temp = at(MB2WS_WX_TEMP)
    hum = at(MB2WS_WX_HUM)
    press = at(MB2WS_WX_PRESS)
    year = at(MB2WS_WX_YEAR)
    return RgbwModeData(
        tod_hours=hh if clock_set else None,
        tod_minutes=mm if clock_set else None,
        fx_aux=at(MB2WS_FX_AUX),
        fx_density=at(MB2WS_FX_DENSITY),
        wx_day=((date >> 8) & 0xFF) if date != MB2WS_WX_DATE_UNSET else None,
        wx_month=(date & 0xFF) if date != MB2WS_WX_DATE_UNSET else None,
        # int16 tenths of a degree — 0x8000 is the sentinel, not -3276.8 °C.
        wx_temp_c=(
            None
            if temp == MB2WS_WX_TEMP_UNSET
            else (temp - 0x10000 if temp >= 0x8000 else temp) / 10.0
        ),
        wx_hum_pct=None if hum == MB2WS_WX_HUM_UNSET else hum / 10.0,
        wx_press_mmhg=None if press == MB2WS_WX_PRESS_UNSET else press / 10.0,
        wx_year=None if year == MB2WS_WX_YEAR_UNSET else year,
        text_colors=(
            at(MB2WS_TEXT_COLOR1),
            at(MB2WS_TEXT_COLOR2),
            at(MB2WS_TEXT_BG1),
            at(MB2WS_TEXT_BG2),
        ),
        wx_temp_color=at(MB2WS_WX_TEMP_COLOR),
    )


# ---------------------------------------------------------------------------
# Matrix geometry (417) and layout (418)
# ---------------------------------------------------------------------------


def rgbw_matrix_pack(width: int, height: int) -> int:
    """Pack (width, height) → reg-417 u16: low byte width, high byte height.

    The LITERAL height is emitted (e.g. 16 → 0x1000 high byte), matching
    firmware's ``0x0010`` back-compat for 16×16. Height 0 is the firmware default
    (=16).
    """
    return ((int(height) & 0xFF) << 8) | (int(width) & 0xFF)


def rgbw_matrix_unpack(v: int) -> Tuple[int, int]:
    """Unpack reg-417 u16 → (width, height); high byte 0 → 16 (firmware default)."""
    x = int(v) & 0xFFFF
    width = x & 0xFF
    height = (x >> 8) & 0xFF
    return width, (height if height else 16)


def rgbw_matrix_layout_compose(prior: int, wiring: int) -> int:
    """Reg-418 value: the UI's wiring bits 0-3 over the device's other fields.

    The window only edits the four wiring checkboxes, but the register also holds
    TileMode (bits 4-6) and TileCount (bits 8-11), which the user configures
    elsewhere. Composing the whole u16 from the checkboxes would zero them on
    every layout write, so ``prior`` (the last value read back from the device)
    supplies them unchanged.
    """
    keep = int(prior) & MB2WS_MATRIX_LAYOUT_PRESERVE_MASK
    return keep | (int(wiring) & MB2WS_MATRIX_LAYOUT_WIRING_MASK)


def rgbw_matrix_layout_compose_full(wiring: int, tile_mode: int, tile_count: int) -> int:
    """Full reg-418 value from UI state: wiring bits 0-3 + TileMode + TileCount.

    For the case where the UI holds ALL six controls (the four wiring checkboxes
    plus both tiling combos) — nothing to preserve, every field is known.
    Reserved bits 7 and 12-15 stay zero by construction (firmware answers
    exception 3 if any is set). Out-of-range tiling raises: the firmware would
    reject the write (TileMode 3..7 / TileCount 5..15 → exception 3), so passing
    it through would only trade a clear error here for an opaque bus error later.
    """
    tm = int(tile_mode)
    tc = int(tile_count)
    if not 0 <= tm <= MB2WS_MATRIX_LAYOUT_TILEMODE_MAX:
        raise ValueError("TileMode out of range 0..2: %d" % tm)
    if not 0 <= tc <= MB2WS_MATRIX_LAYOUT_TILECOUNT_MAX:
        raise ValueError("TileCount out of range 0..4: %d" % tc)
    return (
        (int(wiring) & MB2WS_MATRIX_LAYOUT_WIRING_MASK)
        | (tm << MB2WS_MATRIX_LAYOUT_TILEMODE_SHIFT)
        | (tc << MB2WS_MATRIX_LAYOUT_TILECOUNT_SHIFT)
    )


def rgbw_matrix_layout_write_value(prior: Optional[int], wiring: int) -> Optional[int]:
    """Value to write to reg 418, or ``None`` meaning **do not write it at all**.

    An unknown ``prior`` (never read back, or the read failed) means TileMode and
    TileCount cannot be preserved. Writing anyway would compose them from nothing
    and silently zero the user's tiling — the exact data loss the
    read-modify-write exists to prevent — so the caller must skip the register.
    """
    if prior is None:
        return None
    return rgbw_matrix_layout_compose(int(prior), wiring)


def rgbw_matrix_layout_write_value_ex(
    prior: Optional[int],
    wiring: int,
    tile_mode: Optional[int] = None,
    tile_count: Optional[int] = None,
) -> Optional[int]:
    """Reg-418 write value with optional UI tiling overrides; ``None`` = skip.

    One decision table for every caller, so the skip-vs-compose rule lives in one
    home:

    - ``prior`` known → each tiling field comes from its override where the UI
      supplies one, else is preserved from ``prior``;
    - ``prior`` unknown, BOTH overrides given → full compose (the UI supplies
      every field, nothing left to preserve);
    - ``prior`` unknown, any override missing → ``None``: composing the missing
      field from nothing is the data loss the preserve guard exists to prevent.

    A preserved field that a (misbehaving) device reports out of range is folded
    the way firmware renders it — TileMode > 2 → span (0), TileCount > 4 →
    auto (0) — rather than raised: the value came from the device, not the user.
    """
    if prior is not None:
        base = int(prior)
        if tile_mode is None:
            tile_mode = rgbw_matrix_layout_tilemode(base)
            if tile_mode > MB2WS_MATRIX_LAYOUT_TILEMODE_MAX:
                tile_mode = 0
        if tile_count is None:
            tile_count = rgbw_matrix_layout_tilecount(base)
            if tile_count > MB2WS_MATRIX_LAYOUT_TILECOUNT_MAX:
                tile_count = 0
    elif tile_mode is None or tile_count is None:
        return None
    return rgbw_matrix_layout_compose_full(wiring, tile_mode, tile_count)


def rgbw_matrix_layout_tilemode(v: int) -> int:
    """Reg-418 TileMode field (bits 4-6): 0 span / 1 replicate / 2 mirror."""
    return (int(v) & MB2WS_MATRIX_LAYOUT_TILEMODE_MASK) >> MB2WS_MATRIX_LAYOUT_TILEMODE_SHIFT


def rgbw_matrix_layout_tilecount(v: int) -> int:
    """Reg-418 TileCount field (bits 8-11): 0 = auto, else the explicit count."""
    return (int(v) & MB2WS_MATRIX_LAYOUT_TILECOUNT_MASK) >> MB2WS_MATRIX_LAYOUT_TILECOUNT_SHIFT


def rgbw_tile_mode_choices() -> List[Tuple[int, str]]:
    """(reg-418 TileMode value, i18n key) — how the effect maps onto chained tiles."""
    return list(RGBW_TILE_MODE_CHOICES)


def rgbw_tile_count_choices() -> List[Tuple[int, str]]:
    """(reg-418 TileCount value, i18n key) — 0 auto (derived from LedCount), 1..4."""
    return [(0, "rgbw_tile_count_auto")] + [
        (n, "rgbw_tile_count_%d" % n)
        for n in range(1, MB2WS_MATRIX_LAYOUT_TILECOUNT_MAX + 1)
    ]


def rgbw_matrix_types() -> List[Tuple[str, int, int]]:
    """(i18n_key, width, height) — the fixed panel types the LED window offers."""
    return list(RGBW_MATRIX_TYPES)


def rgbw_matrix_type_index(width: int, height: int) -> int:
    """Index of (width, height) in :func:`rgbw_matrix_types`, or -1 when unlisted.

    A device may legally hold a geometry no listed type matches (firmware takes
    any 1..64 × 8..32). The window then SHOWS what it read — see
    :func:`rgbw_matrix_geometry_label` — instead of silently rewriting the device
    to a listed type.
    """
    w, h = int(width), int(height)
    for i, (_key, tw, th) in enumerate(rgbw_matrix_types()):
        if (tw, th) == (w, h):
            return i
    return -1


def rgbw_matrix_geometry_label(width: int, height: int) -> str:
    """"Ш×В" for a geometry with no listed type — digits only, so no translation."""
    return "%d×%d" % (int(width), int(height))


def rgbw_matrix_type_led_count(width: int, height: int, tile_count: int) -> Optional[int]:
    """LedCount0 for a type + explicit count (W×H×N); ``None`` for TileCount = auto.

    With auto (0) firmware derives N from LedCount0 itself, so the window must
    not invent one — it writes the geometry only and leaves reg 400 as the user
    set it.
    """
    n = int(tile_count)
    if n <= 0:
        return None
    return int(width) * int(height) * n


def rgbw_effective_pixel_format(ui_mode: str, pixel_format: int) -> int:
    """The PixelFormat this map will actually WRITE for ``ui_mode``.

    **This is the flasher's own policy, not a firmware coercion.** Firmware
    leaves reg 403 alone: ``MB2WS_SET_PIXEL_FMT`` is assigned only by the
    power-on default (``svc_mb_mb2ws.c:857``) and by a plain validated write, and
    the LineMode handler (``:648-666``) coerces **LedType and LedCount1 only** —
    never the pixel format. What makes APA102 RGB here is that
    :func:`rgbw_line_mode_to_regs` emits ``PixelFormat = 0`` in that mode.

    So this is the ONE home of that rule, shared by the write path and the
    accept-check (:func:`rgbw_matrix_fits_pool`): both size the pixel budget with
    the format that will really be on the device, so a pick can never be accepted
    at one cap and then silently clamped at another.
    """
    if str(ui_mode) == RGBW_LINE_UI_APA102:
        return 0
    return 1 if int(pixel_format) else 0


def rgbw_matrix_fits_pool(
    led_count0: int,
    *,
    pixel_format: int = 0,
    led_count1: int = 0,
    ui_mode: str = RGBW_LINE_UI_SINGLE,
) -> bool:
    """Does this LedCount0 fit the firmware caps and the shared 3072-byte pool?

    The numbers have ONE home — the clamp inside :func:`rgbw_line_mode_to_regs` —
    so this asks that function whether it would have to clamp, instead of
    restating 1024 / 400 / 3072 here. ``ui_mode`` only resolves the effective
    pixel format; the single/dual proxy below follows ``led_count1``, because
    that is what the shared-pool clamp keys on.
    """
    n0 = int(led_count0)
    n1 = int(led_count1)
    regs = rgbw_line_mode_to_regs(
        RGBW_LINE_UI_DUAL if n1 else RGBW_LINE_UI_SINGLE,
        MB2WS_LED_TYPE_WS2812_0,
        n0,
        n1,
        rgbw_effective_pixel_format(ui_mode, pixel_format),
    )
    return regs[MB2WS_LED_COUNT0] == n0 and regs[MB2WS_LED_COUNT1] == n1


def rgbw_matrix_type_writes(
    width: int,
    height: int,
    tile_count: int,
    *,
    pixel_format: int = 0,
    led_count1: int = 0,
    ui_mode: str = RGBW_LINE_UI_SINGLE,
) -> Optional[Dict[int, int]]:
    """Type + count → register writes, or ``None`` when the combination is refused.

    Always reg 417 (packed W|H). With an explicit count also reg 400 = W×H×N;
    with TileCount = auto reg 400 is left alone (firmware derives N from it).
    ``None`` means the pixels do not fit the pool — the caller refuses the
    selection rather than writing a value firmware would clamp or reject.
    """
    out: Dict[int, int] = {MB2WS_MATRIX_WIDTH: rgbw_matrix_pack(width, height)}
    total = rgbw_matrix_type_led_count(width, height, tile_count)
    if total is None:
        return out
    if not rgbw_matrix_fits_pool(
        total, pixel_format=pixel_format, led_count1=led_count1, ui_mode=ui_mode
    ):
        return None
    out[MB2WS_LED_COUNT0] = total
    return out


def rgbw_clamp_byte_order(byte_order: int) -> int:
    return max(0, min(MB2WS_BYTE_ORDER_MAX, int(byte_order)))


def rgbw_clamp_pwm_mode(mode: int) -> int:
    m = int(mode)
    if m in RGBW_PWM_MODE_CODES:
        return m
    return RGBW_PWM_MODE_RGBW


# ---------------------------------------------------------------------------
# Settings batches
# ---------------------------------------------------------------------------

# Settings registers whose firmware validation reads the device's CURRENT value
# of another register — so the batch order is a correctness constraint, not a
# style choice. See rgbw_settings_write_order() for the per-step reason.
MB2WS_WRITE_ORDER_PRIORITY: Tuple[int, ...] = (
    MB2WS_LINE_MODE,      # 413
    MB2WS_PIXEL_FORMAT,   # 403
    MB2WS_LED_TYPE,       # 401
    MB2WS_LED_COUNT0,     # 400
    MB2WS_LED_COUNT1,     # 414
    MB2WS_CH2_MODE,       # 419
)


def rgbw_settings_write_order(regs: Iterable[int]) -> List[int]:
    """Order a settings batch by DEPENDENCY, not by address — the ONE home.

    Firmware validates several of these registers against the value another one
    **currently holds on the device**, so a batch in the wrong order is silently
    clamped or answered with exception 3. A plain ``sorted()`` satisfies some of
    those constraints by accident (400 < 414) and violates another (400 < 403),
    so the order is spelled out here, once:

    1. **413 LineMode** — leaving DUAL_WS auto-zeroes 414 and 419, and entering it
       is the precondition for both; it also coerces LedType
       (``svc_mb_mb2ws.c:648-666``).
    2. **403 PixelFormat**, then **401 LedType** — 403 sets the pixel caps every
       count is then validated against (``mb2ws_max_pixels`` reads the stored
       format, ``:92-97``; ``mb2ws_max_strip1_pixels`` ``:99-101``), and 401 is
       validated against the *current* LineMode (``:233-244``).
    3. **400 LedCount0**, then **414 LedCount1** — 400 is validated/clamped
       against the current format (``:228-232``, clamp ``:643-647``) and 414
       against the current LineMode **and** the current n0 (``:248-252``).
    4. **419 Ch2Mode** — non-zero requires the current LineMode to be DUAL_WS
       (``mb2ws_pool_ch2_mode_write_valid``, called at ``:621``).
    5. everything else in ascending address order — stable, so registers with no
       dependency keep exactly the behaviour they had.

    Reg 410 (Lock) is not ordered here: the bracket is
    :func:`rgbw_lock_bracket_writes`. Without the 403 → 400 step, widening the
    format and the count in one batch (RGBW → RGB, or entering APA102 while RGBW
    is still selected) lands on the narrower clamp.

    **What ordering alone cannot fix** (stated rather than implied): 400's own
    validator also pool-checks against the device's CURRENT LedCount1
    (``:228-232``), while 414 pool-checks against the current LedCount0 — a true
    cycle when a batch GROWS n0 and SHRINKS n1 while staying in DUAL_WS. No
    single order satisfies both; firmware then REJECTS the 400 write (exception,
    not a silent clamp) and the batch writer reports the failure to the user.
    Leaving DUAL_WS is not affected: step 1 zeroes n1 on the device first.
    Splitting such a batch into a shrink pass and a grow pass is the only real
    fix, and it is not attempted here.
    """
    rank = {int(reg): i for i, reg in enumerate(MB2WS_WRITE_ORDER_PRIORITY)}
    tail = len(rank)
    return sorted(
        (int(r) for r in regs), key=lambda r: (rank.get(r, tail), r)
    )


def rgbw_line_mode_to_regs(
    ui_mode: str,
    led_type: int,
    led_count0: int,
    led_count1: int = 0,
    pixel_format: int = 0,
    byte_order: Optional[int] = None,
    auto_refresh: Optional[int] = None,
) -> Dict[int, int]:
    """Map UI line mode → holding writes (caller brackets with unlock/lock).

    Pixel counts clamp to the firmware caps, format-aware: LedCount0 ≤ 1024 RGB /
    400 RGBW, LedCount1 ≤ 512 RGB / 384 RGBW, and the combined shared pool
    ``(n0+n1)*bpp ≤ 3072`` (bpp 4 RGBW / 3 RGB) — n0 is primary, so an over-pool
    sum clamps n1 down. Firmware fail-closes over-pool (exc 3); this avoids it.

    The format used for those caps is the **effective** one
    (:func:`rgbw_effective_pixel_format`), resolved BEFORE the clamps: this
    function writes ``PixelFormat = RGB`` in the APA102 mode, so costing the same
    pixels at 4 bytes while emitting a 3-byte format would clamp a count the
    device would have taken — the accept-check
    (:func:`rgbw_matrix_fits_pool`) reads the same rule, so the two can never
    disagree.
    """
    mode = str(ui_mode or RGBW_LINE_UI_SINGLE)
    lt = int(led_type) & 0xFFFF
    pf = rgbw_effective_pixel_format(mode, pixel_format)
    bpp = 4 if pf else 3
    n0 = max(0, min(MB2WS_MAX_PX0_RGBW if pf else MB2WS_MAX_PX0_RGB, int(led_count0)))
    n1 = max(0, min(MB2WS_MAX_PX1_RGBW if pf else MB2WS_MAX_PX1_RGB, int(led_count1)))
    # Combined-pool guard: keep n0 (primary), clamp n1 to whatever pool remains.
    if (n0 + n1) * bpp > MB2WS_POOL_BYTES:
        n1 = max(0, MB2WS_POOL_BYTES // bpp - n0)
    if mode == RGBW_LINE_UI_DUAL:
        out = {
            MB2WS_LINE_MODE: MB2WS_LINE_DUAL_WS,
            MB2WS_LED_TYPE: lt if lt != MB2WS_LED_TYPE_APA102 else MB2WS_LED_TYPE_WS2812_0,
            MB2WS_LED_COUNT0: n0,
            MB2WS_LED_COUNT1: n1,
            MB2WS_PIXEL_FORMAT: pf,
        }
    elif mode == RGBW_LINE_UI_APA102:
        out = {
            MB2WS_LINE_MODE: MB2WS_LINE_SPI_CLOCKED,
            MB2WS_LED_TYPE: MB2WS_LED_TYPE_APA102,
            MB2WS_LED_COUNT0: n0,
            MB2WS_LED_COUNT1: 0,
            # pf is already 0 here by rgbw_effective_pixel_format — written from
            # the same variable the caps were computed with, never a literal.
            MB2WS_PIXEL_FORMAT: pf,
        }
    else:
        out = {
            MB2WS_LINE_MODE: MB2WS_LINE_SINGLE_WS,
            MB2WS_LED_TYPE: lt,
            MB2WS_LED_COUNT0: n0,
            MB2WS_LED_COUNT1: 0,
            MB2WS_PIXEL_FORMAT: pf,
        }
    if byte_order is not None:
        out[MB2WS_BYTE_ORDER] = rgbw_clamp_byte_order(byte_order)
    if auto_refresh is not None:
        out[MB2WS_AUTO_REFRESH] = 1 if int(auto_refresh) else 0
    return out


def rgbw_regs_to_line_ui(line_mode: int, led_type: int) -> str:
    lm = int(line_mode) & 0xFFFF
    if lm == MB2WS_LINE_DUAL_WS:
        return RGBW_LINE_UI_DUAL
    if lm == MB2WS_LINE_SPI_CLOCKED or int(led_type) == MB2WS_LED_TYPE_APA102:
        return RGBW_LINE_UI_APA102
    return RGBW_LINE_UI_SINGLE


def rgbw_scene_mode_to_regs(
    ui_mode: str,
    *,
    fx_id: int = 0,
    fx_speed: int = 128,
    fx_param: int = 255,
    flash_slot: int = 0,
    loop: bool = False,
    scale: Optional[int] = None,
) -> Dict[int, int]:
    """Map UI scene mode → holding writes (settings; PlayCtrl separate)."""
    mode = str(ui_mode or RGBW_SCENE_UI_POOL)
    opts = MB2WS_OPT_LOOP if loop else 0
    out: Dict[int, int] = {MB2WS_OPTIONS: opts}
    if scale is not None:
        out[MB2WS_SCALE] = max(0, min(1000, int(scale)))
    if mode == RGBW_SCENE_UI_FX:
        out[MB2WS_RENDER_SOURCE] = MB2WS_RENDER_FX
        out[MB2WS_FX_ID] = rgbw_clamp_fx_id(fx_id)
        out[MB2WS_FX_SPEED] = max(0, min(255, int(fx_speed)))
        out[MB2WS_FX_PARAM] = max(0, min(255, int(fx_param)))
        return out
    if mode == RGBW_SCENE_UI_FLASH:
        out[MB2WS_RENDER_SOURCE] = MB2WS_RENDER_FLASH
        out[MB2WS_FLASH_SLOT] = max(0, min(31, int(flash_slot)))
        return out
    out[MB2WS_RENDER_SOURCE] = MB2WS_RENDER_POOL
    return out


def rgbw_stop_blank_writes() -> List[Tuple[int, int, bool]]:
    """Ordered register writes that stop playback AND blank the strip.

    Returns ``(register, value, lock_protected)`` triples. When
    ``lock_protected`` is True the caller must unlock ``MB2WS_LOCK`` before the
    write; CMD writes are always accepted.

    Sequence (order matters):
      1. PlayCtrl = STOP          — halt the running effect (LOCKED: 416 is inside
                                    the settings block 400..419, so a locked device
                                    rejects a plain write and the strip keeps playing)
      2. RenderSource = POOL      — point rendering at the pixel pool (locked)
      3. CMD = CLEAR_POOL         — clear the pixel pool to black (unlocked)
      4. CMD = REFRESH            — latch the black frame out to the LEDs (unlocked)

    CLEAR_POOL alone latches: firmware sets both ``b_pool_dirty`` and
    ``b_refresh_pending`` when it handles it (svc_mb_mb2ws.c:891-896). The
    trailing REFRESH is therefore redundant; it is kept deliberately — it costs
    one write and makes the blank hold if a future firmware ever splits the two
    flags.

    The ``lock_protected`` column is DERIVED from
    :func:`rgbw_reg_is_lock_gated`, never hand-typed: a hand-typed column is
    exactly how 416 lost its unlock on the desktop and the strip kept playing
    through a reported "stop".
    """
    plan = [
        (MB2WS_PLAY_CTRL, MB2WS_PLAY_STOP),
        (MB2WS_RENDER_SOURCE, MB2WS_RENDER_POOL),
        (MB2WS_CMD, MB2WS_CMD_CLEAR_POOL),
        (MB2WS_CMD, MB2WS_CMD_REFRESH),
    ]
    return [(reg, val, rgbw_reg_is_lock_gated(reg)) for reg, val in plan]


def rgbw_regs_to_scene_ui(render_source: int) -> str:
    rs = int(render_source) & 0xFFFF
    if rs == MB2WS_RENDER_FX:
        return RGBW_SCENE_UI_FX
    if rs == MB2WS_RENDER_FLASH:
        return RGBW_SCENE_UI_FLASH
    return RGBW_SCENE_UI_POOL

# -*- coding: utf-8 -*-
"""LED / MB2WS map unit tests — the five non-negotiables first, then the port.

Ported from the desktop flasher (MR-02m-flasher, branch `feat/led-spy-and-picker`,
tests/test_rgbw_module_config.py) and extended with the board-side rules the
plan named. There is no bench fixture here on purpose: no type-120 device
answered COM3 at either baud during this branch's scan, so every assertion below
is arithmetic over the shipped map — nothing claims a wire observation.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_led import controls as cc  # noqa: E402
from sa02m_led import led_mb2ws as lm  # noqa: E402
from sa02m_led import led_mb2ws_map as lmap  # noqa: E402


class TestNonNegotiable1UnlockOrderLock(unittest.TestCase):
    """§1 — unlock 410 = 0x10C8 → ordered writes → lock 410 = 0, for 400..419."""

    def test_unlock_key_and_span(self):
        self.assertEqual(lm.MB2WS_LOCK, 410)
        self.assertEqual(lm.MB2WS_UNLOCK_KEY, 0x10C8)
        self.assertEqual(lm.MB2WS_LOCK_GATED_FIRST, 400)
        self.assertEqual(lm.MB2WS_LOCK_GATED_LAST, 419)

    def test_every_settings_register_is_lock_gated(self):
        for reg in range(400, 420):
            if reg == lm.MB2WS_LOCK:
                # The lock itself is the one settings register writable while locked.
                self.assertFalse(lm.rgbw_reg_is_lock_gated(reg))
                continue
            self.assertTrue(lm.rgbw_reg_is_lock_gated(reg), reg)

    def test_play_ctrl_416_is_lock_gated(self):
        """The Stop fail-open: 416 inside 400..419, so a plain write is refused."""
        self.assertEqual(lm.MB2WS_PLAY_CTRL, 416)
        self.assertTrue(lm.rgbw_reg_is_lock_gated(lm.MB2WS_PLAY_CTRL))

    def test_always_writable_registers_are_not_lock_gated(self):
        for reg in (
            lm.MB2WS_CMD, lm.MB2WS_TOD_HOURS, lm.MB2WS_FX_AUX, lm.MB2WS_FX_DENSITY,
            lm.MB2WS_SCALE, lm.MB2WS_WX_TEMP_COLOR, lm.MB2WS_TEXT_LINES,
            lm.MB2WS_TEXT_BASE, lm.MB2WS_PIXEL_POOL_BASE,
        ):
            self.assertFalse(lm.rgbw_reg_is_lock_gated(reg), reg)

    def test_bracket_is_unlock_ordered_lock(self):
        plan = lm.rgbw_lock_bracket_writes({400: 256, 403: 1, 413: 1, 414: 8, 419: 2, 401: 0})
        self.assertEqual(plan[0], (lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY))
        self.assertEqual(plan[-1], (lm.MB2WS_LOCK, 0))
        self.assertEqual([r for r, _v in plan[1:-1]], [413, 403, 401, 400, 414, 419])

    def test_bracket_covers_a_bare_stop(self):
        """A Stop-only batch still unlocks — the fail-open the desktop shipped."""
        plan = lm.rgbw_lock_bracket_writes({lm.MB2WS_PLAY_CTRL: lm.MB2WS_PLAY_STOP})
        self.assertEqual(
            plan,
            [(410, 0x10C8), (416, 0), (410, 0)],
        )

    def test_bracket_left_off_when_nothing_is_gated(self):
        plan = lm.rgbw_lock_bracket_writes({lm.MB2WS_FX_AUX: 5, lm.MB2WS_FX_DENSITY: 128})
        self.assertEqual(plan, [(455, 5), (456, 128)])

    def test_stop_blank_plan_marks_the_two_gated_writes(self):
        plan = lm.rgbw_stop_blank_writes()
        self.assertEqual(
            plan,
            [
                (lm.MB2WS_PLAY_CTRL, lm.MB2WS_PLAY_STOP, True),
                (lm.MB2WS_RENDER_SOURCE, lm.MB2WS_RENDER_POOL, True),
                (lm.MB2WS_CMD, lm.MB2WS_CMD_CLEAR_POOL, False),
                (lm.MB2WS_CMD, lm.MB2WS_CMD_REFRESH, False),
            ],
        )


class TestNonNegotiable2TextBase(unittest.TestCase):
    """§2 — never write marquee text below 516 (495 drove safe-AO 503..506)."""

    def test_text_span(self):
        self.assertEqual(lm.rgbw_text_reg_span(), (516, 579))
        self.assertEqual(lm.MB2WS_TEXT_REG_COUNT, 64)
        self.assertEqual(lm.MB2WS_TEXT_MAX_CHARS, 128)

    def test_span_clears_the_safe_ao_block(self):
        first, last = lm.rgbw_text_reg_span()
        self.assertTrue(lm.rgbw_text_span_clears_safe_ao())
        for reg in range(lm.RGBW_PWM_SAFE_BASE, lm.RGBW_PWM_SAFE_LAST + 1):
            self.assertFalse(first <= reg <= last, reg)

    def test_the_legacy_base_would_have_overlapped(self):
        """The regression this floor exists for, stated as arithmetic."""
        legacy_last = lm.MB2WS_TEXT_LEGACY_BASE + 16 - 1  # 495..510, the old window
        self.assertLessEqual(lm.MB2WS_TEXT_LEGACY_BASE, lm.RGBW_PWM_SAFE_BASE)
        self.assertGreaterEqual(legacy_last, lm.RGBW_PWM_SAFE_LAST)


class TestNonNegotiable3NoCmd3(unittest.TestCase):
    """§3 — never issue CMD 3: the range check takes it, the handler refuses it."""

    def test_cmd3_is_refused(self):
        self.assertEqual(lm.MB2WS_CMD_SAVE_FLASH_DEPRECATED, 3)
        self.assertFalse(lm.rgbw_cmd_is_allowed(3))
        self.assertNotIn(3, lm.RGBW_CMD_ALLOWED)

    def test_the_other_four_codes_are_allowed(self):
        for cmd in (0, 1, 2, 4):
            self.assertTrue(lm.rgbw_cmd_is_allowed(cmd), cmd)

    def test_no_plan_in_this_package_emits_cmd3(self):
        for reg, val, _gated in lm.rgbw_stop_blank_writes():
            if reg == lm.MB2WS_CMD:
                self.assertTrue(lm.rgbw_cmd_is_allowed(val))


class TestNonNegotiable4ModeDataRead(unittest.TestCase):
    """§4 — exactly ONE FC03 of 15 regs at 453; widening past 467 fails the block."""

    def test_span_is_453_by_15(self):
        base, count = lm.rgbw_mode_data_read_span()
        self.assertEqual((base, count), (453, 15))
        self.assertEqual(base + count - 1, lm.MB2WS_WX_TEMP_COLOR)
        self.assertEqual(lm.MB2WS_WX_TEMP_COLOR, 467)

    def test_scale_460_sits_inside_the_block(self):
        base, count = lm.rgbw_mode_data_read_span()
        self.assertTrue(base <= lm.MB2WS_SCALE < base + count)

    def test_registers_needing_their_own_read_are_outside(self):
        base, count = lm.rgbw_mode_data_read_span()
        for reg in (lm.MB2WS_TEXT_LINES, lm.MB2WS_TEXT_BASE):
            self.assertFalse(base <= reg < base + count, reg)

    def test_a_short_block_decodes_to_none_not_garbage(self):
        self.assertIsNone(lm.rgbw_mode_data_decode([0] * 14))

    def test_sentinels_decode_to_none(self):
        regs = [0] * 15
        regs[lm.MB2WS_TOD_HOURS - 453] = lm.MB2WS_TOD_UNSET
        regs[lm.MB2WS_TOD_MINUTES - 453] = lm.MB2WS_TOD_UNSET
        regs[lm.MB2WS_WX_TEMP - 453] = lm.MB2WS_WX_TEMP_UNSET
        regs[lm.MB2WS_WX_HUM - 453] = lm.MB2WS_WX_HUM_UNSET
        d = lm.rgbw_mode_data_decode(regs)
        self.assertIsNotNone(d)
        for field in (d.tod_hours, d.tod_minutes, d.wx_temp_c, d.wx_hum_pct,
                      d.wx_press_mmhg, d.wx_year, d.wx_day, d.wx_month):
            self.assertIsNone(field)

    def test_real_values_decode(self):
        regs = [0] * 15
        regs[0] = 13                      # 453 hours
        regs[1] = 45                      # 454 minutes
        regs[lm.MB2WS_FX_AUX - 453] = 0x0310
        regs[lm.MB2WS_FX_DENSITY - 453] = 200
        regs[lm.MB2WS_WX_DATE - 453] = (17 << 8) | 3
        regs[lm.MB2WS_WX_TEMP - 453] = 0x10000 - 55   # -5.5 °C as int16
        regs[lm.MB2WS_WX_HUM - 453] = 615
        regs[lm.MB2WS_WX_PRESS - 453] = 7412
        regs[lm.MB2WS_WX_YEAR - 453] = 2026
        d = lm.rgbw_mode_data_decode(regs)
        self.assertEqual((d.tod_hours, d.tod_minutes), (13, 45))
        self.assertEqual((d.wx_day, d.wx_month, d.wx_year), (17, 3, 2026))
        self.assertAlmostEqual(d.wx_temp_c, -5.5)
        self.assertAlmostEqual(d.wx_hum_pct, 61.5)
        self.assertAlmostEqual(d.wx_press_mmhg, 741.2)
        self.assertEqual(d.fx_aux, 0x0310)
        self.assertEqual(d.fx_density, 200)


class TestNonNegotiable5Reg418ReadModifyWrite(unittest.TestCase):
    """§5 — 418 is read-modify-write; unknown prior + unknown field ⇒ skip it."""

    def test_unknown_prior_without_overrides_skips_the_register(self):
        self.assertIsNone(lm.rgbw_matrix_layout_write_value(None, 0x05))
        self.assertIsNone(lm.rgbw_matrix_layout_write_value_ex(None, 0x05))
        self.assertIsNone(lm.rgbw_matrix_layout_write_value_ex(None, 0x05, tile_mode=1))
        self.assertIsNone(lm.rgbw_matrix_layout_write_value_ex(None, 0x05, tile_count=2))

    def test_unknown_prior_with_both_overrides_composes(self):
        v = lm.rgbw_matrix_layout_write_value_ex(None, 0x05, tile_mode=2, tile_count=3)
        self.assertEqual(v, 0x05 | (2 << 4) | (3 << 8))

    def test_known_prior_preserves_the_tiling_fields(self):
        prior = (1 << 4) | (4 << 8) | 0x0A  # TileMode 1, TileCount 4, some wiring
        v = lm.rgbw_matrix_layout_write_value(prior, 0x05)
        self.assertEqual(lm.rgbw_matrix_layout_tilemode(v), 1)
        self.assertEqual(lm.rgbw_matrix_layout_tilecount(v), 4)
        self.assertEqual(v & lm.MB2WS_MATRIX_LAYOUT_WIRING_MASK, 0x05)

    def test_out_of_range_device_fields_fold_rather_than_raise(self):
        prior = (7 << 4) | (15 << 8)  # a misbehaving device
        v = lm.rgbw_matrix_layout_write_value_ex(prior, 0x01)
        self.assertEqual(lm.rgbw_matrix_layout_tilemode(v), 0)
        self.assertEqual(lm.rgbw_matrix_layout_tilecount(v), 0)

    def test_out_of_range_ui_overrides_raise(self):
        with self.assertRaises(ValueError):
            lm.rgbw_matrix_layout_compose_full(0, 3, 0)
        with self.assertRaises(ValueError):
            lm.rgbw_matrix_layout_compose_full(0, 0, 5)

    def test_4tiles_90cw_is_swap_xy_without_mirror_x(self):
        """4 tiles + 90° CW + no MIRROR_X → 0x0408 (bit 3, not bit 2)."""
        v = lm.rgbw_matrix_layout_4tiles_90cw()
        self.assertEqual(v, 0x0408)
        self.assertEqual(v & lm.MB2WS_MATRIX_LAYOUT_MIRROR_X, 0)
        self.assertEqual(v & lm.MB2WS_MATRIX_LAYOUT_SWAP_XY, lm.MB2WS_MATRIX_LAYOUT_SWAP_XY)
        self.assertEqual(v & lm.MB2WS_MATRIX_LAYOUT_ROTATE_90_CW, lm.MB2WS_MATRIX_LAYOUT_ROTATE_90_CW)
        self.assertEqual(lm.rgbw_matrix_layout_tilecount(v), 4)
        self.assertEqual(lm.rgbw_matrix_layout_tilemode(v), 0)
        self.assertNotEqual(v, 0x0404)  # previous bench: tiles + MIRROR_X only
        self.assertEqual(lm.rgbw_matrix_layout_4tiles_90cw(mirror_x=True), 0x040C)

    def test_weather_two_line_is_register_494_value_2(self):
        self.assertEqual(lm.MB2WS_TEXT_LINES, 494)
        self.assertEqual(lm.MB2WS_TEXT_LINES_DOUBLE, 2)
        self.assertEqual(lm.rgbw_text_lines_ui_code(0), lm.MB2WS_TEXT_LINES_SINGLE)
        self.assertEqual(lm.rgbw_text_lines_ui_code(2), lm.MB2WS_TEXT_LINES_DOUBLE)
        self.assertEqual(lm.rgbw_wx_lines_choices()[-1][0], lm.MB2WS_TEXT_LINES_DOUBLE)

    def test_yaml_bench_recipe_is_0x0408_and_two_lines(self):
        layout = lm.rgbw_layout_from_yaml(
            {"tile_count": 4, "rotate_90_cw": True, "mirror_x": False}
        )
        self.assertEqual(layout, 0x0408)
        self.assertEqual(lm.rgbw_layout_from_yaml({"matrix_layout": 0x0408}), 0x0408)
        self.assertIsNone(lm.rgbw_layout_from_yaml({}))
        self.assertEqual(lm.rgbw_text_lines_from_yaml({"weather_lines": 2}), 2)
        self.assertEqual(lm.rgbw_text_lines_from_yaml({"text_lines": 0}), 1)
        self.assertIsNone(lm.rgbw_text_lines_from_yaml({}))


class TestWriteOrder(unittest.TestCase):
    """The batch order is a correctness constraint — never `sorted()`."""

    def test_dependency_order(self):
        regs = [400, 401, 403, 412, 413, 414, 419]
        self.assertEqual(
            lm.rgbw_settings_write_order(regs), [413, 403, 401, 400, 414, 419, 412]
        )

    def test_plain_sorted_would_violate_403_before_400(self):
        regs = [400, 403]
        self.assertEqual(lm.rgbw_settings_write_order(regs), [403, 400])
        self.assertNotEqual(lm.rgbw_settings_write_order(regs), sorted(regs))

    def test_unranked_registers_keep_ascending_order(self):
        self.assertEqual(
            lm.rgbw_settings_write_order([460, 409, 404, 411]), [404, 409, 411, 460]
        )


class TestFxCatalog(unittest.TestCase):
    def test_82_modes_not_81(self):
        """The code wins over the desktop note: id 81 «Индикатор линии» exists."""
        self.assertEqual(lm.RGBW_FX_MODE_COUNT, 82)
        self.assertEqual(lm.RGBW_FX_MODE_MB_SPY, 81)
        self.assertEqual(len(lm.rgbw_fx_mode_choices()), 82)

    def test_group_ranges_tile_0_to_81_exactly(self):
        covered = []
        for (_gid, _key, first, last) in lmap.RGBW_FX_GROUP_RANGES:
            covered.extend(range(first, last + 1))
        self.assertEqual(covered, list(range(lm.RGBW_FX_MODE_COUNT)))
        self.assertEqual(len(lm.rgbw_fx_group_choices()), 10)

    def test_group_membership_matches_the_ranges(self):
        for (gid, _key, first, last) in lmap.RGBW_FX_GROUP_RANGES:
            for fx in range(first, last + 1):
                self.assertEqual(lm.rgbw_fx_group_of(fx), gid, fx)

    def test_gyver_aliases_resolve(self):
        self.assertEqual(lm.rgbw_resolve_fx_id(128), 0)
        self.assertEqual(lm.rgbw_resolve_fx_id(128 + 81), 81)
        self.assertEqual(lm.rgbw_resolve_fx_id(210), lm.RGBW_FX_MODE_STATIC)
        self.assertEqual(lm.rgbw_resolve_fx_id(82), lm.RGBW_FX_MODE_STATIC)
        self.assertEqual(lm.rgbw_clamp_fx_id(999), 81)


class TestFxAux(unittest.TestCase):
    """Reg 455 has six layouts — never hand-pack it."""

    def test_spec_union_equals_the_renderer_set(self):
        derived = tuple(
            fx for fx in range(lm.RGBW_FX_MODE_COUNT) if lm.rgbw_fx_uses_aux(fx)
        )
        self.assertEqual(derived, lmap.RGBW_FX_AUX_MODES)

    def test_escort_high_byte_is_a_length_not_a_colour(self):
        spec = lm.rgbw_fx_aux_spec(51)
        self.assertEqual(spec.high_kind, lm.RGBW_FX_AUX_HIGH_LENGTH)
        self.assertEqual(lm.rgbw_fx_aux_compose(51, low=3, flag=True, high=90), (90 << 8) | 0x83)
        self.assertEqual(lm.rgbw_fx_aux_decompose(51, (90 << 8) | 0x83), (3, True, 90))

    def test_a_mode_ignoring_the_high_byte_writes_zero_there(self):
        """The colour combo can never leak into a mode that reads a length."""
        for fx in (0, 5, 30):
            self.assertEqual(lm.rgbw_fx_aux_compose(fx, low=7, high=6), 7)

    def test_level_75_packs_percent_and_orientation(self):
        self.assertEqual(lm.rgbw_fx_aux_compose(75, low=100, flag=True, high=2), (2 << 8) | 0xE4)
        self.assertEqual(lm.rgbw_fx_aux_decompose(75, (2 << 8) | 0xE4), (100, True, 2))

    def test_hazard_70_is_a_flag_only(self):
        self.assertEqual(lm.rgbw_fx_aux_compose(70, flag=True), 1)
        self.assertEqual(lm.rgbw_fx_aux_decompose(70, 1)[1], True)

    def test_out_of_range_style_folds_to_the_first_choice(self):
        low, _flag, _high = lm.rgbw_fx_aux_decompose(62, 0x00FF)
        self.assertEqual(low, 0)

    def test_scale_control_is_hidden_for_every_mode(self):
        for fx in range(lm.RGBW_FX_MODE_COUNT):
            self.assertFalse(lm.rgbw_fx_control_visibility(fx)[lm.RGBW_CTL_SCALE], fx)
        self.assertFalse(lm.rgbw_scale_is_honoured())

    def test_visibility_map_covers_every_declared_control(self):
        vis = lm.rgbw_fx_control_visibility(62)
        self.assertEqual(sorted(vis), sorted(lm.RGBW_MODE_SPECIFIC_CONTROLS))


class TestTextWindow(unittest.TestCase):
    def test_cp1251_round_trip_high_byte_first(self):
        regs = lm.rgbw_pack_text_cp1251("Тест")
        self.assertEqual(len(regs), 64)
        self.assertEqual(lm.rgbw_unpack_text_cp1251(regs), "ТЕСТ")

    def test_odd_length_pads_the_low_byte(self):
        regs = lm.rgbw_pack_text_cp1251("A")
        self.assertEqual(regs[0], ord("A") << 8)
        self.assertEqual(regs[1], 0)

    def test_truncation_at_128_chars(self):
        long = "X" * 200
        self.assertFalse(lm.rgbw_text_within_limit(long))
        self.assertEqual(len(lm.rgbw_truncate_text(long)), 128)
        self.assertEqual(len(lm.rgbw_unpack_text_cp1251(lm.rgbw_pack_text_cp1251(long))), 128)

    def test_empty_window_reads_back_empty(self):
        self.assertEqual(lm.rgbw_unpack_text_cp1251([0] * 64), "")

    def test_2x_capacity_rounds_the_trailing_gap_away(self):
        # 64 px fits 5 glyphs, not 4: the gap sits between glyphs.
        self.assertEqual(lm.rgbw_text_2x_capacity(64), 5)
        self.assertEqual(lm.rgbw_text_2x_capacity(9), 0)
        self.assertEqual(lm.rgbw_text_2x_hint_args(), (13, 64, 5))

    def test_text_lines_folds_0_and_1_together(self):
        self.assertEqual(lm.rgbw_text_lines_ui_code(0), lm.MB2WS_TEXT_LINES_SINGLE)
        self.assertEqual(lm.rgbw_text_lines_ui_code(1), lm.MB2WS_TEXT_LINES_SINGLE)
        self.assertEqual(lm.rgbw_text_lines_ui_code(2), lm.MB2WS_TEXT_LINES_DOUBLE)


class TestColour(unittest.TestCase):
    def test_rgb565_round_trip_is_stable(self):
        for hexs in ("#000000", "#FFFFFF", "#FF8000", "#123456"):
            v = lm.rgbw_hex_to_rgb565(hexs)
            self.assertEqual(lm.rgbw_hex_to_rgb565(lm.rgbw_rgb565_to_hex(v)), v)

    def test_malformed_hex_is_the_firmware_default(self):
        self.assertEqual(lm.rgbw_hex_to_rgb565("nope"), 0)
        self.assertEqual(lm.rgbw_hex_to_rgb565(""), 0)

    def test_permille_bridge_rounds_rather_than_floors(self):
        v = lm.rgbw_rgb_permille_to_rgb565(1000, 500, 0)
        back = lm.rgbw_rgb565_to_rgb_permille(v)
        again = lm.rgbw_rgb_permille_to_rgb565(*back)
        self.assertEqual(again, v)  # no drift on a second trip


class TestPwmColourBridge(unittest.TestCase):
    """The PWM triple 33..35 is permille; MQTT publishes 8-bit hex."""

    def test_the_eight_bit_round_trip_is_exact_over_the_whole_domain(self):
        # A colour written from MQTT is echoed at once and re-read on the next
        # poll. If the two disagree by one step anywhere, the tile flickers
        # between two values forever and the writeback guard just hides it.
        for v in range(256):
            self.assertEqual(
                lm.rgbw_permille_to_rgb8(lm.rgbw_rgb8_to_permille(v)), v)

    def test_the_bridges_clamp_rather_than_wrap(self):
        self.assertEqual(lm.rgbw_rgb8_to_permille(-5), 0)
        self.assertEqual(lm.rgbw_rgb8_to_permille(999), lm.RGBW_PWM_PERMILLE_MAX)
        self.assertEqual(lm.rgbw_permille_to_rgb8(-5), 0)
        self.assertEqual(lm.rgbw_permille_to_rgb8(99999), 255)

    def test_hex_round_trips_through_permille(self):
        for hexs in ("#000000", "#FFFFFF", "#FF8000", "#123456", "#010203"):
            triple = lm.rgbw_hex_to_pwm_permille(hexs)
            self.assertEqual(lm.rgbw_pwm_permille_to_hex(*triple), hexs)

    def test_the_pwm_hex_path_does_not_go_through_rgb565(self):
        # RGB565 quantises to 5/6/5 bits. Routing the PWM triple through it
        # would throw away most of the strip's resolution: #010203 collapses to
        # black on the way out.
        self.assertEqual(lm.rgbw_pwm_permille_to_hex(
            *lm.rgbw_hex_to_pwm_permille("#010203")), "#010203")
        self.assertNotEqual(
            lm.rgbw_rgb565_to_hex(lm.rgbw_hex_to_rgb565("#010203")), "#010203")

    def test_a_malformed_hex_is_refused_not_defaulted(self):
        # This one feeds a Modbus write, unlike the text-colour sibling: a
        # malformed payload must be REFUSED, never silently rendered as black.
        for bad in ("", "nope", "#12345", "#GGGGGG", "FF8000", None):
            self.assertIsNone(lm.rgbw_hex_to_pwm_permille(bad), bad)
        # The text-colour path deliberately keeps the other behaviour.
        self.assertEqual(lm.rgbw_hex_to_rgb565("nope"), 0)


class TestLiveAnalogInputs(unittest.TestCase):
    """UNVERIFIED family scales — pinned so a bench correction is one edit."""

    def test_vled_uses_the_family_hundredths_of_a_volt(self):
        self.assertEqual(lm.rgbw_vled_volts(1198), 11.98)
        self.assertEqual(lm.rgbw_vled_volts(0), 0.0)

    def test_the_ntc_reading_is_signed(self):
        # Unsigned, −5.0 °C reads as +6549.1 °C — a plausible-looking number on
        # a tile is worse than none.
        self.assertEqual(lm.rgbw_ntc_celsius(315), 31.5)
        self.assertEqual(lm.rgbw_ntc_celsius(0x10000 - 50), -5.0)

    def test_the_live_di_block_is_the_family_input_base(self):
        # Product-low-map relocation moved the DI CONFIGURATION and the press
        # counters; the live state stays on the family block, so the two must
        # not be confused.
        self.assertEqual(lm.RGBW_DI_INPUT_BASE, 18)
        self.assertNotEqual(lm.RGBW_DI_INPUT_BASE, lm.RGBW_DI_MODE_BASE)
        self.assertNotEqual(lm.RGBW_DI_INPUT_BASE, lm.RGBW_DI_CNT_SHORT_BASE)


class TestLineAndSceneMode(unittest.TestCase):
    def test_apa102_forces_rgb_and_zero_second_strip(self):
        regs = lm.rgbw_line_mode_to_regs(lm.RGBW_LINE_UI_APA102, lm.MB2WS_LED_TYPE_SK6812, 300, 100, 1)
        self.assertEqual(regs[lm.MB2WS_PIXEL_FORMAT], 0)
        self.assertEqual(regs[lm.MB2WS_LED_TYPE], lm.MB2WS_LED_TYPE_APA102)
        self.assertEqual(regs[lm.MB2WS_LED_COUNT1], 0)

    def test_rgbw_caps(self):
        regs = lm.rgbw_line_mode_to_regs(lm.RGBW_LINE_UI_SINGLE, 0, 9999, 0, 1)
        self.assertEqual(regs[lm.MB2WS_LED_COUNT0], lm.MB2WS_MAX_PX0_RGBW)

    def test_shared_pool_clamps_the_second_strip(self):
        regs = lm.rgbw_line_mode_to_regs(lm.RGBW_LINE_UI_DUAL, 0, 900, 500, 0)
        self.assertEqual(regs[lm.MB2WS_LED_COUNT0], 900)
        self.assertEqual(regs[lm.MB2WS_LED_COUNT1], 3072 // 3 - 900)

    def test_fits_pool_agrees_with_the_clamp(self):
        self.assertTrue(lm.rgbw_matrix_fits_pool(1024))
        self.assertFalse(lm.rgbw_matrix_fits_pool(1025))
        self.assertFalse(lm.rgbw_matrix_fits_pool(401, pixel_format=1))

    def test_matrix_pack_unpack(self):
        self.assertEqual(lm.rgbw_matrix_pack(16, 16), 0x1010)
        self.assertEqual(lm.rgbw_matrix_unpack(0x1010), (16, 16))
        self.assertEqual(lm.rgbw_matrix_unpack(0x0040), (64, 16))  # height 0 → 16

    def test_matrix_type_writes_refuses_an_over_pool_pick(self):
        self.assertIsNone(lm.rgbw_matrix_type_writes(64, 16, 4, pixel_format=1))
        out = lm.rgbw_matrix_type_writes(16, 16, 2)
        self.assertEqual(out[lm.MB2WS_LED_COUNT0], 512)

    def test_auto_tile_count_leaves_led_count_alone(self):
        out = lm.rgbw_matrix_type_writes(16, 16, 0)
        self.assertNotIn(lm.MB2WS_LED_COUNT0, out)

    def test_scene_round_trip(self):
        regs = lm.rgbw_scene_mode_to_regs(lm.RGBW_SCENE_UI_FX, fx_id=64, fx_speed=300, fx_param=-4)
        self.assertEqual(regs[lm.MB2WS_FX_ID], 64)
        self.assertEqual(regs[lm.MB2WS_FX_SPEED], 255)
        self.assertEqual(regs[lm.MB2WS_FX_PARAM], 0)
        self.assertEqual(lm.rgbw_regs_to_scene_ui(regs[lm.MB2WS_RENDER_SOURCE]), lm.RGBW_SCENE_UI_FX)

    def test_line_ui_round_trip(self):
        self.assertEqual(lm.rgbw_regs_to_line_ui(lm.MB2WS_LINE_DUAL_WS, 0), lm.RGBW_LINE_UI_DUAL)
        self.assertEqual(
            lm.rgbw_regs_to_line_ui(lm.MB2WS_LINE_SINGLE_WS, lm.MB2WS_LED_TYPE_APA102),
            lm.RGBW_LINE_UI_APA102,
        )


class TestClockAndWeatherWrites(unittest.TestCase):
    def test_pc_clock_writes(self):
        w = lm.rgbw_pc_clock_writes(datetime(2026, 9, 3, 14, 7))
        self.assertEqual(w, {453: 14, 454: 7})

    def test_11_05_is_hours_then_minutes_not_swapped(self):
        """A reversed 4×16×16 panel (11:00 shown as 00:11) is MIRROR_X, not this."""
        w = lm.rgbw_pc_clock_writes(datetime(2026, 9, 4, 11, 5))
        self.assertEqual(w, {lm.MB2WS_TOD_HOURS: 11, lm.MB2WS_TOD_MINUTES: 5})
        self.assertNotEqual(w.get(lm.MB2WS_TOD_HOURS), 5)

    def test_04_09_is_day_high_month_low_not_swapped(self):
        """Firmware ``(day<<8)|month``: 04.09 → 0x0409. 0x0904 would paint 09.04."""
        self.assertEqual(lm.rgbw_wx_date_pack(4, 9), 0x0409)
        self.assertEqual(lm.rgbw_wx_date_unpack(0x0409), (4, 9))
        w = lm.rgbw_pc_date_writes(datetime(2026, 9, 4, 11, 5))
        self.assertEqual(w[lm.MB2WS_WX_DATE], 0x0409)
        self.assertNotEqual(w[lm.MB2WS_WX_DATE], 0x0904)

    def test_pc_date_clamps_the_year(self):
        w = lm.rgbw_pc_date_writes(datetime(1999, 12, 31))
        self.assertEqual(w[lm.MB2WS_WX_YEAR], lm.MB2WS_WX_YEAR_MIN)
        self.assertEqual(w[lm.MB2WS_WX_DATE], (31 << 8) | 12)

    def test_unset_writes_both_registers(self):
        self.assertEqual(lm.rgbw_tod_unset_writes(), {453: 0xFFFF, 454: 0xFFFF})

    def test_clock_set_is_a_property_of_the_pair(self):
        self.assertFalse(lm.rgbw_tod_is_set(0xFFFF, 30))
        self.assertFalse(lm.rgbw_tod_is_set(12, 0xFFFF))
        self.assertTrue(lm.rgbw_tod_is_set(0, 0))


class TestSpy(unittest.TestCase):
    def test_slot_and_limit_addresses(self):
        self.assertEqual(lm.rgbw_spy_slot_reg(0, 0), 648)
        self.assertEqual(lm.rgbw_spy_slot_reg(3, 7), 679)
        self.assertEqual(lm.rgbw_spy_lim_reg(3, 1), 695)
        self.assertEqual(lm.rgbw_wx_spy_reg(0, 0), 696)
        self.assertEqual(lm.rgbw_wx_spy_reg(6, 1), 709)

    def test_uid_fc_pack_defaults_to_fc03(self):
        self.assertEqual(lm.rgbw_wx_spy_pack_uid_fc(13, 0), 13 | (3 << 8))
        self.assertEqual(lm.rgbw_wx_spy_unpack_uid_fc(13), (13, 3))

    def test_unit_pack_round_trip(self):
        a, b = lm.rgbw_spy_pack_unit("°C")
        self.assertEqual(lm.rgbw_spy_unpack_unit(a, b), "°C")

    def test_live_i32_is_signed(self):
        self.assertEqual(lm.rgbw_spy_live_i32(0xFFFF, 0xFFFF), -1)
        self.assertEqual(lm.rgbw_spy_live_i32(0, 5), 5)


class TestSignature(unittest.TestCase):
    """Exact-or-prefix, never a substring — the alias list has one home."""

    def test_the_four_aliases(self):
        for sig in ("RGBW_WS2812", "RGBWWS2812", "RGBW", "LED"):
            self.assertTrue(lm.signature_looks_like_led(sig), sig)
            self.assertTrue(lm.signature_looks_like_led(sig.lower()), sig)
            self.assertTrue(lm.signature_looks_like_led(" %s " % sig), sig)

    def test_only_the_two_long_aliases_match_as_a_prefix(self):
        self.assertTrue(lm.signature_looks_like_led("RGBW_WS2812_v2"))
        self.assertTrue(lm.signature_looks_like_led("RGBWWS2812B"))
        # "LED" and "RGBW" are exact-only — see LED_SIGNATURE_PREFIXES.
        self.assertFalse(lm.signature_looks_like_led("LED-64"))
        self.assertFalse(lm.signature_looks_like_led("RGBWX"))

    def test_a_foreign_signature_merely_containing_led_is_not_a_strip(self):
        # "ledGe" is a real Wiren Board signature the flasher's own route test
        # carries; a three-letter prefix match would steal its .wbfw route.
        for sig in ("WB-MLED3", "MYLEDBOX", "LEDGER", "ledGe",
                    "6AI6AO", "CRSTDrAHAQ", "SENS.", ""):
            self.assertFalse(lm.signature_looks_like_led(sig), sig)

    def test_placeholders_are_not_a_strip(self):
        for sig in ("—", "-", "NONE", "?"):
            self.assertFalse(lm.signature_looks_like_led(sig), sig)


class TestControls(unittest.TestCase):
    def test_every_control_name_is_unique(self):
        names = cc.control_names()
        self.assertEqual(len(names), len(set(names)))

    def test_the_plan_named_controls_are_present(self):
        for name in ("power", "brightness", "effect", "speed", "color", "text"):
            self.assertIn(name, cc.control_names())

    def test_writables_are_the_non_readonly_rows(self):
        self.assertEqual(
            set(cc.writable_names()),
            {"power", "brightness", "effect", "speed", "scene_source", "color", "white", "text"},
        )

    def test_registers_point_into_the_map(self):
        self.assertEqual(cc.register_for("brightness"), lm.MB2WS_FX_PARAM)
        self.assertEqual(cc.register_for("effect"), lm.MB2WS_FX_ID)
        self.assertEqual(cc.register_for("speed"), lm.MB2WS_FX_SPEED)
        self.assertEqual(cc.register_for("power"), lm.MB2WS_PLAY_CTRL)
        self.assertIsNone(cc.register_for("di_1"))

    def test_power_is_a_lock_gated_register(self):
        """`power` drives 416 — the bridge must bracket it, not write it plain."""
        self.assertTrue(lm.rgbw_reg_is_lock_gated(cc.register_for("power")))

    def test_effect_range_matches_the_catalog(self):
        self.assertEqual(cc.RANGE_LIMITS["effect"], (0, lm.RGBW_FX_MODE_COUNT - 1))

    def test_four_di_controls_match_the_declared_caps(self):
        di = [n for n in cc.control_names() if n.startswith("di_")]
        self.assertEqual(len(di), lm.RGBW_DI_COUNT)


if __name__ == "__main__":
    unittest.main()

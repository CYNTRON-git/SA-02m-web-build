# -*- coding: utf-8 -*-
"""Config-window backend for the MR-02m LED strip: what a poll reads, and what a
command writes.

WHAT THESE PIN, AND WHY EACH ONE EXISTS

* THE PER-TAB READ SCOPE. The «Сцена» tab costs four extra reads, one of them 70
  registers wide. On a line shared with MPLC4 and the MQTT bridge that is not
  something a poll of another tab may pay. The pin is at the wire: every request
  is decoded from its PDU and a poll outside that tab must not cover 453 / 494 /
  516 / 640.
* THE MODE-DATA READ IS FIFTEEN REGISTERS AT 453. 453..467 is contiguously
  mapped; an unmapped address ANYWHERE in a block fails the WHOLE read, so a
  widened block returns an exception and the panel shows nothing — not partial
  data. The desktop learned this the hard way.
* THE SPY BLOCK BELONGS TO ONE EFFECT. 640..709 is read only while the line
  indicator (81) is selected. Reading it always would put 70 registers into every
  scene poll for a card no other effect renders.
* THE LOCK BRACKET, INCLUDING 416. Registers 400..419 are writable only between
  410 = 0x10C8 and 410 = 0. PlayCtrl 416 is INSIDE that block, so a «Стоп»
  written plainly is REJECTED by firmware while the wire looks fine — the strip
  keeps playing and the UI reports it stopped. The pin walks the actual frames.
* THE WRITE ORDER IS A DEPENDENCY, NOT AN ADDRESS SORT. Firmware validates
  several settings registers against what ANOTHER one currently holds, so 413
  before 403 before 401 before 400 before 414. A plain sorted() satisfies some of
  those by accident and violates 403 → 400.
* TEXT NEVER LANDS BELOW 516. The pre-1.0.2.2 base 495 spanned the family
  safe-state AO block 503..506 and writing text drove the module's PWM safe
  values to 100 %.
* CMD 3 NEVER REACHES THE WIRE. Firmware ACKs it and stores nothing, so it looks
  successful and does nothing at all.

The register map is never restated here: every address arrives from the shared
package through the flasher's one import seam, module_profiles.led_mb2ws().
service.py is NOT imported (it needs `grp`/`cgi` and cannot load off Linux) — the
new route's handler is extracted with `ast` and executed against stubs, the idiom
of test_carel_config.py / test_health_lease.py.

NOT VERIFIED ON HARDWARE: no type-120 device answered COM3 at either baud during
this branch. Every assertion below is against a frame-level fake built from the
shared map, and no bench result is implied.
"""
from __future__ import annotations

import ast
import struct
import textwrap
import types
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from sa02m_flasher import device_config, led_poll, modbus_rtu, module_profiles

lm = module_profiles.led_mb2ws()

_PKG = Path(__file__).resolve().parent.parent / "sa02m_flasher"
_SERVICE_SRC = (_PKG / "service.py").read_text(encoding="utf-8")

LED_SIGNATURE = "RGBW_WS2812"

# The blocks that must never ride a poll of another tab.
EXPENSIVE_READ_ANCHORS = (
    lm.MB2WS_MODE_DATA_BASE,   # 453
    lm.MB2WS_TEXT_LINES,       # 494
    lm.MB2WS_TEXT_BASE,        # 516
    lm.MB2WS_SPY_WORK_PORT,    # 640
)


def _signature_regs(text: str) -> Dict[int, int]:
    """290..301 as the decoder reads them: one ASCII byte in each LOW byte."""
    raw = text.encode("ascii")[:12].ljust(12, b"\x00")
    return {device_config.REG_SIGNATURE + i: raw[i] for i in range(12)}


def _strip_block() -> Dict[int, int]:
    """400..419 as a strip on the bench would hold them: single WS2812 line, 60
    RGB pixels, the marquee effect selected and playing, locked."""
    base = {reg: 0 for reg in range(lm.MB2WS_REG_BASE, lm.MB2WS_CH2_MODE + 1)}
    base[lm.MB2WS_LED_COUNT0] = 60
    base[lm.MB2WS_LED_TYPE] = lm.MB2WS_LED_TYPE_WS2812_0
    base[lm.MB2WS_RENDER_SOURCE] = lm.MB2WS_RENDER_FX
    base[lm.MB2WS_PIXEL_FORMAT] = 0
    base[lm.MB2WS_AUTO_REFRESH] = 1
    base[lm.MB2WS_FX_ID] = lm.RGBW_FX_MODE_MATRIX_TEXT
    base[lm.MB2WS_FX_SPEED] = 128
    base[lm.MB2WS_FX_PARAM] = 200
    base[lm.MB2WS_LOCK] = 0
    base[lm.MB2WS_GAMMA] = 128
    base[lm.MB2WS_LINE_MODE] = lm.MB2WS_LINE_SINGLE_WS
    base[lm.MB2WS_PLAY_CTRL] = lm.MB2WS_PLAY_PLAY
    base[lm.MB2WS_MATRIX_WIDTH] = lm.rgbw_matrix_pack(16, 16)
    base[lm.MB2WS_MATRIX_LAYOUT] = (
        lm.MB2WS_MATRIX_LAYOUT_PROGRESSIVE
        | (2 << lm.MB2WS_MATRIX_LAYOUT_TILECOUNT_SHIFT)  # TileCount = 2
    )
    return base


def _mode_data_block() -> Dict[int, int]:
    """453..467: clock 07:30, weather set, glyph colours, WxTempColor."""
    base = {reg: 0 for reg in range(lm.MB2WS_MODE_DATA_BASE, lm.MB2WS_WX_TEMP_COLOR + 1)}
    base[lm.MB2WS_TOD_HOURS] = 7
    base[lm.MB2WS_TOD_MINUTES] = 30
    base[lm.MB2WS_FX_AUX] = 0x0102
    base[lm.MB2WS_FX_DENSITY] = lm.MB2WS_FX_DENSITY_DEFAULT
    base[lm.MB2WS_WX_DATE] = (4 << 8) | 9
    base[lm.MB2WS_WX_TEMP] = lm.rgbw_i16_to_u16(-115)  # -11.5 °C
    base[lm.MB2WS_WX_HUM] = 615                        # 61.5 %RH
    base[lm.MB2WS_WX_PRESS] = 7480                     # 748.0 mmHg
    base[lm.MB2WS_WX_YEAR] = 2026
    base[lm.MB2WS_TEXT_COLOR1] = lm.rgbw_hex_to_rgb565("#FF0000")
    base[lm.MB2WS_WX_TEMP_COLOR] = lm.rgbw_hex_to_rgb565("#00FF00")
    return base


def _text_block(text: str) -> Dict[int, int]:
    words = lm.rgbw_pack_text_cp1251(text)
    return {lm.MB2WS_TEXT_BASE + i: words[i] for i in range(lm.MB2WS_TEXT_REG_COUNT)}


def _spy_block() -> Dict[int, int]:
    """640..709: Spy on port A, one live slot with a unit and limits."""
    base = {reg: 0 for reg in range(lm.MB2WS_SPY_WORK_PORT, lm.MB2WS_SPY_LAST + 1)}
    base[lm.MB2WS_SPY_WORK_PORT] = 1
    base[lm.MB2WS_SPY_WORK_MODE] = lm.MB2WS_SPY_MODE_SPY | lm.MB2WS_SPY_TAP_BIT
    base[lm.MB2WS_SPY_BAUD] = lm.rgbw_spy_baud_choices()[0][0]
    base[lm.MB2WS_SPY_LINE] = 0 | (1 << 8)
    base[lm.MB2WS_SPY_POLL_MS] = 1000
    base[lm.MB2WS_SPY_TIMEOUT_MS] = 500
    base[lm.MB2WS_SPY_STALE_MS] = 10000
    base[lm.rgbw_spy_slot_reg(0, 0)] = 7           # UID
    base[lm.rgbw_spy_slot_reg(0, 1)] = lm.MB2WS_SPY_FC_INPUT
    base[lm.rgbw_spy_slot_reg(0, 2)] = 12
    base[lm.rgbw_spy_slot_reg(0, 3)] = lm.MB2WS_SPY_TYPE_INT16
    base[lm.rgbw_spy_slot_reg(0, 4)] = 1
    unit01, unit23 = lm.rgbw_spy_pack_unit("°C")
    base[lm.rgbw_spy_slot_reg(0, 5)] = unit01
    base[lm.rgbw_spy_slot_reg(0, 6)] = unit23
    base[lm.rgbw_spy_lim_reg(0, 0)] = lm.rgbw_i16_to_u16(-200)
    base[lm.rgbw_spy_lim_reg(0, 1)] = lm.rgbw_i16_to_u16(600)
    base[lm.MB2WS_SPY_LIVE_BASE + 0] = 0
    base[lm.MB2WS_SPY_LIVE_BASE + 1] = 231
    return base


def _led_holding() -> Dict[int, int]:
    regs: Dict[int, int] = {}
    # Line + identity block — the strip is one of ours and answers all of it.
    regs.update({
        device_config.REG_NET_BAUD: 115200 // 100,
        device_config.REG_NET_PARITY: 0,
        device_config.REG_NET_STOP: 1,
        device_config.REG_FAST_MODBUS: 0,
        device_config.REG_NET_ADDR: 13,
    })
    regs.update({device_config.REG_SERIAL_LO: 0x0001, device_config.REG_SERIAL_LO + 1: 0x2345})
    regs.update(_signature_regs(LED_SIGNATURE))
    regs.update({device_config.REG_APP_VERSION + i: 0 for i in range(device_config.REG_APP_VERSION_COUNT)})
    regs.update({device_config.REG_BOOTLOADER_VER + i: 0 for i in range(device_config.REG_BOOTLOADER_VER_COUNT)})
    # Product low map: PWM levels, strip mode, DI configuration.
    regs.update({lm.RGBW_PWM_HOLDING_BASE + i: 250 * (i + 1) for i in range(lm.RGBW_PWM_CHANNELS)})
    regs[lm.RGBW_PWM_STRIP_MODE_HOLDING] = lm.RGBW_PWM_MODE_RGBW
    regs.update({reg: 0 for reg in range(lm.RGBW_DI_MODE_BASE, lm.RGBW_DI_HOLD_LAST + 1)})
    regs[lm.RGBW_DI_MODE_BASE] = 1
    regs[lm.RGBW_DI_DEBOUNCE_BASE] = 50
    # MB2WS block.
    regs.update(_strip_block())
    regs.update(_mode_data_block())
    regs[lm.MB2WS_TEXT_LINES] = lm.MB2WS_TEXT_LINES_DOUBLE
    regs.update(_text_block("ЦИНТРОН"))
    regs.update(_spy_block())
    return regs


LED_INPUT: Dict[int, int] = {0: module_profiles.RGBW_WS2812}
LED_INPUT.update({lm.RGBW_IREG_CURRENT_BASE + i: 120 * (i + 1) for i in range(lm.RGBW_PWM_CHANNELS)})
LED_INPUT[lm.RGBW_IREG_NTC] = 315
LED_INPUT[lm.RGBW_IREG_VLED] = 1198

LED_DEVICE = {
    "address": 13,
    "baudrate": 115200,
    "parity": "N",
    "stopbits": 1,
    "signature": LED_SIGNATURE,
    "app_version": "1.0.2.3",
}

MR_DEVICE = {
    "address": 5,
    "baudrate": 9600,
    "parity": "N",
    "stopbits": 1,
    "signature": "6AI6AO",
}


class FakeLedStrip:
    """A Modbus RTU slave built from the tables above, speaking real frames.

    Every request is decoded from its PDU and recorded, so a test asserts on what
    went out on the wire rather than on which helper was called. An address
    outside the tables answers NOTHING — the device's own behaviour, and the
    reason a widened block read costs a timeout instead of partial data.
    """

    def __init__(self, slave: int, *, holding: Dict[int, int], input_regs: Dict[int, int]) -> None:
        self.slave = slave
        self.holding = dict(holding)
        self.input_regs = dict(input_regs)
        self.reads: List[Tuple[int, int, int]] = []   # (func, start, count)
        self.writes: List[Tuple[int, int, Any]] = []  # (func, address, value)

    # -- frame helpers ------------------------------------------------------
    def _crc(self, body: bytes) -> bytes:
        return struct.pack("<H", modbus_rtu.crc16_modbus(body))

    def _read_reply(self, func: int, data: bytes) -> bytes:
        body = bytes([self.slave, func, len(data)]) + data
        return body + self._crc(body)

    def _echo(self, req: bytes) -> bytes:
        body = req[:6]
        return body + self._crc(body)

    def _regs_reply(self, func: int, table: Dict[int, int], start: int, count: int) -> Optional[bytes]:
        if any(start + i not in table for i in range(count)):
            return None  # unmapped address anywhere ⇒ the WHOLE block fails
        data = b"".join(struct.pack(">H", table[start + i] & 0xFFFF) for i in range(count))
        return self._read_reply(func, data)

    # -- the SendRtuFn contract --------------------------------------------
    def __call__(self, req: bytes, timeout_ms: int) -> Optional[bytes]:
        func = req[1]
        if func in (0x03, 0x04):
            start, count = struct.unpack(">HH", req[2:6])
            self.reads.append((func, start, count))
            table = self.holding if func == 0x03 else self.input_regs
            return self._regs_reply(func, table, start, count)
        if func in (0x01, 0x02):
            start, count = struct.unpack(">HH", req[2:6])
            self.reads.append((func, start, count))
            return None  # the strip exposes no coils to this window
        if func == 0x05:
            addr, raw = struct.unpack(">HH", req[2:6])
            self.writes.append((func, addr, bool(raw)))
            return self._echo(req)
        if func == 0x06:
            addr, value = struct.unpack(">HH", req[2:6])
            self.writes.append((func, addr, value))
            self.holding[addr] = value
            return self._echo(req)
        if func == 0x10:
            addr, count = struct.unpack(">HH", req[2:6])
            words = [struct.unpack(">H", req[7 + 2 * i : 9 + 2 * i])[0] for i in range(count)]
            self.writes.append((func, addr, tuple(words)))
            for i, w in enumerate(words):
                self.holding[addr + i] = w
            return self._echo(req)
        return None

    # -- assertion helpers --------------------------------------------------
    def read_covers(self, address: int) -> bool:
        return any(
            func in (0x03, 0x04) and start <= address < start + count
            for func, start, count in self.reads
        )

    def holding_reads(self) -> List[Tuple[int, int]]:
        return [(start, count) for func, start, count in self.reads if func == 0x03]

    def written_regs(self) -> List[Tuple[int, int]]:
        """FC06 writes in wire order: (address, value)."""
        return [(addr, value) for func, addr, value in self.writes if func == 0x06]

    def written_addresses(self) -> List[int]:
        return [addr for addr, _value in self.written_regs()]

    def block_writes(self) -> List[Tuple[int, Tuple[int, ...]]]:
        """FC16 writes in wire order: (address, words)."""
        return [(addr, value) for func, addr, value in self.writes if func == 0x10]


def led_strip(slave: int = 13) -> FakeLedStrip:
    return FakeLedStrip(slave, holding=_led_holding(), input_regs=LED_INPUT)


def _snapshot(plc: FakeLedStrip, device: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    closed: List[bool] = []
    with patch.object(
        device_config, "_open_transport", return_value=(plc, lambda: closed.append(True))
    ):
        payload = device_config.snapshot_for_device("/dev/ttyS4", device, **kwargs)
    assert closed, "порт не закрыт"
    return payload


def _led_write(
    plc: FakeLedStrip,
    device: Dict[str, Any],
    action: str,
    params: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> Dict[str, Any]:
    with patch.object(device_config, "_open_transport", return_value=(plc, lambda: None)):
        return device_config.led_write("/dev/ttyS4", device, action, params, **kwargs)


class TestSharedMapAvailable(unittest.TestCase):
    def test_seam_resolves(self) -> None:
        """Non-vacuity floor: every case below reads addresses from the shared
        package. Without it they would all pass by never asserting anything."""
        self.assertIsNotNone(lm, "sa02m_led не разрешается через шов пакета")

    def test_the_fake_answers_every_block_the_window_reads(self) -> None:
        """Second non-vacuity floor: a table with a hole answers NOTHING for the
        whole block, so a read-scope pin would pass because the read failed."""
        plc = led_strip()
        payload = _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)
        led = payload["led"]
        self.assertTrue(led["answered"])
        for key in ("strip", "scene", "mode_data", "text"):
            self.assertIn(key, led, f"блок «{key}» не прочитан — таблица стенда неполна")


class TestKindResolution(unittest.TestCase):
    def test_kind_from_identity_answers_led_for_a_strip_signature(self) -> None:
        self.assertEqual(device_config._kind_from_identity(LED_SIGNATURE, None), "led")
        self.assertEqual(device_config._kind_from_identity("LED", None), "led")
        # Input reg 0 alone is enough when the signature says nothing.
        self.assertEqual(
            device_config._kind_from_identity("", module_profiles.RGBW_WS2812), "led"
        )
        # …and a foreign signature that merely starts with "LED" is not a strip.
        self.assertEqual(device_config._kind_from_identity("6AI6AO", None), "mr")

    def test_snapshot_reports_kind_led_with_identity_and_network(self) -> None:
        payload = _snapshot(led_strip(), LED_DEVICE)
        self.assertEqual(payload["kind"], "led")
        self.assertEqual(payload["info"]["signature"], LED_SIGNATURE)
        self.assertEqual(payload["info"]["type_code"], module_profiles.RGBW_WS2812)
        self.assertEqual(payload["network"]["baudrate"], 115200)
        self.assertEqual(payload["network"]["address"], 13)


class TestPerTabReadScope(unittest.TestCase):
    """The whole point of the tab argument: an expensive block is read on ITS tab."""

    def _assert_no_expensive_reads(self, plc: FakeLedStrip, where: str) -> None:
        for anchor in EXPENSIVE_READ_ANCHORS:
            self.assertFalse(
                plc.read_covers(anchor),
                f"{where}: прочитан регистр {anchor} — дорогой блок попал в такт опроса",
            )

    def test_a_poll_with_no_tab_reads_only_the_base_block(self) -> None:
        plc = led_strip()
        payload = _snapshot(plc, LED_DEVICE)
        self.assertTrue(payload["led"]["answered"], "базовый блок не прочитан")
        self.assertTrue(plc.read_covers(lm.MB2WS_FX_ID), "базовый блок 400 не прочитан")
        self._assert_no_expensive_reads(plc, "опрос без вкладки")
        self.assertNotIn("mode_data", payload["led"])
        self.assertNotIn("text", payload["led"])
        self.assertNotIn("spy", payload["led"])

    def test_the_pwm_tab_does_not_pay_for_the_scene_blocks(self) -> None:
        plc = led_strip()
        payload = _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_PWM)
        self._assert_no_expensive_reads(plc, "вкладка «RGBW каналы»")
        pwm = payload["led"]["pwm"]
        self.assertEqual(pwm["levels"][0], 250)
        self.assertEqual(pwm["currents_ma"][3], 480)
        self.assertEqual(pwm["vled_raw"], 1198)

    def test_the_di_tab_reads_one_block_and_no_scene_block(self) -> None:
        plc = led_strip()
        payload = _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_DI)
        self._assert_no_expensive_reads(plc, "вкладка «Входы DI»")
        count = (lm.RGBW_DI_HOLD_LAST - lm.RGBW_DI_MODE_BASE) + 1
        self.assertIn(
            (lm.RGBW_DI_MODE_BASE, count),
            plc.holding_reads(),
            "конфигурация DI прочитана не одним блоком 135..186",
        )
        channels = payload["led"]["di"]["channels"]
        self.assertEqual(len(channels), lm.RGBW_DI_COUNT)
        self.assertEqual(channels[0]["mode"], 1)
        self.assertEqual(channels[0]["debounce_ms"], 50)

    def test_the_strip_tab_adds_no_read_of_its_own(self) -> None:
        """400..419 is the base block, so «Адресная лента» costs nothing extra."""
        idle = led_strip()
        _snapshot(idle, LED_DEVICE)
        opened = led_strip()
        payload = _snapshot(opened, LED_DEVICE, active_tab=led_poll.LED_TAB_STRIP)
        self.assertEqual(opened.holding_reads(), idle.holding_reads())
        strip = payload["led"]["strip"]
        self.assertEqual(strip["led_count0"], 60)
        self.assertEqual(strip["line_ui"], lm.RGBW_LINE_UI_SINGLE)
        self.assertEqual(strip["matrix_width"], 16)
        self.assertEqual(strip["matrix_tile_count"], 2)

    def test_an_unknown_tab_falls_back_to_the_cheap_read(self) -> None:
        """Fail-safe to CHEAP: a tab id we do not know must not cost the Spy read."""
        plc = led_strip()
        payload = _snapshot(plc, LED_DEVICE, active_tab="led_something_new")
        self._assert_no_expensive_reads(plc, "неизвестная вкладка")
        # …and the snapshot says so, rather than echoing a tab it did not honour.
        self.assertIsNone(payload["led"]["active_tab"])
        for tab in led_poll.LED_TABS:
            echoed = _snapshot(led_strip(), LED_DEVICE, active_tab=tab)
            self.assertEqual(echoed["led"]["active_tab"], tab)


class TestSceneTabReads(unittest.TestCase):
    def test_mode_data_is_exactly_fifteen_registers_at_453(self) -> None:
        plc = led_strip()
        _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)
        base, count = lm.rgbw_mode_data_read_span()
        self.assertEqual((base, count), (453, 15))
        self.assertIn(
            (base, count),
            plc.holding_reads(),
            "блок данных режима прочитан не одним FC03 из 15 регистров с 453",
        )
        # And nothing wider starting inside the block: 468+ is unmapped, and one
        # unmapped address fails the WHOLE read.
        for start, cnt in plc.holding_reads():
            if start == base:
                self.assertLessEqual(
                    start + cnt - 1,
                    lm.MB2WS_WX_TEMP_COLOR,
                    "чтение блока данных режима расширено за 467",
                )

    def test_mode_data_decodes_the_sentinels_and_the_values(self) -> None:
        payload = _snapshot(led_strip(), LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)
        md = payload["led"]["mode_data"]
        self.assertEqual((md["tod_hours"], md["tod_minutes"]), (7, 30))
        self.assertAlmostEqual(md["wx_temp_c"], -11.5, places=3)
        self.assertAlmostEqual(md["wx_hum_pct"], 61.5, places=3)
        self.assertEqual(md["wx_year"], 2026)
        self.assertEqual(md["text_colors"][0], lm.rgbw_hex_to_rgb565("#FF0000"))

    def test_text_window_and_lines_are_read_and_decoded(self) -> None:
        payload = _snapshot(led_strip(), LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)
        led = payload["led"]
        self.assertEqual(led["text"], "ЦИНТРОН")
        self.assertEqual(led["text_lines"], lm.MB2WS_TEXT_LINES_DOUBLE)

    def test_the_spy_block_is_not_read_for_an_ordinary_effect(self) -> None:
        plc = led_strip()  # holds effect 62 (marquee)
        payload = _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)
        self.assertEqual(payload["led"]["scene"]["fx_id"], lm.RGBW_FX_MODE_MATRIX_TEXT)
        self.assertFalse(
            plc.read_covers(lm.MB2WS_SPY_WORK_PORT),
            "блок Spy (70 регистров) прочитан для эффекта, который его не рисует",
        )
        self.assertNotIn("spy", payload["led"])

    def test_the_spy_block_is_read_for_effect_81(self) -> None:
        plc = led_strip()
        plc.holding[lm.MB2WS_FX_ID] = lm.RGBW_FX_MODE_MB_SPY
        payload = _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)
        self.assertIn(
            (lm.MB2WS_SPY_WORK_PORT, lm.MB2WS_SPY_REG_COUNT),
            plc.holding_reads(),
            "блок Spy не прочитан для эффекта «Индикатор линии»",
        )
        spy = payload["led"]["spy"]
        self.assertEqual(spy["port"], 1)
        self.assertEqual(spy["mode"], lm.MB2WS_SPY_MODE_SPY)
        self.assertTrue(spy["tap"])
        self.assertEqual(spy["slots"][0]["uid"], 7)
        self.assertEqual(spy["slots"][0]["unit"], "°C")
        self.assertEqual(spy["slots"][0]["lo"], -200)
        self.assertEqual(spy["slots"][0]["live"], 231)
        # An empty slot polls nothing — its live word is not reported as a reading.
        self.assertIsNone(spy["slots"][1]["live"])

    def test_a_gyver_alias_resolves_before_the_spy_decision(self) -> None:
        """FxId 128+81 is the same effect; a strip left on the alias must still
        get its Spy card, and must not read as «Статика»."""
        plc = led_strip()
        plc.holding[lm.MB2WS_FX_ID] = lm.RGBW_FX_MODE_GYVER_BASE + lm.RGBW_FX_MODE_MB_SPY
        payload = _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)
        self.assertEqual(payload["led"]["scene"]["fx_id"], lm.RGBW_FX_MODE_MB_SPY)
        self.assertIn("spy", payload["led"])


class TestLockBracket(unittest.TestCase):
    """Every write into 400..419 goes unlock → ordered writes → lock."""

    def _bracket(self, writes: List[Tuple[int, int]]) -> None:
        self.assertEqual(
            writes[0], (lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY),
            "пакет настроек не начался со снятия блокировки рег. 410",
        )
        self.assertEqual(
            writes[-1], (lm.MB2WS_LOCK, 0),
            "пакет настроек не закончился постановкой блокировки рег. 410",
        )

    def test_stop_unlocks_before_playctrl_416(self) -> None:
        """416 lives INSIDE the settings block. A plain write to it on a locked
        device is refused while the wire looks fine — the strip keeps playing."""
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "stop")
        writes = plc.written_regs()
        self.assertEqual(
            writes[:3],
            [
                (lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY),
                (lm.MB2WS_PLAY_CTRL, lm.MB2WS_PLAY_STOP),
                (lm.MB2WS_LOCK, 0),
            ],
            "«Стоп» записал PlayCtrl 416 без снятия блокировки",
        )

    def test_stop_blanks_the_strip_in_the_mandated_order(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "stop")
        writes = plc.written_regs()
        order = [
            (lm.MB2WS_PLAY_CTRL, lm.MB2WS_PLAY_STOP),
            (lm.MB2WS_RENDER_SOURCE, lm.MB2WS_RENDER_POOL),
            (lm.MB2WS_CMD, lm.MB2WS_CMD_CLEAR_POOL),
            (lm.MB2WS_CMD, lm.MB2WS_CMD_REFRESH),
        ]
        positions = [writes.index(step) for step in order]
        self.assertEqual(positions, sorted(positions), "порядок остановки нарушен")
        # RenderSource is inside the block too; the pool clear is not.
        self.assertEqual(
            writes[3], (lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY),
            "RenderSource 402 записан без снятия блокировки",
        )
        cmd_writes = [w for w in writes if w[0] == lm.MB2WS_CMD]
        self.assertEqual(len(cmd_writes), 2)
        self.assertNotIn(
            lm.MB2WS_LOCK,
            [addr for addr, _v in writes[writes.index((lm.MB2WS_CMD, lm.MB2WS_CMD_CLEAR_POOL)):]],
            "команды CMD 430 обёрнуты лишним снятием блокировки",
        )

    def test_a_plain_register_batch_is_not_bracketed(self) -> None:
        """An unlock that is not needed still costs two frames on a shared line —
        and leaves 400..419 open if the caller aborts."""
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "clock", {"hours": 9, "minutes": 5})
        self.assertEqual(
            plc.written_regs(),
            [(lm.MB2WS_TOD_HOURS, 9), (lm.MB2WS_TOD_MINUTES, 5)],
        )

    def test_the_device_is_re_locked_when_a_batch_write_fails(self) -> None:
        class Deaf(FakeLedStrip):
            def __call__(self, req: bytes, timeout_ms: int):
                if req[1] == 0x06:
                    addr = struct.unpack(">H", req[2:4])[0]
                    if addr == lm.MB2WS_LED_COUNT0:
                        self.writes.append((0x06, addr, None))
                        return None  # firmware refuses the count
                return super().__call__(req, timeout_ms)

        plc = Deaf(13, holding=_led_holding(), input_regs=LED_INPUT)
        with self.assertRaises(RuntimeError):
            _led_write(plc, LED_DEVICE, "strip", {"line": lm.RGBW_LINE_UI_SINGLE, "led_count0": 120})
        self.assertEqual(
            plc.written_regs()[-1], (lm.MB2WS_LOCK, 0),
            "после неудачной записи блок настроек остался разблокированным",
        )


class TestStripAction(unittest.TestCase):
    def test_settings_are_written_in_dependency_order_not_address_order(self) -> None:
        plc = led_strip()
        _led_write(
            plc, LED_DEVICE,
            "strip",
            {
                "line": lm.RGBW_LINE_UI_DUAL,
                "led_type": lm.MB2WS_LED_TYPE_WS2812_0,
                "led_count0": 300,
                "led_count1": 100,
                "pixel_format": 1,
            },
        )
        addrs = plc.written_addresses()
        self.assertEqual(addrs[0], lm.MB2WS_LOCK)
        self.assertEqual(addrs[-1], lm.MB2WS_LOCK)
        body = addrs[1:-1]
        for earlier, later in (
            (lm.MB2WS_LINE_MODE, lm.MB2WS_PIXEL_FORMAT),   # 413 before 403
            (lm.MB2WS_PIXEL_FORMAT, lm.MB2WS_LED_TYPE),    # 403 before 401
            (lm.MB2WS_LED_TYPE, lm.MB2WS_LED_COUNT0),      # 401 before 400
            (lm.MB2WS_LED_COUNT0, lm.MB2WS_LED_COUNT1),    # 400 before 414
        ):
            self.assertLess(
                body.index(earlier), body.index(later),
                f"рег. {earlier} записан после {later} — прошивка проверяет их друг против друга",
            )
        # 403 before 400 is exactly what a sorted() batch gets wrong.
        self.assertLess(
            body.index(lm.MB2WS_PIXEL_FORMAT), body.index(lm.MB2WS_LED_COUNT0),
            "пакет отсортирован по адресу — 400 ушёл раньше 403",
        )

    def test_reg_418_is_read_modify_write_and_preserves_tiling(self) -> None:
        plc = led_strip()
        _led_write(
            plc, LED_DEVICE,
            "strip",
            {"line": lm.RGBW_LINE_UI_SINGLE, "led_count0": 60, "mirror_x": True},
        )
        layout = dict(plc.written_regs())[lm.MB2WS_MATRIX_LAYOUT]
        self.assertTrue(layout & lm.MB2WS_MATRIX_LAYOUT_MIRROR_X)
        self.assertFalse(layout & lm.MB2WS_MATRIX_LAYOUT_PROGRESSIVE)
        self.assertEqual(
            lm.rgbw_matrix_layout_tilecount(layout), 2,
            "TileCount устройства затёрт — read-modify-write не сработал",
        )

    def test_reg_418_is_skipped_when_the_prior_value_is_unknown(self) -> None:
        """Composing the preserved fields from nothing is the data loss the
        read-modify-write exists to prevent — the register is skipped instead."""
        plc = led_strip()
        del plc.holding[lm.MB2WS_MATRIX_LAYOUT]  # the read comes back silent
        _led_write(
            plc, LED_DEVICE,
            "strip",
            {"line": lm.RGBW_LINE_UI_SINGLE, "led_count0": 60, "mirror_x": True},
        )
        self.assertNotIn(lm.MB2WS_MATRIX_LAYOUT, plc.written_addresses())

    def test_a_geometry_that_does_not_fit_the_pool_is_refused_not_clamped(self) -> None:
        plc = led_strip()
        with self.assertRaises(ValueError):
            _led_write(
                plc, LED_DEVICE,
                "strip",
                {
                    "line": lm.RGBW_LINE_UI_SINGLE,
                    "pixel_format": 1,
                    "matrix_width": 64,
                    "matrix_height": 32,
                    "tile_count": 4,
                },
            )
        self.assertEqual(plc.written_regs(), [], "отказ произошёл уже после записей")

    def test_an_unlisted_line_mode_is_refused_before_the_port_is_opened(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.led_write("/dev/ttyS4", LED_DEVICE, "strip", {"line": "rgb_over_wifi"})
        opener.assert_not_called()


class TestSceneAndPlayback(unittest.TestCase):
    def test_scene_writes_the_selection_as_one_bracketed_batch(self) -> None:
        plc = led_strip()
        _led_write(
            plc, LED_DEVICE, "scene",
            {"source": lm.RGBW_SCENE_UI_FX, "fx_id": 12, "fx_speed": 200, "fx_param": 40, "loop": True},
        )
        writes = dict(plc.written_regs())
        self.assertEqual(writes[lm.MB2WS_RENDER_SOURCE], lm.MB2WS_RENDER_FX)
        self.assertEqual(writes[lm.MB2WS_FX_ID], 12)
        self.assertEqual(writes[lm.MB2WS_FX_SPEED], 200)
        self.assertEqual(writes[lm.MB2WS_FX_PARAM], 40)
        self.assertEqual(writes[lm.MB2WS_OPTIONS], lm.MB2WS_OPT_LOOP)
        addrs = plc.written_addresses()
        self.assertEqual((addrs[0], addrs[-1]), (lm.MB2WS_LOCK, lm.MB2WS_LOCK))

    def test_scale_460_is_never_written(self) -> None:
        """Firmware stores Scale and no render path reads it — the window does not
        offer it, and the plan must not smuggle it in."""
        self.assertFalse(lm.rgbw_scale_is_honoured())
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "scene", {"source": lm.RGBW_SCENE_UI_FX, "fx_id": 1, "scale": 500})
        self.assertNotIn(lm.MB2WS_SCALE, plc.written_addresses())

    def test_play_adds_playctrl_only_for_flash_playback(self) -> None:
        """A bare PlayCtrl=1 was a no-op for the built-in effects: firmware starts
        them when the scene registers land. Flash playback is the exception."""
        fx = led_strip()
        _led_write(fx, LED_DEVICE, "play", {"source": lm.RGBW_SCENE_UI_FX, "fx_id": 3})
        self.assertNotIn(lm.MB2WS_PLAY_CTRL, fx.written_addresses())

        flash = led_strip()
        _led_write(flash, LED_DEVICE, "play", {"source": lm.RGBW_SCENE_UI_FLASH, "flash_slot": 4})
        writes = dict(flash.written_regs())
        self.assertEqual(writes[lm.MB2WS_RENDER_SOURCE], lm.MB2WS_RENDER_FLASH)
        self.assertEqual(writes[lm.MB2WS_FLASH_SLOT], 4)
        self.assertEqual(writes[lm.MB2WS_PLAY_CTRL], lm.MB2WS_PLAY_PLAY)
        addrs = flash.written_addresses()
        # 416 rides the same batch, after RenderSource and FlashSlot.
        self.assertLess(addrs.index(lm.MB2WS_FLASH_SLOT), addrs.index(lm.MB2WS_PLAY_CTRL))

    def test_refresh_is_a_single_plain_cmd_write(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "refresh")
        self.assertEqual(plc.written_regs(), [(lm.MB2WS_CMD, lm.MB2WS_CMD_REFRESH)])

    def test_load_flash_selects_the_slot_then_issues_the_command(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "load_flash", {"slot": 7})
        writes = plc.written_regs()
        self.assertIn((lm.MB2WS_FLASH_SLOT, 7), writes)
        self.assertEqual(writes[-1], (lm.MB2WS_CMD, lm.MB2WS_CMD_LOAD_FLASH))
        # The slot is lock-gated, the command is not.
        self.assertEqual(writes[0], (lm.MB2WS_LOCK, lm.MB2WS_UNLOCK_KEY))


class TestCommandThreeIsUnreachable(unittest.TestCase):
    """Firmware ACKs CMD 3 and stores nothing, so it looks successful on the wire
    and does nothing at all. No action may reach it, and the executor refuses it
    even if one ever tried."""

    def test_no_action_writes_cmd_3(self) -> None:
        self.assertFalse(lm.rgbw_cmd_is_allowed(lm.MB2WS_CMD_SAVE_FLASH_DEPRECATED))
        walked = 0
        for action, params in _every_action_sample():
            plc = led_strip()
            _led_write(plc, LED_DEVICE, action, params)
            walked += 1
            for addr, value in plc.written_regs():
                if addr == lm.MB2WS_CMD:
                    self.assertNotEqual(
                        value, lm.MB2WS_CMD_SAVE_FLASH_DEPRECATED,
                        f"действие «{action}» отправило команду 3",
                    )
        self.assertEqual(walked, len(device_config.LED_ACTIONS), "обойдены не все действия")

    def test_the_executor_refuses_cmd_3_even_from_a_hand_built_plan(self) -> None:
        plc = led_strip()
        plan = [device_config._led_op_regs({lm.MB2WS_CMD: lm.MB2WS_CMD_SAVE_FLASH_DEPRECATED})]
        with self.assertRaises(ValueError):
            device_config._led_run_plan(lm, plc, 13, plan)
        self.assertEqual(plc.written_regs(), [], "команда 3 всё-таки ушла на шину")


class TestTextWindow(unittest.TestCase):
    def test_text_lands_at_516_as_one_block_and_never_lower(self) -> None:
        """The pre-1.0.2.2 base 495 spanned the safe-state AO block 503..506 and
        writing text drove the module's PWM safe values to 100 %."""
        self.assertTrue(lm.rgbw_text_span_clears_safe_ao())
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "text", {"text": "тест"})
        blocks = plc.block_writes()
        self.assertEqual(len(blocks), 1)
        addr, words = blocks[0]
        self.assertEqual(addr, lm.MB2WS_TEXT_BASE)
        self.assertEqual(len(words), lm.MB2WS_TEXT_REG_COUNT)
        self.assertEqual(lm.rgbw_unpack_text_cp1251(list(words)), "ТЕСТ")
        # Nothing anywhere near the safe-state AO block, on either function code.
        for target in plc.written_addresses() + [a for a, _w in blocks]:
            self.assertFalse(
                lm.RGBW_PWM_SAFE_BASE <= target <= lm.RGBW_PWM_SAFE_LAST,
                f"запись текста задела блок безопасных значений (рег. {target})",
            )
        self.assertNotIn(
            lm.MB2WS_TEXT_LEGACY_BASE, [a for a, _w in blocks],
            "текст ушёл в разрушительную базу 495",
        )

    def test_the_executor_refuses_any_other_block_write(self) -> None:
        plc = led_strip()
        plan = [device_config._led_op_block(lm.MB2WS_TEXT_LEGACY_BASE, [0] * 16)]
        with self.assertRaises(ValueError):
            device_config._led_run_plan(lm, plc, 13, plan)
        self.assertEqual(plc.block_writes(), [])

    def test_text_longer_than_the_window_is_refused_not_truncated(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.led_write(
                    "/dev/ttyS4", LED_DEVICE, "text", {"text": "A" * (lm.MB2WS_TEXT_MAX_CHARS + 1)}
                )
        opener.assert_not_called()

    def test_text_lines_and_colours_are_plain_writes(self) -> None:
        plc = led_strip()
        _led_write(
            plc, LED_DEVICE, "text",
            {"lines": lm.MB2WS_TEXT_LINES_DOUBLE, "colors": {"color1": "#FF8000", "bg2": 1234}},
        )
        writes = dict(plc.written_regs())
        self.assertEqual(writes[lm.MB2WS_TEXT_LINES], lm.MB2WS_TEXT_LINES_DOUBLE)
        self.assertEqual(writes[lm.MB2WS_TEXT_COLOR1], lm.rgbw_hex_to_rgb565("#FF8000"))
        self.assertEqual(writes[lm.MB2WS_TEXT_BG2], 1234)
        self.assertNotIn(lm.MB2WS_LOCK, plc.written_addresses())


class TestClockAndWeather(unittest.TestCase):
    def test_a_half_filled_clock_is_refused(self) -> None:
        """Firmware ticks only while BOTH registers are set, and 0 is a real time
        — a half-filled request must never seed midnight."""
        plc = led_strip()
        with self.assertRaises(ValueError):
            _led_write(plc, LED_DEVICE, "clock", {"hours": 9})
        self.assertEqual(plc.written_regs(), [])

    def test_unset_writes_both_sentinels(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "clock", {"unset": True})
        self.assertEqual(
            sorted(plc.written_regs()),
            sorted([(lm.MB2WS_TOD_HOURS, lm.MB2WS_TOD_UNSET), (lm.MB2WS_TOD_MINUTES, lm.MB2WS_TOD_UNSET)]),
        )

    def test_the_pc_seed_adds_the_date_only_for_the_weather_effect(self) -> None:
        """The weather renderer composes "HH:MM DD.MM.YYYY" from both blocks, so
        seeding the time there without the date leaves half the panel unset. Which
        effect is running is asked of the DEVICE, not of the request."""
        moment = datetime(2026, 9, 4, 21, 15)
        marquee = led_strip()  # effect 62
        with patch.object(device_config, "datetime") as dt:
            dt.now.return_value = moment
            _led_write(marquee, LED_DEVICE, "clock", {"from_pc": True})
        self.assertEqual(
            sorted(marquee.written_regs()),
            sorted([(lm.MB2WS_TOD_HOURS, 21), (lm.MB2WS_TOD_MINUTES, 15)]),
        )

        weather = led_strip()
        weather.holding[lm.MB2WS_FX_ID] = lm.RGBW_FX_MODE_WEATHER
        with patch.object(device_config, "datetime") as dt:
            dt.now.return_value = moment
            _led_write(weather, LED_DEVICE, "clock", {"from_pc": True})
        writes = dict(weather.written_regs())
        self.assertEqual(writes[lm.MB2WS_WX_DATE], (4 << 8) | 9)
        self.assertEqual(writes[lm.MB2WS_WX_YEAR], 2026)

    def test_an_omitted_weather_field_writes_its_sentinel_not_zero(self) -> None:
        """0 is a real reading (0 °C, 0 %RH) and would render as one."""
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "weather", {"temp_c": "-11,5", "day": 4, "month": 9, "year": 2026})
        writes = dict(plc.written_regs())
        self.assertEqual(writes[lm.MB2WS_WX_TEMP], lm.rgbw_i16_to_u16(-115))
        self.assertEqual(writes[lm.MB2WS_WX_HUM], lm.MB2WS_WX_HUM_UNSET)
        self.assertEqual(writes[lm.MB2WS_WX_PRESS], lm.MB2WS_WX_PRESS_UNSET)
        self.assertEqual(writes[lm.MB2WS_WX_DATE], (4 << 8) | 9)
        self.assertNotIn(lm.MB2WS_SCALE, writes, "Scale 460 попал в блок погоды")

    def test_the_weather_temperature_colour_rides_its_own_register(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "weather", {"temp_color": "#00FF00"})
        writes = dict(plc.written_regs())
        self.assertEqual(writes[lm.MB2WS_WX_TEMP_COLOR], lm.rgbw_hex_to_rgb565("#00FF00"))


class TestPwmAndDiActions(unittest.TestCase):
    def test_a_pwm_channel_writes_the_level_then_the_mirror(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "pwm", {"channel": 2, "value": 640})
        self.assertEqual(
            plc.written_regs(),
            [(lm.RGBW_PWM_HOLDING_BASE + 2, 640), (lm.RGBW_PWM_MIRROR_BASE + 2, 640)],
        )

    def test_a_pwm_channel_outside_the_product_is_refused(self) -> None:
        plc = led_strip()
        with self.assertRaises(ValueError):
            _led_write(plc, LED_DEVICE, "pwm", {"value": 500})  # no channel at all
        self.assertEqual(plc.written_regs(), [])

    def test_an_unlisted_pwm_mode_is_refused_not_folded_to_rgbw(self) -> None:
        plc = led_strip()
        with self.assertRaises(ValueError):
            _led_write(plc, LED_DEVICE, "pwm", {"mode": 99})
        self.assertEqual(plc.written_regs(), [])

    def test_di_writes_land_on_the_channel_the_request_named(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "di", {"channel": 3, "mode": 1, "debounce_ms": 5})
        self.assertEqual(
            plc.written_regs(),
            [
                (lm.RGBW_DI_MODE_BASE + 3, 1),
                (lm.RGBW_DI_DEBOUNCE_BASE + 3, 10),  # clamped up to the firmware floor
            ],
        )

    def test_a_di_channel_beyond_the_product_is_clamped_into_its_own_block(self) -> None:
        """Four inputs; a request naming a fifth must not walk off 135..186 into
        the dimmer block that follows."""
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "di", {"channel": 99, "mode": 1})
        for addr, _value in plc.written_regs():
            self.assertLessEqual(addr, lm.RGBW_DI_HOLD_LAST)
            self.assertGreaterEqual(addr, lm.RGBW_DI_MODE_BASE)


class TestSpyAction(unittest.TestCase):
    def test_slot_fields_land_on_the_addresses_the_map_computes(self) -> None:
        plc = led_strip()
        _led_write(
            plc, LED_DEVICE, "spy",
            {
                "port": 2,
                "mode": lm.MB2WS_SPY_MODE_MASTER,
                "tap": True,
                "parity": 2,
                "stopbits": 2,
                "poll_ms": 250,
                "slots": [
                    {"uid": 9, "fc": lm.MB2WS_SPY_FC_INPUT, "reg": 100, "type": lm.MB2WS_SPY_TYPE_FLOAT_AB,
                     "decimals": 2, "unit": "кПа", "lo": -50, "hi": 150},
                ],
                "weather": [{"uid": 11, "fc": lm.MB2WS_SPY_FC_HOLDING, "reg": 5}],
            },
        )
        writes = dict(plc.written_regs())
        self.assertEqual(writes[lm.MB2WS_SPY_WORK_PORT], 2)
        self.assertEqual(
            writes[lm.MB2WS_SPY_WORK_MODE], lm.MB2WS_SPY_MODE_MASTER | lm.MB2WS_SPY_TAP_BIT
        )
        self.assertEqual(writes[lm.MB2WS_SPY_LINE], 2 | (2 << 8))
        self.assertEqual(writes[lm.MB2WS_SPY_POLL_MS], 250)
        self.assertEqual(writes[lm.rgbw_spy_slot_reg(0, 0)], 9)
        self.assertEqual(writes[lm.rgbw_spy_slot_reg(0, 2)], 100)
        self.assertEqual(writes[lm.rgbw_spy_lim_reg(0, 0)], lm.rgbw_i16_to_u16(-50))
        self.assertEqual(writes[lm.rgbw_wx_spy_reg(0, 1)], 5)
        # Nothing outside the Spy block, and no unlock: 640+ is not lock-gated.
        for addr in plc.written_addresses():
            self.assertTrue(lm.MB2WS_SPY_WORK_PORT <= addr <= lm.MB2WS_SPY_LAST)

    def test_an_unlisted_slot_function_code_is_refused(self) -> None:
        plc = led_strip()
        with self.assertRaises(ValueError):
            _led_write(plc, LED_DEVICE, "spy", {"slots": [{"uid": 1, "fc": 23}]})
        self.assertEqual(plc.written_regs(), [])

    def test_a_fifth_slot_cannot_walk_past_the_block(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "spy", {"slots": [{"uid": n + 1} for n in range(9)]})
        for addr in plc.written_addresses():
            self.assertLessEqual(addr, lm.MB2WS_SPY_LAST)

    def test_more_weather_fields_than_the_block_cannot_walk_past_it(self) -> None:
        """Seven weather-listen fields, then the block ends. An eighth entry in
        the request must not write past 709 into whatever the map maps next."""
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "spy", {"weather": [{"uid": n + 1} for n in range(12)]})
        for addr in plc.written_addresses():
            self.assertLessEqual(addr, lm.MB2WS_SPY_LAST)


class TestRefusals(unittest.TestCase):
    def test_unknown_action_refused_before_the_port_is_opened(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.led_write("/dev/ttyS4", LED_DEVICE, "save_flash", {})
        opener.assert_not_called()

    def test_the_refusal_is_in_russian(self) -> None:
        with self.assertRaises(ValueError) as caught:
            device_config.led_write("/dev/ttyS4", LED_DEVICE, "save_flash", {})
        self.assertTrue(
            any("а" <= ch.lower() <= "я" for ch in str(caught.exception)),
            "отказ показан оператору не по-русски",
        )

    def test_led_write_refuses_a_non_led_device(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.led_write("/dev/ttyS4", MR_DEVICE, "stop", {})
        opener.assert_not_called()

    def test_single_holding_endpoint_refuses_led(self) -> None:
        """A plain FC06 into 400..419 on a locked strip is REJECTED while the wire
        looks fine — there must be no way around the bracket."""
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.write_allowed_holding("/dev/ttyS4", LED_DEVICE, lm.MB2WS_PLAY_CTRL, 0)
        opener.assert_not_called()

    def test_single_coil_endpoint_refuses_led(self) -> None:
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.write_allowed_coil("/dev/ttyS4", LED_DEVICE, 0, True)
        opener.assert_not_called()

    def test_the_strip_is_recognised_by_reg_0_alone_at_the_refusal(self) -> None:
        """A scan row with no signature but Input reg 0 = 120 is still a strip."""
        device = dict(LED_DEVICE, signature="", type_code=module_profiles.RGBW_WS2812)
        with patch.object(device_config, "_open_transport") as opener:
            with self.assertRaises(ValueError):
                device_config.write_allowed_holding("/dev/ttyS4", device, lm.MB2WS_FX_ID, 1)
        opener.assert_not_called()


def _every_action_sample() -> List[Tuple[str, Dict[str, Any]]]:
    """One valid invocation of EVERY action in the allow-list.

    Used by the CMD-3 walk; the length assertion there fails when an action is
    added without a sample, so the walk cannot go stale.
    """
    return [
        ("pwm", {"channel": 0, "value": 100}),
        ("di", {"channel": 0, "mode": 1}),
        ("strip", {"line": lm.RGBW_LINE_UI_SINGLE, "led_count0": 60}),
        ("scene", {"source": lm.RGBW_SCENE_UI_FX, "fx_id": 5}),
        ("text", {"text": "A"}),
        ("clock", {"hours": 1, "minutes": 2}),
        ("weather", {"temp_c": 1.0}),
        ("spy", {"port": 1}),
        ("play", {"source": lm.RGBW_SCENE_UI_FLASH, "flash_slot": 1}),
        ("stop", {}),
        ("refresh", {}),
        ("load_flash", {"slot": 1}),
    ]


def _extract_method(class_name: str, method: str) -> Any:
    """exec one method of service.py in an isolated namespace (service.py itself
    imports grp/cgi and cannot load off Linux — the test_carel_config idiom)."""
    tree = ast.parse(_SERVICE_SRC)
    lines = _SERVICE_SRC.splitlines()
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if cls is None:
        raise AssertionError(f"service.py больше не определяет класс {class_name}")
    node = next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method), None)
    if node is None:
        raise AssertionError(f"service.py больше не определяет {class_name}.{method}()")
    src = textwrap.dedent("\n".join(lines[node.lineno - 1 : node.end_lineno]))
    ns: Dict[str, Any] = {}
    exec("from __future__ import annotations\n" + src, ns)  # noqa: S102
    return ns[method], ns


class TestLedWriteRoute(unittest.TestCase):
    ROUTE = "/device_config/led_write"

    def test_route_is_dispatched_and_sits_behind_auth(self) -> None:
        lines = _SERVICE_SRC.splitlines()
        route = next((i for i, ln in enumerate(lines, 1) if f'p == "{self.ROUTE}"' in ln), None)
        auth = next((i for i, ln in enumerate(lines, 1) if "if not self._check_auth()" in ln), None)
        self.assertIsNotNone(route, "маршрут led_write не разбирается в _dispatch")
        self.assertIsNotNone(auth)
        self.assertLess(auth, route, "маршрут записи в ленту оказался выше проверки сессии")
        following = "\n".join(lines[route : route + 2])
        self.assertIn("_handle_device_config_led_write(ctx)", following)

    def test_handler_runs_under_the_port_lease(self) -> None:
        """Half the commands are unlock → writes → lock; the MQTT bridge must not
        wedge in and leave the settings block open."""
        handler, ns = _extract_method("Handler", "_handle_device_config_led_write")
        seen: Dict[str, Any] = {}

        class _DeviceConfig:
            @staticmethod
            def led_write(device_path, device, action, params, *, active_tab=None):
                seen["call"] = (device_path, device, action, params, active_tab)
                return {"kind": "led", "action": action}

        class _Self:
            def _device_config_request(self, ctx, data):
                return "/dev/ttyS4", data["device"]

            def _run_device_config_modbus(self, ctx, port, device_path, fn):
                seen["leased"] = (port, device_path)
                return fn()

        ns["_read_json_body"] = lambda _self: {
            "port": "COM3",
            "device": dict(LED_DEVICE),
            "action": "stop",
            "params": {"value": 1},
            "active_tab": " led_scene ",
        }
        ns["_send_json"] = lambda _self, payload: seen.update(sent=payload)
        ns["device_config"] = _DeviceConfig
        handler(_Self(), types.SimpleNamespace())

        self.assertEqual(seen["leased"], ("COM3", "/dev/ttyS4"))
        self.assertEqual(seen["call"][2], "stop")
        self.assertEqual(seen["call"][3], {"value": 1})
        self.assertEqual(seen["call"][4], "led_scene")
        self.assertTrue(seen["sent"]["ok"])

    def test_handler_tolerates_a_missing_params_object(self) -> None:
        handler, ns = _extract_method("Handler", "_handle_device_config_led_write")
        seen: Dict[str, Any] = {}

        class _DeviceConfig:
            @staticmethod
            def led_write(device_path, device, action, params, *, active_tab=None):
                seen["params"] = params
                seen["tab"] = active_tab
                return {}

        class _Self:
            def _device_config_request(self, ctx, data):
                return "/dev/ttyS4", data["device"]

            def _run_device_config_modbus(self, ctx, port, device_path, fn):
                return fn()

        ns["_read_json_body"] = lambda _self: {
            "port": "COM3",
            "device": dict(LED_DEVICE),
            "action": "refresh",
            "params": "not-an-object",
            "active_tab": 17,
        }
        ns["_send_json"] = lambda _self, payload: None
        ns["device_config"] = _DeviceConfig
        handler(_Self(), types.SimpleNamespace())
        self.assertEqual(seen["params"], {})
        self.assertIsNone(seen["tab"])


class TestPollerUsesTheSharedMapOnly(unittest.TestCase):
    def test_no_bare_serial_port_in_the_poller(self) -> None:
        """Every Modbus access rides the leased device_config transport; a bare
        pyserial handle would open the line behind the lease's back."""
        src = (_PKG / "led_poll.py").read_text(encoding="utf-8")
        self.assertNotIn("import serial", src)
        self.assertNotIn("serial.Serial", src)
        self.assertIn("module_profiles", src)

    def test_the_poller_restates_no_register_number(self) -> None:
        """The map has one home. A literal address here is how a donor offset
        survives a map change.

        Read with `ast`, not by text: a prose mention of 453 in a comment is
        documentation, an integer literal 453 in an expression is a second home.
        """
        src = (_PKG / "led_poll.py").read_text(encoding="utf-8")
        literals = {
            node.value
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Constant) and isinstance(node.value, int)
        }
        for reg in (
            lm.MB2WS_REG_BASE,
            lm.MB2WS_MODE_DATA_BASE,
            lm.MB2WS_TEXT_LINES,
            lm.MB2WS_TEXT_BASE,
            lm.MB2WS_SPY_WORK_PORT,
            lm.RGBW_DI_MODE_BASE,
            lm.RGBW_DI_HOLD_LAST,
            lm.RGBW_PWM_HOLDING_BASE,
            lm.RGBW_PWM_STRIP_MODE_HOLDING,
            lm.RGBW_IREG_CURRENT_BASE,
            lm.RGBW_IREG_NTC,
        ):
            self.assertNotIn(
                reg, literals, f"адрес {reg} записан числом в led_poll вместо карты"
            )

    def test_snapshot_reports_no_answer_when_the_strip_is_silent(self) -> None:
        silent = FakeLedStrip(13, holding={}, input_regs={})
        snap = led_poll.read_led_snapshot(silent, 13, active_tab=led_poll.LED_TAB_SCENE)
        self.assertFalse(snap["answered"])
        self.assertNotIn("strip", snap)


def _strip_with_fx(fx_id: int) -> FakeLedStrip:
    plc = led_strip()
    plc.holding[lm.MB2WS_FX_ID] = int(fx_id)
    return plc


class TestWindowFieldsL4(unittest.TestCase):
    """L4 (1.0.6.40): what the web window renders from — nothing restated in JS.

    The choice lists, the effect groups, the per-effect reg-455 layout and the
    colour/telemetry conversions all come from the shared map through the daemon;
    the browser only maps i18n KEYS to labels. Every pin here reads the snapshot
    the window really receives, through the frame-level fake.
    """

    CHOICE_KEYS = (
        "led_types", "byte_orders", "pwm_modes", "line_ui", "scene_ui",
        "tile_modes", "tile_counts", "matrix_types", "ch2_modes", "text_lines",
        "wx_lines", "aux_colors", "spy_ports", "spy_modes", "spy_parity",
        "spy_baud", "spy_fc", "spy_types", "spy_units", "wx_spy_fields",
        "di_modes", "pixel_formats",
    )

    def test_matrix_wiring_is_decoded_for_the_window(self) -> None:
        # Fixture layout: PROGRESSIVE | TileCount 2 — one wiring bit set.
        strip = _snapshot(led_strip(), LED_DEVICE)["led"]["strip"]
        self.assertEqual(
            strip["matrix_wiring"],
            {"progressive": True, "origin_bottom": False, "mirror_x": False, "swap_xy": False},
        )
        self.assertEqual(strip["matrix_tile_count"], 2)
        choices = _snapshot(led_strip(), LED_DEVICE)["led"]["choices"]
        self.assertEqual(choices["led_type_apa102"], lm.MB2WS_LED_TYPE_APA102)

    def test_choices_ride_every_snapshot_but_a_panel_poll(self) -> None:
        plc = led_strip()
        full = _snapshot(plc, LED_DEVICE, active_tab=led_poll.LED_TAB_STRIP)["led"]
        choices = full.get("choices") or {}
        for key in self.CHOICE_KEYS:
            self.assertTrue(choices.get(key), "список «%s» пуст или отсутствует" % key)
        self.assertEqual(
            [c for c, _lab in choices["led_types"]],
            [c for c, _lab in lm.rgbw_led_type_choices()],
        )
        self.assertEqual(choices["text_max_chars"], lm.MB2WS_TEXT_MAX_CHARS)
        self.assertEqual(list(choices["text_2x_hint_args"]), list(lm.rgbw_text_2x_hint_args()))
        # The 1 s background poll is the hot path on a shared line: the static
        # lists must not ride it (the window caches them from the first snapshot).
        panel = _snapshot(
            plc, LED_DEVICE, snapshot_detail="panel", active_tab=led_poll.LED_TAB_STRIP
        )["led"]
        self.assertNotIn("choices", panel)

    def test_fx_groups_tile_the_catalog_in_display_order(self) -> None:
        groups = _snapshot(led_strip(), LED_DEVICE)["led"]["scene"]["fx_groups"]
        ids: List[int] = []
        for g in groups:
            ids.extend(range(int(g["first"]), int(g["last"]) + 1))
        self.assertEqual(ids, list(range(lm.RGBW_FX_MODE_COUNT)))
        self.assertEqual([g["key"] for g in groups], [k for _g, k in lm.rgbw_fx_group_choices()])

    def test_aux_spec_follows_the_current_effect(self) -> None:
        expect = {
            62: lm.RGBW_FX_AUX_LOW_STYLE,
            51: lm.RGBW_FX_AUX_LOW_ESCORT,
            70: lm.RGBW_FX_AUX_LOW_FLAG,
            75: lm.RGBW_FX_AUX_LOW_LEVEL,
            43: lm.RGBW_FX_AUX_LOW_PERCENT,
            66: lm.RGBW_FX_AUX_LOW_DIRECTION,
            0: lm.RGBW_FX_AUX_LOW_VARIANT,
        }
        for fx, kind in expect.items():
            spec = _snapshot(_strip_with_fx(fx), LED_DEVICE)["led"]["scene"]["aux_spec"]
            self.assertEqual(spec["low_kind"], kind, fx)
        escort = _snapshot(_strip_with_fx(51), LED_DEVICE)["led"]["scene"]["aux_spec"]
        self.assertEqual(escort["high_kind"], lm.RGBW_FX_AUX_HIGH_LENGTH)
        text = _snapshot(_strip_with_fx(62), LED_DEVICE)["led"]["scene"]["aux_spec"]
        self.assertEqual(len(text["low_choices"]), lm.MB2WS_FX_AUX_STYLE_MAX + 1)
        self.assertEqual(text["high_kind"], lm.RGBW_FX_AUX_HIGH_COLOR)

    def test_fx_aux_fields_decompose_by_the_effect_the_device_runs(self) -> None:
        # The fixture holds 455 = 0x0102: style 2 + colour 1 for the marquee, colour
        # code 2 + pool length 1 for Escort, and a plain variant with the high byte
        # ignored for Static.
        for fx, want in ((62, (2, False, 1)), (51, (2, False, 1)), (0, (2, False, 0))):
            md = _snapshot(
                _strip_with_fx(fx), LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE
            )["led"]["mode_data"]
            fields = md["fx_aux_fields"]
            self.assertEqual((fields["low"], fields["flag"], fields["high"]), want, fx)

    def test_colour_hex_and_scaled_telemetry(self) -> None:
        pwm = _snapshot(led_strip(), LED_DEVICE, active_tab=led_poll.LED_TAB_PWM)["led"]["pwm"]
        self.assertEqual(pwm["color_hex"], lm.rgbw_pwm_permille_to_hex(250, 500, 750))
        self.assertEqual(pwm["ntc_c"], lm.rgbw_ntc_celsius(315))
        self.assertEqual(pwm["vled_v"], lm.rgbw_vled_volts(1198))
        md = _snapshot(led_strip(), LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)["led"]["mode_data"]
        self.assertEqual(len(md["text_colors_hex"]), 4)
        self.assertEqual(
            md["text_colors_hex"][0], lm.rgbw_rgb565_to_hex(lm.rgbw_hex_to_rgb565("#FF0000"))
        )
        self.assertEqual(
            md["wx_temp_color_hex"], lm.rgbw_rgb565_to_hex(lm.rgbw_hex_to_rgb565("#00FF00"))
        )

    def test_aux_hint_keys_ride_the_scene_tab_only(self) -> None:
        # Fixture: marquee (62), style 2, text «ЦИНТРОН», TextLines = 2 — so the
        # two-line hint applies and the wide-text hint always does.
        scene = _snapshot(led_strip(), LED_DEVICE, active_tab=led_poll.LED_TAB_SCENE)["led"]["scene"]
        self.assertEqual(
            list(scene["aux_hint_keys"]), list(lm.rgbw_fx_aux_hint_keys(62, 2, "ЦИНТРОН", 2))
        )
        self.assertIn("rgbw_fx_aux_style_hint_two_line", scene["aux_hint_keys"])
        pwm = _snapshot(led_strip(), LED_DEVICE, active_tab=led_poll.LED_TAB_PWM)["led"]["scene"]
        self.assertEqual(list(pwm["aux_hint_keys"]), [])


class TestWindowActionsL4(unittest.TestCase):
    """L4 (1.0.6.40): the two request shapes the window adds to the allow-list."""

    def test_scene_aux_and_density_land_as_a_plain_second_op(self) -> None:
        plc = led_strip()
        _led_write(
            plc, LED_DEVICE, "scene",
            {"source": lm.RGBW_SCENE_UI_FX, "fx_id": 62,
             "fx_aux": {"low": 5, "flag": False, "high": 3}, "fx_density": 77},
        )
        regs = plc.written_regs()
        addrs = [a for a, _v in regs]
        # 455/456 are NOT lock-gated: they follow the closing lock write, never
        # sit inside the bracket (an aborted batch must not leave them half-written
        # under an open lock either).
        last_lock = len(addrs) - 1 - addrs[::-1].index(lm.MB2WS_LOCK)
        self.assertGreater(addrs.index(lm.MB2WS_FX_AUX), last_lock)
        self.assertGreater(addrs.index(lm.MB2WS_FX_DENSITY), last_lock)
        writes = dict(regs)
        self.assertEqual(writes[lm.MB2WS_FX_AUX], lm.rgbw_fx_aux_compose(62, low=5, flag=False, high=3))
        self.assertEqual(writes[lm.MB2WS_FX_DENSITY], 77)

    def test_scene_aux_high_byte_is_forced_zero_where_the_mode_ignores_it(self) -> None:
        plc = led_strip()
        _led_write(
            plc, LED_DEVICE, "scene",
            {"source": lm.RGBW_SCENE_UI_FX, "fx_id": 1, "fx_aux": {"low": 0, "high": 6}},
        )
        self.assertEqual(dict(plc.written_regs())[lm.MB2WS_FX_AUX] >> 8, 0)

    def test_scene_without_aux_fields_writes_no_plain_op(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "scene", {"source": lm.RGBW_SCENE_UI_FX, "fx_id": 1})
        self.assertNotIn(lm.MB2WS_FX_AUX, plc.written_addresses())
        self.assertNotIn(lm.MB2WS_FX_DENSITY, plc.written_addresses())

    def test_pwm_colour_writes_level_then_mirror_for_r_g_b(self) -> None:
        plc = led_strip()
        _led_write(plc, LED_DEVICE, "pwm", {"color": "#FF8000"})
        r, g, b = lm.rgbw_hex_to_pwm_permille("#FF8000")
        lvl, mir = lm.RGBW_PWM_HOLDING_BASE, lm.RGBW_PWM_MIRROR_BASE
        self.assertEqual(
            plc.written_regs(),
            [(lvl, r), (mir, r), (lvl + 1, g), (mir + 1, g), (lvl + 2, b), (mir + 2, b)],
        )

    def test_pwm_bad_colour_is_refused_before_the_port_is_opened(self) -> None:
        with patch.object(
            device_config, "_open_transport", side_effect=AssertionError("порт открыт")
        ):
            with self.assertRaises(ValueError):
                device_config.led_write("/dev/ttyS4", LED_DEVICE, "pwm", {"color": "red"})


if __name__ == "__main__":
    unittest.main()

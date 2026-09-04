# -*- coding: utf-8 -*-
"""LED strip poller (`type: led`) — MR-02m RGBW_WS2812 (type 120, MB2WS base 400).

Why a poller of its own rather than a `type: template` entry:

  * every settings register 400..419 is LOCK-GATED. A write has to be bracketed
    by an unlock of register 410 → dependency-ordered writes → re-lock (the key
    and the order are the map's, `rgbw_lock_bracket_writes`), and a template
    emits a bare FC06.
    PlayCtrl 416 is inside that block, so an unbracketed "off" is REFUSED by the
    device while the ack reads normal: the strip keeps playing and MQTT reports
    it off. That fail-open is the whole reason `power` is not a template row;
  * the marquee text is a 64-register cp1251 block write, not a value;
  * `color` is three registers (PWM R/G/B permille) presented as one control,
    written in ONE FC16 so a half-applied colour never reaches the LEDs.

The register map is NOT here: it lives in `sa02m_led` (installed to /opt/sa02m-led,
shared with the flasher daemon). Control inventory: `sa02m_led.controls`.
Contract: docs/contracts/led-mb2ws.md. Topics: docs/MQTT_TOPICS.md.

MIXED BAUD ON ONE LINE IS NOT SUPPORTED — deliberately. The strip leaves the
factory at 115200 while the Carel units on the same bench line sit at 19200.
The bridge pools serial handles by `port:baud` (bridge_serial.get_port) and runs
one PortCycleScheduler per key, and ModbusSerial opens the tty with
`exclusive=True`; two keys on one physical port therefore mean a second
`serial.Serial` open on a locked device, not a shared line. There is no
per-transaction baud arbitration and adding one would re-open and re-configure
the tty around every frame, slowing every other device on that line. So: put the
strip on its own COM port, or change the strip's baud to the line's. The entry
module logs the conflict by name at composition
(`modbus_mqtt_bridge.mixed_baud_port_conflicts`) instead of failing silently.

NOT VERIFIED ON HARDWARE. No type-120 device has answered a scan on this branch,
so everything below is unit-test evidence over a FakeSerial. Two reads are the
MR-02m FAMILY convention rather than a product-low-map fact — the live DI block
(Input 18..21) and the NTC/VLED scales — which is why they are read as OPTIONAL
blocks: a failure there publishes a read error for those controls only and never
marks the device offline.
"""
from __future__ import annotations

import os
import sys
import time

from bridge_device import DevicePoller
from bridge_mqtt import DeviceLiveCache, MQTTPublisher


def _import_led():
    """Import the shared LED package from wherever it is deployed.

    /opt/sa02m-led is on no service's PYTHONPATH: the bridge runs out of
    /opt/sa02m-modbus-mqtt as root, the flasher out of /opt/sa02m-flasher as its
    own user. Try the installed path first, then the repo layout (tests and a
    dev checkout run from the repo root).
    """
    try:
        from sa02m_led import led_mb2ws, controls  # noqa: F401
        return led_mb2ws, controls
    except ImportError:
        pass
    candidates = [os.environ.get("SA02M_LED_DIR", "/opt/sa02m-led")]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "sa02m-led"))
    for path in candidates:
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    from sa02m_led import led_mb2ws, controls  # re-raise if truly absent
    return led_mb2ws, controls


lm, lc = _import_led()

DEFAULT_POLL_S = 2.0
# The marquee window is 64 registers (a ~133-byte reply). It is volatile on the
# device and changes only when something writes it, so it rides a slow cadence
# instead of the 2 s loop — the read-back exists to tell the truth after a power
# cycle wiped it, not to track a value that moves on its own.
DEFAULT_TEXT_POLL_S = 30.0

# The ONLY multi-register writes this poller may issue: {start: length}.
# An executor guard independent of the control table, because the address that
# must never be written is one the map itself refuses to name — the pre-1.0.2.2
# marquee base sat below the safe-state PWM block and a text write drove those
# outputs to 100 %. A block write that drifts down to it is refused here even if
# a future control table asks for it.
_LEGAL_BLOCK_WRITES = {
    lm.MB2WS_TEXT_BASE: lm.MB2WS_TEXT_REG_COUNT,
    lm.RGBW_PWM_HOLDING_BASE: 3,          # the R/G/B triple of `color`
}

_TRUE_WORDS = ("1", "true", "on", "yes")
_FALSE_WORDS = ("0", "false", "off", "no")


class LedPoller(DevicePoller):
    """Poll an LED strip and publish the controls named in sa02m_led.controls."""

    def __init__(self, cfg: dict, pub: MQTTPublisher):
        super().__init__(cfg, pub)
        self._poll_s = float(cfg.get("poll_s", DEFAULT_POLL_S))
        self._poll_text_s = float(cfg.get("poll_text_s", DEFAULT_TEXT_POLL_S))
        self._t_poll = 0.0
        self._t_text = 0.0
        self._names = set(lc.control_names())

    # --- setup ----------------------------------------------------------------

    def setup(self) -> None:
        name = self.cfg.get("name") or "LED (%s addr=%d)" % (
            self.port_path.replace("/dev/", ""), self.address)
        self.publish_device_meta(name, driver="led")
        for cname, wb_type, units, readonly, _reg in lc.CONTROLS:
            self.pub.pub_control_meta(self.device_id, cname, "type", wb_type)
            self.pub.pub_control_meta(
                self.device_id, cname, "readonly", "1" if readonly else "0")
            if units:
                self.pub.pub_control_units(self.device_id, cname, units)
        for cname, (lo, hi) in lc.RANGE_LIMITS.items():
            self.pub.pub_control_meta(self.device_id, cname, "min", str(lo))
            self.pub.pub_control_meta(self.device_id, cname, "max", str(hi))
        self.pub.pub_control_meta(
            self.device_id, "text", "max", str(lc.TEXT_MAX_CHARS))
        for cname in lc.writable_names():
            self.pub.subscribe_writeback(
                self.device_id, cname, self._make_writeback_cb(cname))

    def fmb_event_ranges(self):
        """No Fast Modbus: the strip's event ranges are unverified, so none are claimed.

        Configuring FC 0x46 event ranges against a device that does not implement
        them costs a failed transaction per reconfigure on a shared line, and a
        guessed range would silently publish the wrong register as an event.
        """
        return []

    # --- poll -----------------------------------------------------------------

    def poll_io(self) -> None:
        now = time.monotonic()
        if now - self._t_poll < self._poll_s:
            return
        self._t_poll = now
        t_read = now
        snap = self._read_snapshot()
        if snap is None:
            return
        self._publish(self._controls_from_snapshot(snap), t_read)
        DeviceLiveCache.flush_file(self.device_id)

    def poll_slow_if_due(self, now: float) -> None:
        if now - self._t_text < self._poll_text_s:
            return
        self._t_text = now
        regs = self._read_optional(
            "holding", lm.MB2WS_TEXT_BASE, lm.MB2WS_TEXT_REG_COUNT)
        if regs is None:
            self.pub.pub_error(self.device_id, "text", "r")
            return
        # An empty read-back is the TRUTH, not a lost write: 516..579 is
        # pixel-data class and a power cycle clears it.
        self._publish({"text": lm.rgbw_unpack_text_cp1251(regs)}, now)
        DeviceLiveCache.flush_file(self.device_id)

    def _publish(self, values: dict, t_read: float) -> None:
        for cname, value in values.items():
            if cname not in self._names:
                continue
            if value is None:
                # Block absent / read failed: an error flag, never a made-up 0.
                self.pub.pub_error(self.device_id, cname, "r")
                continue
            self._wb_publish_poll(cname, value, t_read)
            self.pub.pub_error(self.device_id, cname, "")

    def _read_optional(self, kind: str, start: int, count: int):
        """Read a block that may not exist on this firmware — None on failure.

        Deliberately NOT through the inherited read wrappers: those feed the
        availability state machine, and an unmapped optional block would take a
        perfectly healthy strip offline and back off the whole poll. The port
        object is still the pooled ModbusSerial, so the port lease is unaffected.
        """
        port = self.get_port()
        fn = (port.read_input_registers if kind == "input"
              else port.read_holding_registers)
        try:
            return fn(self.address, start, count)
        except Exception as e:
            self.log.debug("led optional block %s %d+%d: %s", kind, start, count, e)
            return None

    def _read_snapshot(self) -> dict | None:
        """One poll: the mandatory settings block plus four optional ones."""
        a = self.address
        try:
            span = (lm.MB2WS_LOCK_GATED_LAST - lm.MB2WS_LOCK_GATED_FIRST) + 1
            base = self.read_holding_registers(a, lm.MB2WS_LOCK_GATED_FIRST, span)
        except Exception as e:
            self.log.warning("led poll: %s", e)
            return None
        out: dict = {"base": [int(v) & 0xFFFF for v in base]}
        out["pwm"] = self._read_optional(
            "holding", lm.RGBW_PWM_HOLDING_BASE, lm.RGBW_PWM_CHANNELS)
        # 33..36 and 49 are separated by addresses this product's low map does
        # not document as mapped, and one unmapped address fails a whole block.
        mode = self._read_optional(
            "holding", lm.RGBW_PWM_STRIP_MODE_HOLDING, 1)
        out["pwm_mode"] = None if mode is None else int(mode[0]) & 0xFFFF
        out["supply"] = self._read_optional("input", lm.RGBW_IREG_NTC, 2)
        out["di"] = self._read_optional(
            "input", lm.RGBW_DI_INPUT_BASE, lm.RGBW_DI_COUNT)
        return out

    # --- snapshot -> controls -------------------------------------------------

    def _controls_from_snapshot(self, snap: dict) -> dict:
        base = snap["base"]

        def reg(address: int) -> int:
            return base[address - lm.MB2WS_LOCK_GATED_FIRST]

        play = reg(lm.MB2WS_PLAY_CTRL)
        # A Gyver alias (128..209) is the same effect by another number; the
        # control publishes the canonical id so a read-back matches what was
        # written and the group lookup cannot fall off the end of the catalog.
        fx_id = lm.rgbw_resolve_fx_id(reg(lm.MB2WS_FX_ID))
        out = {
            "power": "1" if play == lm.MB2WS_PLAY_PLAY else "0",
            "play_state": str(play),
            "brightness": str(reg(lm.MB2WS_FX_PARAM)),
            "effect": str(fx_id),
            "effect_group": lm.rgbw_fx_group_of(fx_id),
            "speed": str(reg(lm.MB2WS_FX_SPEED)),
            "scene_source": str(reg(lm.MB2WS_RENDER_SOURCE)),
            "led_count": str(reg(lm.MB2WS_LED_COUNT0)),
            "led_type": str(reg(lm.MB2WS_LED_TYPE)),
            "line_mode": str(reg(lm.MB2WS_LINE_MODE)),
        }
        out.update(self._pwm_controls(snap))
        out.update(self._supply_controls(snap))
        out.update(self._di_controls(snap))
        return out

    @staticmethod
    def _pwm_controls(snap: dict) -> dict:
        pwm = snap.get("pwm")
        mode = snap.get("pwm_mode")
        out = {"pwm_mode": None if mode is None else str(mode)}
        if pwm is None or len(pwm) < lm.RGBW_PWM_CHANNELS:
            out["color"] = None
            out["white"] = None
            return out
        r, g, b, w = (int(v) & 0xFFFF for v in pwm[:lm.RGBW_PWM_CHANNELS])
        out["color"] = lm.rgbw_pwm_permille_to_hex(r, g, b)
        out["white"] = str(w)
        return out

    @staticmethod
    def _supply_controls(snap: dict) -> dict:
        supply = snap.get("supply")
        if supply is None or len(supply) < 2:
            return {"temperature": None, "vled": None}
        return {
            "temperature": str(lm.rgbw_ntc_celsius(supply[0])),
            "vled": str(lm.rgbw_vled_volts(supply[1])),
        }

    @staticmethod
    def _di_controls(snap: dict) -> dict:
        di = snap.get("di")
        out = {}
        for i in range(lm.RGBW_DI_COUNT):
            name = "di_%d" % (i + 1)
            if di is None or i >= len(di):
                out[name] = None
            else:
                out[name] = str(int(di[i]) & 1)
        return out

    # --- writeback ------------------------------------------------------------

    def _make_writeback_cb(self, name: str):
        def cb(client, userdata, msg):
            if msg.retain:
                # A retained /on must not replay a stop (or a colour) at broker
                # restart — the strip's state is the device's, not the broker's.
                return
            try:
                payload = msg.payload.decode().strip()
            except UnicodeDecodeError:
                return
            self._wb_submit(name, lambda: self._writeback(name, payload))
        return cb

    def _writeback(self, name: str, payload: str) -> None:
        if self._wb_offline_skip(name):
            return
        try:
            handler = getattr(self, "_wb_" + name, None)
            if handler is None:
                self.log.warning("led writeback: no handler for %s", name)
                return
            handler(payload)
        except Exception as e:
            # Every handler validates its payload BEFORE it touches the bus, so
            # a refusal here means nothing was written.
            self.log.warning("led writeback %s: %s", name, e)
            self.pub.pub_error(self.device_id, name, "w")

    # --- write primitives -----------------------------------------------------

    def _write_settings(self, writes: dict) -> None:
        """Write settings-block registers through the map's lock bracket.

        The call site NEVER decides whether an unlock is needed and never orders
        the writes: `rgbw_lock_bracket_writes` derives both from the map. A
        hand-listed set is exactly how PlayCtrl 416 lost its unlock on the
        desktop and a reported "stop" left the strip playing.

        On a failed write the bracket is closed by hand before re-raising: the
        map's helper is pure and cannot know a write failed, and a device left
        with 410 unlocked accepts settings writes from anything on the line.
        """
        plan = lm.rgbw_lock_bracket_writes(writes)
        try:
            for reg, value in plan:
                self._wb_write_retry(
                    lambda r=reg, v=value: self.write_register(self.address, r, v))
        except Exception:
            if any(lm.rgbw_reg_is_lock_gated(r) for r in writes):
                try:
                    self.get_port().write_register(
                        self.address, lm.MB2WS_LOCK, 0)
                except Exception:
                    pass
            raise

    def _write_block(self, start: int, values) -> None:
        """One FC16, refused unless (start, length) is a sanctioned block write."""
        vals = [int(v) & 0xFFFF for v in values]
        expected = _LEGAL_BLOCK_WRITES.get(int(start))
        if expected is None or expected != len(vals):
            raise ValueError(
                "refused block write %d x%d — not a sanctioned LED block"
                % (int(start), len(vals)))
        self._wb_write_retry(
            lambda: self.get_port().write_registers(self.address, int(start), vals))

    # --- writeback handlers ---------------------------------------------------

    def _wb_power(self, payload: str) -> None:
        on = _bool_payload(payload)
        value = lm.MB2WS_PLAY_PLAY if on else lm.MB2WS_PLAY_STOP
        self._write_settings({lm.MB2WS_PLAY_CTRL: value})
        self._wb_done("power", "1" if on else "0")

    def _wb_brightness(self, payload: str) -> None:
        self._wb_ranged_setting("brightness", payload, lm.MB2WS_FX_PARAM)

    def _wb_speed(self, payload: str) -> None:
        self._wb_ranged_setting("speed", payload, lm.MB2WS_FX_SPEED)

    def _wb_scene_source(self, payload: str) -> None:
        self._wb_ranged_setting("scene_source", payload, lm.MB2WS_RENDER_SOURCE)

    def _wb_effect(self, payload: str) -> None:
        """FxId 405 — clamped by the MAP, not by a number typed here.

        `controls.RANGE_LIMITS['effect']` mirrors the same bound; the package
        tests pin the two against each other so a firmware that grows an effect
        cannot leave one of them behind.
        """
        fx_id = lm.rgbw_clamp_fx_id(_int_payload(payload))
        self._write_settings({lm.MB2WS_FX_ID: fx_id})
        self._wb_done("effect", str(fx_id))

    def _wb_ranged_setting(self, name: str, payload: str, reg: int) -> None:
        value = _clamp(_int_payload(payload), *lc.RANGE_LIMITS[name])
        self._write_settings({reg: value})
        self._wb_done(name, str(value))

    def _wb_white(self, payload: str) -> None:
        """The W channel — a plain FC06: the PWM block is not lock-gated."""
        value = _clamp(_int_payload(payload), *lc.RANGE_LIMITS["white"])
        self._wb_write_retry(
            lambda: self.write_register(
                self.address, lm.RGBW_PWM_HOLDING_BASE + 3, value))
        self._wb_done("white", str(value))

    def _wb_color(self, payload: str) -> None:
        """The R/G/B triple 33..35 in ONE FC16.

        Three FC06 writes would put a wrong colour on the LEDs between frames;
        one transaction cannot.
        """
        triple = _parse_color(payload)
        if triple is None:
            raise ValueError("unparseable colour payload %r" % (payload,))
        self._write_block(lm.RGBW_PWM_HOLDING_BASE, triple)
        self._wb_done("color", lm.rgbw_pwm_permille_to_hex(*triple))

    def _wb_text(self, payload: str) -> None:
        """The marquee window — always the full 64 registers at the CURRENT base.

        Never a partial write and never a computed start: the packer produces the
        whole window, `_write_block` refuses any other shape, and the map owns
        the base address (the one that had to move off the safe-state AO block).
        """
        text = lm.rgbw_truncate_text(payload)
        self._write_block(lm.MB2WS_TEXT_BASE, lm.rgbw_pack_text_cp1251(text))
        self._wb_done("text", text)


# --- small helpers -----------------------------------------------------------

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(int(lo), min(int(hi), int(value)))


def _int_payload(payload: str) -> int:
    """MQTT payload → int, raising on anything else (never a silent 0)."""
    return int(float(str(payload).strip()))


def _bool_payload(payload: str) -> bool:
    """MQTT payload → bool, raising on anything that is not a known word.

    A switch payload the device would act on must be recognised, not guessed:
    treating every unknown string as "on" is how a typo turns a strip on.
    """
    t = str(payload).strip().lower()
    if t in _TRUE_WORDS:
        return True
    if t in _FALSE_WORDS:
        return False
    raise ValueError("unrecognised switch payload %r" % (payload,))


def _parse_color(payload: str):
    """Colour payload → the PWM permille triple, or None when unparseable.

    THREE forms are accepted and the choice is not cosmetic — see
    docs/MQTT_TOPICS.md «Формат `color`»:

      * `#RRGGBB` — the form this poller PUBLISHES, and the one the Alice bridge
        already reads (sa02m_alice.client.converters.mqtt_to_color_setting);
      * a decimal 24-bit integer — what that same Alice bridge WRITES BACK
        (yandex_to_color_setting emits `str(int(value))`). Refusing it would
        break the round trip in the one consumer this control has;
      * `R;G;B` 0..255 — the Wiren Board convention for a control of type `rgb`,
        so a wb-rules script or a WB-aware client is not silently misread.

    Bare `RRGGBB` without the `#` is NOT hex here: it is ambiguous with the
    decimal form (`255000` is a valid value in both) and the Alice reader draws
    the same line, requiring the `#`.
    """
    t = str(payload or "").strip()
    if not t:
        return None
    if ";" in t:
        parts = t.split(";")
        if len(parts) != 3:
            return None
        try:
            rgb = [int(float(p)) for p in parts]
        except ValueError:
            return None
        if any(v < 0 or v > 255 for v in rgb):
            return None
        return tuple(lm.rgbw_rgb8_to_permille(v) for v in rgb)
    if t.startswith("#"):
        return lm.rgbw_hex_to_pwm_permille(t)
    try:
        packed = int(float(t))
    except ValueError:
        return None
    if packed < 0 or packed > 0xFFFFFF:
        return None
    return lm.rgbw_hex_to_pwm_permille("#%06X" % packed)

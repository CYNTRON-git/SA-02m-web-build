"""Fast Modbus event engine for the SA-02m Modbus->MQTT bridge.

FastModbusEventPortManager (FC 0x46 configure_events / poll_events per
port) and its timing constants. References pollers duck-typed only (never
imports the poller/device modules). Split out of modbus_mqtt_bridge.py by the
bridge decompose (backlog "Decompose worklist" — the entry was the
fastest-growing module across three audits).

NOT quite verbatim, and this is the whole of the difference: the three
get_port(...) call sites in FastModbusEventPortManager now read _get_port(...),
the late-binding shim below. Name resolution only — no logic change, and every
class body is otherwise byte-identical to the monolith.
"""

from __future__ import annotations

import sys
import time
import threading
import logging

from bridge_serial import (
    FMB_EVT_COIL, FMB_EVT_DISCRETE, FMB_EVT_HOLDING, FMB_EVT_INPUT,
    FMB_EVT_REBOOT,
    ModbusSerial, build_fmb_poll_events,
    build_fmb_configure_events, build_fmb_configure_events_wb, crc16,
)


def _get_port(port_path: str, baudrate: int):
    """Resolve get_port through the entry module at call time.

    Not a plain import, for two reasons:
    * tests patch modbus_mqtt_bridge.get_port (test_ce_power_poll) and the
      patch must reach these call sites;
    * under systemd the entry runs as __main__ — importing it by name would
      execute it twice, so it is looked up in sys.modules, never imported.
    """
    entry = sys.modules.get("modbus_mqtt_bridge") or sys.modules["__main__"]
    return entry.get_port(port_path, baudrate)


# ── Fast Modbus (wb-mqtt-serial serial_client*.cpp / modbus_ext_common.cpp) ───
# ReadEventsPeriod @≥115200 → 50 ms; event burst ≤ MAX_POLL_TIME 100 ms;
# 0x12 ends burst only; classic = separate TimeBalancer POLLING task;
# sporadic-only insurance ≥500 ms; BALANCING_THRESHOLD 500 ms → Force poll.
FMB_EVENT_PERIOD_S = 0.05
FMB_EVENT_BURST_S = 0.10          # MAX_POLL_TIME
FMB_INSURANCE_POLL_S = 0.5        # DEFAULT_SPORADIC_ONLY_READ_RATE_LIMIT
FMB_BALANCING_THRESHOLD_S = 0.5   # BALANCING_THRESHOLD
FMB_MAX_POLL_TIME_S = 0.10        # MAX_POLL_TIME (events + classic slice)
FMB_RECONFIGURE_BACKOFF_S = 15.0  # rebooted-then-silent device retry window
                                  # (same order as the initial-offline
                                  # next_fmb_retry throttle in the run loop)
FMB_UNPARSED_LOG_PERIOD_S = 10.0  # unparsable-frame log throttle: the burst
                                  # loop runs at 20 Hz, so an unthrottled line
                                  # would write ~20 journal records/s per port


class FastModbusEventPortManager:
    """Wire-protocol helper for one RS-485 port (no own thread).

    Owned by PortCycleScheduler — same role as TSerialClientEventsReader
    inside TSerialClientRegisterAndEventsReader.
    """
    POLL_TIMEOUT = 0.25
    MAX_DATA_LEN = 128

    def __init__(self, port_path: str, baudrate: int):
        self._port_path = port_path
        self._baudrate = baudrate
        self._devices: dict[int, dict] = {}
        self._ack_slave: int = 0
        self._ack_flag:  int = 0
        self._last_poll_answered = False
        # Event-record generation per slave: True = WB grammar, False = legacy.
        # Authoritative source is the configure_events handshake; the parser
        # only fills it in for a slave nothing is known about yet
        # (docs/contracts/fmb-event-wire.md).
        self._wb_frame_slaves: dict[int, bool] = {}
        # slave → (last log monotonic, suppressed count) for unparsable frames.
        self._unparsed_log: dict[int, tuple[float, int]] = {}
        self._stop = threading.Event()
        self._log = logging.getLogger(f"fmb.{port_path.replace('/dev/', '')}")
        if baudrate >= 115200:
            self._event_period_s = 0.05
        elif baudrate >= 38400:
            self._event_period_s = 0.10
        else:
            self._event_period_s = 0.20

    @property
    def event_period_s(self) -> float:
        return self._event_period_s

    def has_devices(self) -> bool:
        return bool(self._devices)

    def has_configured(self) -> bool:
        return any(d.get("configured") for d in self._devices.values())

    def register_device(self, addr: int, device_id: str,
                        ranges: list[tuple[int, int, int]],
                        dispatch, poller=None, dev_type: str = "",
                        wire_mode: str = "auto") -> None:
        """ranges from poller.fmb_event_ranges(); dispatch = fmb_dispatch.

        dev_type + wire_mode (yaml `fmb_event_wire`: auto|wb|legacy) pick the
        configure_events form — see _configure_contracts().
        """
        mode = str(wire_mode or "auto").strip().lower()
        if mode not in ("auto", "wb", "legacy"):
            self._log.warning(
                "fmb_event_wire=%r addr=%d not recognized — using auto",
                wire_mode, addr)
            mode = "auto"
        self._devices[addr] = {
            "id": device_id,
            "ranges": list(ranges),
            "pending": list(ranges),
            "dispatch": dispatch,
            "poller": poller,
            "type": str(dev_type or "").strip().lower(),
            "wire_mode": mode,
            "configured": False,
            "retry_at": 0.0,
        }
        # A module reflashed since the last registration may have swapped
        # generations — start from "nothing known", let the handshake decide.
        self._wb_frame_slaves.pop(addr, None)

    # --- configure_events (0x18) for one device ------------------------------

    def _configure_contracts(self, addr: int, dev: dict) -> tuple[str, ...]:
        """Which configure_events forms to try for this device, in order.

        WB is probed only where it can pay off: MR-02m (the only family whose
        firmware branch speaks it) or an explicit yaml opt-in. An unexpected
        0x18 form once wedged a CE-02m-3 on COM2, recoverable only by a power
        cycle (CHANGELOG 1.0.5.46), and neither CE nor DTV ever receives MR-02m
        firmware — so probing them carries the documented risk for no gain.
        A handshake-confirmed contract goes first, so the extra probe is paid
        at most once per slave rather than on every reconfigure.
        """
        mode = dev.get("wire_mode", "auto")
        if mode == "legacy":
            contracts = ["legacy"]
        elif mode == "wb" or dev.get("type") == "mr02m":
            contracts = ["wb", "legacy"]
        else:
            contracts = ["legacy"]
        known = self._wb_frame_slaves.get(addr)
        if known is not None:
            preferred = "wb" if known else "legacy"
            if preferred in contracts:
                contracts = [preferred] + [c for c in contracts
                                           if c != preferred]
        return tuple(contracts)

    def _configure_device(self, ser: ModbusSerial, addr: int, dev: dict) -> bool:
        """One configure pass over the still-unacked ranges. Per-range
        graceful: a rejected range stays on classic polling while the rest
        get events. Returns True when no range is left pending."""

        def _try(frame: bytes) -> tuple[bool, str]:
            try:
                # ACK: [addr][0x46][0x18][mask_len][mask…][CRC] — mask_len
                # depends on count; leading 0xFF arbitration noise possible.
                resp = ser.fmb_send_recv(frame, 5, 48, 0.4)
                for i in range(max(0, len(resp) - 4)):
                    if (resp[i] == addr and resp[i + 1] == 0x46
                            and resp[i + 2] == 0x18):
                        return True, ""
                return False, ""
            except Exception as e:
                return False, f" ({e})"

        contracts = self._configure_contracts(addr, dev)
        still_pending: list[tuple[int, int, int]] = []
        for (evt_type, start_reg, count) in dev["pending"]:
            ok = False
            err = ""
            used = ""
            for contract in contracts:
                build = (build_fmb_configure_events_wb if contract == "wb"
                         else build_fmb_configure_events)
                ok, attempt_err = _try(
                    build(addr, evt_type, start_reg, count, 1))
                err = err or attempt_err
                if ok:
                    used = contract
                    break
            if ok:
                dev["configured"] = True
                # The handshake is the authoritative generation signal: from
                # here the parser no longer has to guess for this slave.
                self._wb_frame_slaves[addr] = (used == "wb")
                self._log.info(
                    "configure_events addr=%d type=0x%02X start=%d count=%d "
                    "— %s contract", addr, evt_type, start_reg, count, used)
            else:
                still_pending.append((evt_type, start_reg, count))
                self._log.warning(
                    "configure_events addr=%d type=0x%02X start=%d count=%d "
                    "rejected%s — classic polling covers this range",
                    addr, evt_type, start_reg, count, err)
        dev["pending"] = still_pending
        return not still_pending

    # --- poll_events (0x10) loop ---------------------------------------------

    def _poll_once(self, ser: ModbusSerial) -> tuple[bool, list[tuple]]:
        """
        One poll_events cycle.  Returns (had_events, [(slave, type, reg, val)]).
        """
        self._last_poll_answered = False
        frame = build_fmb_poll_events(1, self.MAX_DATA_LEN,
                                      self._ack_slave, self._ack_flag)
        buf = ser.fmb_send_recv(frame, 4, 256, self.POLL_TIMEOUT)
        # Перед кадром ответа мастер видит байты арбитража (рецессивные 0xFF
        # и обрезки слов) — ищем реальное начало кадра, как wb-mqtt-serial
        # (IsModbusExtRTUPacket в modbus_ext_common.cpp).
        for i in range(len(buf) - 2):
            if (buf[i] != 0xFF and buf[i + 1] == 0x46
                    and buf[i + 2] in (0x11, 0x12)):
                if i:
                    buf = buf[i:]
                break
        if len(buf) < 4 or buf[1] != 0x46:
            return False, []

        slave_id = buf[0]
        subcode  = buf[2]

        if subcode == 0x12:
            # No events: [slave][0x46][0x12][flag][CRC_L][CRC_H] = 6 bytes
            if len(buf) >= 6:
                flag = buf[3]
                calc = crc16(buf[:4])
                recv = buf[4] | (buf[5] << 8)
                if calc == recv:
                    self._ack_slave = slave_id
                    self._ack_flag  = flag
                    self._last_poll_answered = True
            return False, []

        if subcode != 0x11:
            return False, []

        # Events: [slave][0x46][0x11][FLAG][N][DATA_LEN]{events}[CRC_L][CRC_H]
        if len(buf) < 8:
            return False, []

        flag     = buf[3]
        n_events = buf[4]
        data_len = buf[5]
        total    = 6 + data_len + 2

        if len(buf) < total:
            return False, []

        # Verify CRC
        calc = crc16(buf[:6 + data_len])
        recv = buf[6 + data_len] | (buf[7 + data_len] << 8)
        if calc != recv:
            self._log.warning("poll_events: CRC error (slave=%d)", slave_id)
            return False, []

        # Parse events from buf[6 .. 6+data_len-1] — two record grammars,
        # one home: docs/contracts/fmb-event-wire.md.
        events = self._parse_event_records(slave_id, buf, 6, 6 + data_len,
                                           n_events)
        if events is None:
            self._log_unparsable(slave_id, n_events, data_len)
            events = []

        self._ack_slave = slave_id
        self._ack_flag  = flag
        self._last_poll_answered = True
        return True, events

    # --- event-record grammars (0x11) ----------------------------------------

    def _parse_event_records(self, slave_id: int, buf: bytes, start: int,
                             end: int, n_events: int) -> list[tuple] | None:
        """Decode one frame's records; None when neither grammar fits exactly.

        Length validation rejects most mismatches but is NOT a decider: a WB
        reboot record is byte-identical to a legacy COIL record at reg
        >= 0x0F00, and several two-record shapes collide as well. The
        deciding signal is the contract confirmed by configure_events
        (docs/contracts/fmb-event-wire.md); everything below is that cached
        contract as attempt order, plus the one rule that overrides it.
        """
        wb_events = self._parse_events_wb(slave_id, buf, start, end, n_events)
        if wb_events is not None and any(
                evt[1] == FMB_EVT_REBOOT for evt in wb_events):
            # THE reboot rule — decisive, and independent of both the record's
            # position in the frame and the cached generation. A frame that
            # decodes as WB *and carries a reboot* cannot come from a legacy
            # slave: the WB grammar accepts a record only when its second byte
            # is a wire code and its first byte equals that wire's payload
            # length, while in a legacy record those are the type and the
            # register high byte — and no (type, reg_high) pair a genuine
            # legacy frame can START with (the configured ranges plus a reboot
            # at any register) satisfies that gate, so such a frame fails WB on
            # its FIRST record whatever its length (pinned by
            # tests/test_fmb_event_parsers.py; the per-type register limits are
            # COIL 0x0F00, DISCRETE 0x0100, HOLDING 0x0300, INPUT unbounded).
            # Losing a reboot is the one unrecoverable outcome: dev["configured"]
            # would stay True, the reflashed module would keep its wiped event
            # table, and not a line would be logged.
            self._wb_frame_slaves.setdefault(slave_id, True)
            return wb_events

        prefer_wb = self._wb_frame_slaves.get(slave_id, True)
        order = ("wb", "legacy") if prefer_wb else ("legacy", "wb")
        for fmt in order:
            events = (wb_events if fmt == "wb"
                      else self._parse_events_legacy(slave_id, buf, start, end,
                                                     n_events))
            if events is not None:
                # Guess only for an unknown slave — never overwrite the
                # handshake-confirmed contract.
                self._wb_frame_slaves.setdefault(slave_id, fmt == "wb")
                return events
        return None

    @staticmethod
    def _parse_events_wb(slave_id: int, buf: bytes, start: int, end: int,
                         n_events: int) -> list[tuple] | None:
        """WB grammar: [LEN][TYPE wire][ID_H][ID_L][payload x LEN]."""
        wire_plen = {1: 1, 2: 1, 3: 2, 4: 2, 0x0F: 0}
        events: list[tuple] = []
        pos = start
        for _ in range(n_events):
            if pos + 4 > end:
                return None
            plen, wire = buf[pos], buf[pos + 1]
            if wire not in wire_plen or plen != wire_plen[wire]:
                return None
            reg = (buf[pos + 2] << 8) | buf[pos + 3]
            pos += 4
            if pos + plen > end:
                return None
            if plen == 0:
                val = -1
            elif plen == 1:
                val = buf[pos]
            else:
                # WB standard: VALUE little-endian (legacy frames carry it BE).
                val = buf[pos] | (buf[pos + 1] << 8)
            pos += plen
            internal = FMB_EVT_REBOOT if wire == 0x0F else wire - 1
            events.append((slave_id, internal, reg, val))
        return events if pos == end else None

    @staticmethod
    def _parse_events_legacy(slave_id: int, buf: bytes, start: int, end: int,
                             n_events: int) -> list[tuple] | None:
        """Legacy grammar: [TYPE int][REG_H][REG_L][payload sized by type]."""
        events: list[tuple] = []
        pos = start
        for _ in range(n_events):
            if pos + 3 > end:
                return None
            evt_type = buf[pos]
            reg      = (buf[pos + 1] << 8) | buf[pos + 2]
            pos += 3
            if evt_type in (FMB_EVT_COIL, FMB_EVT_DISCRETE):
                if pos >= end:
                    return None
                val = buf[pos]; pos += 1
            elif evt_type in (FMB_EVT_HOLDING, FMB_EVT_INPUT):
                if pos + 1 >= end:
                    return None
                val = (buf[pos] << 8) | buf[pos + 1]; pos += 2
            elif evt_type == FMB_EVT_REBOOT:
                val = -1
            else:
                return None
            events.append((slave_id, evt_type, reg, val))
        return events if pos == end else None

    def _log_unparsable(self, slave_id: int, n_events: int,
                        data_len: int) -> None:
        """One line per slave per FMB_UNPARSED_LOG_PERIOD_S, suppressed count
        included — a permanently mismatched slave must not fill the device's
        small journal from the 20 Hz burst loop."""
        now = time.monotonic()
        prev = self._unparsed_log.get(slave_id)
        if prev is not None and now - prev[0] < FMB_UNPARSED_LOG_PERIOD_S:
            self._unparsed_log[slave_id] = (prev[0], prev[1] + 1)
            return
        suppressed = prev[1] if prev is not None else 0
        self._unparsed_log[slave_id] = (now, 0)
        self._log.warning(
            "poll_events: unparsable event frame (slave=%d, %d ev, %d B)%s",
            slave_id, n_events, data_len,
            " — %d more suppressed in the last %.0fs"
            % (suppressed, FMB_UNPARSED_LOG_PERIOD_S) if suppressed else "")

    # --- MQTT dispatch -------------------------------------------------------

    def _dispatch(self, slave_id: int, evt_type: int, reg: int, val: int) -> None:
        dev = self._devices.get(slave_id)
        if not dev:
            return
        self._log.debug("event %s type=%02X reg=%d val=%d",
                        dev["id"], evt_type, reg, val)
        if evt_type == FMB_EVT_REBOOT:
            # wb-mqtt-serial: reboot → SetDisconnected + re-EnableEvents.
            self._log.info("Device addr=%d rebooted (event 0x0F)", slave_id)
            dev["pending"] = list(dev["ranges"])
            dev["configured"] = False
            # A reboot is also what a just-reflashed module sends: the cached
            # generation may now be wrong, so the next handshake re-decides.
            self._wb_frame_slaves.pop(slave_id, None)
            # The reboot event proves the device is talking — reconfigure
            # immediately, regardless of a backoff from an earlier silence.
            dev["retry_at"] = 0.0
            poller = dev.get("poller")
            if poller is not None:
                poller.set_fmb_io_covered(False)
            return
        dev["dispatch"](evt_type, reg, val)

    def configure_all(self, *, only_ready: bool = False) -> None:
        """EnableEvents for registered devices.

        only_ready: skip devices that have not completed classic reads yet
        (CE wedged after 0x18 while silent — arm FMB only after mark_ok).
        """
        if not self._devices:
            return
        ser = _get_port(self._port_path, self._baudrate)
        for addr, dev in self._devices.items():
            if dev.get("configured") and not dev.get("pending"):
                continue
            poller = dev.get("poller")
            if only_ready and poller is not None:
                if not poller.classic_ready_for_fmb():
                    continue
            for _attempt in range(3):
                if self._configure_device(ser, addr, dev):
                    break
                time.sleep(0.5)
            if dev["configured"]:
                acked = [r for r in dev["ranges"] if r not in dev["pending"]]
                self._log.info(
                    "FMB events configured addr=%d (%s) ranges=%s%s",
                    addr, dev["id"],
                    ["type=0x%02X start=%d count=%d" % r for r in acked],
                    " — %d range(s) rejected, classic polling covers them"
                    % len(dev["pending"]) if dev["pending"] else "")
                if poller is not None:
                    poller.set_fmb_io_covered(True)
                    try:
                        poller.poll_io()
                    except Exception as e:
                        self._log.debug("post-configure poll %s: %s",
                                        poller.device_id, e)
            else:
                self._log.warning(
                    "FMB events config failed addr=%d — polling only", addr)
                if poller is not None:
                    poller.set_fmb_io_covered(False)
        if not self.has_configured() and not only_ready:
            self._log.warning(
                "FMB events: no devices configured — classic polling only")

    def reconfigure_pending(self) -> None:
        """After reboot events — wb RescheduleDisconnectedDevices / EnableEvents."""
        now = time.monotonic()
        ser = None
        for addr, dev in self._devices.items():
            if dev["configured"] or not dev.get("pending"):
                continue
            # A rebooted-then-silent device must not burn the 0.4 s configure
            # timeout per range under the port lock on every ~50 ms event
            # cycle — back off like the run loop's next_fmb_retry throttle.
            if now < dev.get("retry_at", 0.0):
                continue
            if ser is None:
                ser = _get_port(self._port_path, self._baudrate)
            if self._configure_device(ser, addr, dev) and dev["configured"]:
                poller = dev.get("poller")
                if poller is not None:
                    poller.set_fmb_io_covered(True)
                self._log.info(
                    "FMB events re-configured addr=%d (%s) after reboot",
                    addr, dev["id"])
            if not dev["configured"]:
                dev["retry_at"] = now + FMB_RECONFIGURE_BACKOFF_S

    def retry_unconfigured(self) -> bool:
        """Quiet retry when first configure_all failed (device was offline)."""
        if not self._devices:
            return self.has_configured()
        # Prefer the shared configure path with classic-ready gate.
        self.configure_all(only_ready=True)
        return self.has_configured()

    def event_burst(self) -> int:
        """ModbusExt::ReadEvents loop: 0x11 continue, 0x12 / timeout stop.

        Returns count of unanswered polls in this burst (0 = OK).
        """
        ser = _get_port(self._port_path, self._baudrate)
        t0 = time.monotonic()
        silent = 0
        while not self._stop.is_set():
            if time.monotonic() - t0 >= FMB_EVENT_BURST_S:
                break
            had_events, events = self._poll_once(ser)
            for (slave_id, evt_type, reg, val) in events:
                self._dispatch(slave_id, evt_type, reg, val)
            if not self._last_poll_answered:
                silent += 1
                break
            if not had_events:
                break
        return silent

    def set_insurance(self, armed: bool) -> None:
        for dev in self._devices.values():
            poller = dev.get("poller")
            if poller is None:
                continue
            if armed and dev.get("configured"):
                poller.set_fmb_io_covered(True)
            else:
                poller.set_fmb_io_covered(False)

    def stop(self) -> None:
        self._stop.set()

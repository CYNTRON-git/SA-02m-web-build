"""Device runtime base for the SA-02m bridge: poller base + port cycle.

DevicePoller (availability state machine, writeback plumbing, FMB
insurance switching) and PortCycleScheduler (one thread per port:baud,
interleaved EVENTS + POLLING). Split out of modbus_mqtt_bridge.py verbatim
(1.0.5.55 decompose).
"""

from __future__ import annotations

import time
import threading
import logging

import bridge_serial
from bridge_serial import ModbusSerial, WRITEBACK_POLL_GRACE_S
from bridge_fmb import (
    FastModbusEventPortManager, FMB_INSURANCE_POLL_S,
    FMB_BALANCING_THRESHOLD_S, FMB_MAX_POLL_TIME_S,
)
from bridge_mqtt import DeviceLiveCache, MQTTPublisher


# ── Base device poller ─────────────────────────────────────────────────────────
class DevicePoller:
    def __init__(self, cfg: dict, pub: MQTTPublisher):
        self.cfg       = cfg
        self.pub       = pub
        self.device_id = cfg["id"]
        self.port_path = cfg.get("port", "/dev/COM1")
        self.baudrate  = int(cfg.get("baudrate", 115200))
        self.address   = int(cfg.get("address", 1))
        self._stop     = threading.Event()
        self._meta_ok  = False
        self.log       = logging.getLogger(f"dev.{self.device_id}")
        # Availability / error back-off (wb-mqtt-serial style). A device that
        # stops answering must not keep hammering the shared half-duplex RS-485
        # bus and starving healthy devices, so failed reads back off
        # exponentially up to ``backoff_max_s`` and the device is flagged
        # offline (device-level meta/error="r") after ``offline_after_fails``.
        self._fail_threshold = max(1, int(cfg.get("offline_after_fails", 3)))
        self._backoff_base_s = float(cfg.get("backoff_base_s", 2.0))
        self._backoff_max_s  = float(cfg.get("backoff_max_s", 30.0))
        self._fail_count     = 0
        self._classic_ok_count = 0
        self._online         = True
        self._backoff_until  = 0.0
        # Последняя MQTT-запись по каналу: name → (value, monotonic ts).
        # Защищает свежий writeback от затирания устаревшим поллом (A2).
        self._wb_recent: dict[str, tuple[str, float]] = {}
        self._wb_offline_log_t = 0.0
        # When True, hot-path classic intervals are floored to FMB_INSURANCE_POLL_S
        # (wb-mqtt-serial DEFAULT_SPORADIC_ONLY_READ_RATE_LIMIT) while events
        # deliver changes; PortCycleScheduler POLLING task still calls poll_io.
        self._fmb_io_covered = False

    def set_fmb_io_covered(self, covered: bool) -> None:
        """Arm/disarm ≥500 ms classic insurance while Fast Modbus events are live."""
        covered = bool(covered)
        if covered == self._fmb_io_covered:
            return
        self._fmb_io_covered = covered
        # MR / DTV expose interval attrs; CE has none — no-op.
        for attr in ("_poll_do_di_s", "_poll_ai_ao_s",
                     "_poll_sensors_s", "_poll_presence_s"):
            if not hasattr(self, attr):
                continue
            saved = f"{attr}_pre_fmb"
            if covered:
                if not hasattr(self, saved):
                    setattr(self, saved, float(getattr(self, attr)))
                setattr(self, attr, max(float(getattr(self, attr)), FMB_INSURANCE_POLL_S))
            elif hasattr(self, saved):
                setattr(self, attr, float(getattr(self, saved)))
                delattr(self, saved)

    def fmb_covers_io(self) -> bool:
        return self._fmb_io_covered

    def get_port(self) -> ModbusSerial:
        return bridge_serial.get_port(self.port_path, self.baudrate)

    # --- Writeback (очередь + worker, вне MQTT callback) ----------------------

    def _wb_submit(self, name: str, job) -> None:
        key = f"{self.port_path}:{self.baudrate}"
        bridge_serial.WritebackWorker.for_port(key).submit((self.device_id, name), job)

    def _wb_offline_skip(self, name: str) -> bool:
        """A3: устройство offline (meta/error=r) — не долбить шину записями."""
        if self._online:
            return False
        now = time.monotonic()
        if now - self._wb_offline_log_t >= 10.0:
            self._wb_offline_log_t = now
            self.log.warning("writeback %s skipped: device offline", name)
        self.pub.pub_error(self.device_id, name, "w")
        return True

    def _wb_write_retry(self, write_fn) -> None:
        """D4 аудита: одна повторная попытка записи после короткой паузы
        сглаживает единичную коллизию шины (wb-mqtt-serial ретраит transient
        ошибки записи в фоне до MaxWriteFailTime)."""
        try:
            write_fn()
        except Exception:
            time.sleep(0.1)
            write_fn()

    def _wb_done(self, name: str, value: str) -> None:
        """A2: echo сразу после успешной записи + мгновенный flush кэша.

        force: echo обязан уйти в MQTT даже при неизменном значении —
        HardPy подтверждает запись именно по нему.
        """
        self._wb_recent[name] = (value, time.monotonic())
        self.pub.pub_control(self.device_id, name, value, force=True)
        self.pub.pub_error(self.device_id, name, "")
        DeviceLiveCache.flush_file(self.device_id)

    def _wb_publish_poll(self, name: str, value: str, t_read: float) -> None:
        """Публикация из полла, не затирающая свежий writeback (A2).

        Если по каналу была запись, снимок полла начат до неё (или запись
        моложе WRITEBACK_POLL_GRACE_S) и значения расходятся — снимок для
        этого канала пропускается: это гонка read→write→publish, а не
        реальное состояние. Совпавшее значение снимает защиту.
        """
        wb = self._wb_recent.get(name)
        if wb is not None:
            wb_val, wb_ts = wb
            if value != wb_val and (
                    wb_ts >= t_read
                    or time.monotonic() - wb_ts < WRITEBACK_POLL_GRACE_S):
                return
            self._wb_recent.pop(name, None)
        self.pub.pub_control(self.device_id, name, value)

    def publish_device_meta(self, name: str, driver: str = "modbus-rtu") -> None:
        if self._meta_ok:
            return
        self.pub.pub_meta(self.device_id, "name", name)
        self.pub.pub_meta(self.device_id, "driver", driver)
        self._meta_ok = True

    # --- Availability state machine ------------------------------------------

    def mark_ok(self) -> None:
        """A read succeeded — device is alive again."""
        self._fail_count = 0
        self._backoff_until = 0.0
        self._classic_ok_count = getattr(self, "_classic_ok_count", 0) + 1
        DeviceLiveCache.set_online(self.device_id, True)
        if not self._online:
            self._online = True
            self.log.info("device back online")
            self.pub.device_online(self.device_id, True)

    def mark_fail(self) -> None:
        """A read failed — count it and (once past threshold) go offline + back off."""
        self._fail_count += 1
        if self._fail_count >= self._fail_threshold:
            over = self._fail_count - self._fail_threshold
            delay = min(self._backoff_base_s * (2 ** over), self._backoff_max_s)
            self._backoff_until = time.monotonic() + delay
            DeviceLiveCache.set_online(self.device_id, False)
            if self._online:
                self._online = False
                self.log.warning("device offline after %d failed reads — "
                                  "backing off polling", self._fail_count)
                self.pub.device_online(self.device_id, False)

    def classic_ready_for_fmb(self, min_ok: int = 2) -> bool:
        """True after a few successful classic reads (safe to send 0x18)."""
        return self._online and getattr(self, "_classic_ok_count", 0) >= min_ok

    def in_backoff(self) -> bool:
        return time.monotonic() < self._backoff_until

    def _io(self, fn, *args):
        """Run a Modbus read and feed the availability state machine."""
        try:
            res = fn(*args)
            self.mark_ok()
            return res
        except Exception:
            self.mark_fail()
            raise

    # Read wrappers that drive availability (writes do not affect device online state).
    def read_coils(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_coils, addr, start, count)

    def read_discrete_inputs(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_discrete_inputs, addr, start, count)

    def read_holding_registers(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_holding_registers, addr, start, count)

    def read_input_registers(self, addr: int, start: int, count: int) -> list[int]:
        return self._io(self.get_port().read_input_registers, addr, start, count)

    def write_register(self, addr: int, reg: int, value: int) -> None:
        self.get_port().write_register(addr, reg, value)

    def stop(self) -> None:
        self._stop.set()

    def setup(self) -> None:
        """Однократная инициализация (вызывается из потока порта)."""

    def poll_io(self) -> None:
        """Один проход основных измерений (в общем цикле порта)."""

    def poll_slow_if_due(self, now: float) -> None:
        """Редкий опрос (диагностика, счётчики энергии) по своим интервалам."""

    # --- Fast Modbus events (optional per device type) ------------------------

    def fmb_event_ranges(self) -> list[tuple[int, int, int]]:
        """Event ranges [(evt_type, start_reg, count)] for configure_events.
        [] (the default) = the device type has no Fast Modbus event support."""
        return []

    def fmb_dispatch(self, evt_type: int, reg: int, val: int) -> None:
        """Publish one Fast Modbus event (called from the FMB manager thread).
        Must validate (evt_type, reg) against the device's own map and never
        raise — an unknown event is ignored, not an error."""

    def run(self) -> None:
        raise NotImplementedError


# ── Port cycle (wb-mqtt-serial TSerialClientRegisterAndEventsReader) ─────────
class PortCycleScheduler:
    """One thread per port:baud — interleaved EVENTS + POLLING (TimeBalancer).

    Mirrors OpenPortCycle:
      * EVENTS (High): event_burst ≤100 ms, reschedule @ ReadEventsPeriod
      * POLLING (Low): classic poll_io / poll_slow within ≤100 ms slice
      * Wait until next deadline, capped at MAX_POLL_TIME (MQTT writeback)
      * BALANCING_THRESHOLD: if event time accumulates ≥500 ms → Force poll
    """

    def __init__(self, port_path: str, baudrate: int,
                 pollers: list[DevicePoller],
                 fmb: FastModbusEventPortManager | None = None):
        self._port_path = port_path
        self._baudrate = baudrate
        self._pollers = pollers
        self._fmb = fmb
        self._stop = threading.Event()
        self._poll_idx = 0
        tag = port_path.replace("/dev/", "")
        self._log = logging.getLogger(f"port.{tag}")

    def stop(self) -> None:
        self._stop.set()
        if self._fmb is not None:
            self._fmb.stop()
        for p in self._pollers:
            p.stop()

    def _classic_slice(self, budget_s: float, read_at_least_one: bool) -> bool:
        """RegisterPoller.OpenPortCycle — one time-boxed classic pass."""
        t0 = time.monotonic()
        n = len(self._pollers)
        if n == 0:
            return False
        did_bus = False
        for i in range(n):
            if self._stop.is_set():
                break
            elapsed = time.monotonic() - t0
            if elapsed >= budget_s and (did_bus or not read_at_least_one):
                break
            p = self._pollers[(self._poll_idx + i) % n]
            if p.in_backoff():
                continue
            try:
                before = time.monotonic()
                p.poll_io()
                if time.monotonic() - before >= 0.005:
                    did_bus = True
            except Exception as e:
                self._log.debug("poll_io %s: %s", p.device_id, e)
        self._poll_idx = (self._poll_idx + 1) % n
        # Slow channels every slice (diag/uptime) — cheap when not due.
        for p in self._pollers:
            try:
                p.poll_slow_if_due(t0)
            except Exception as e:
                self._log.debug("poll_slow %s: %s", p.device_id, e)
        return did_bus

    def run(self) -> None:
        time.sleep(0.5)
        for p in self._pollers:
            if self._stop.is_set():
                return
            try:
                p.setup()
            except Exception as e:
                self._log.error("setup %s: %s", p.device_id, e)

        # Classic warmup before any FC46 0x18 — a silent/hung slave must not
        # get configure_events (observed CE COM2 wedge after early 0x18).
        if not self._stop.is_set():
            for p in self._pollers:
                if self._stop.is_set():
                    break
                try:
                    if not p.in_backoff():
                        p.poll_io()
                except Exception as e:
                    self._log.debug("warmup poll %s: %s", p.device_id, e)
            time.sleep(0.3)

        if self._fmb is not None and self._fmb.has_devices():
            if not self._stop.is_set():
                # Ready devices now; others via retry_unconfigured / only_ready.
                self._fmb.configure_all(only_ready=True)

        has_fmb = self._fmb is not None and self._fmb.has_configured()
        self._log.info(
            "wb-style port cycle on %s, %d device(s), fmb=%s",
            self._port_path, len(self._pollers),
            "on" if has_fmb else "off")

        now = time.monotonic()
        next_events = now
        next_poll = now
        next_fmb_retry = now + 10.0
        high_time_accum = 0.0
        last_too_small = False
        silent_polls = 0
        cyc_n = 0
        cyc_busy_max = 0.0
        cyc_busy_sum = 0.0
        stats_t = now

        while not self._stop.is_set():
            now = time.monotonic()
            has_fmb = self._fmb is not None and self._fmb.has_configured()
            # Offline at first configure_all → quiet retry until device answers.
            if (self._fmb is not None and self._fmb.has_devices()
                    and not has_fmb and now >= next_fmb_retry):
                try:
                    has_fmb = self._fmb.retry_unconfigured()
                except Exception as e:
                    self._log.debug("FMB configure retry: %s", e)
                next_fmb_retry = now + (5.0 if has_fmb else 15.0)
                if has_fmb:
                    self._log.info(
                        "wb-style port cycle on %s: fmb became on",
                        self._port_path)

            # GetDeadline + cap at MAX_POLL_TIME (responsive MQTT writeback).
            if has_fmb:
                deadline = min(next_events, next_poll)
            else:
                deadline = next_poll
            wait = min(max(0.0, deadline - now), FMB_MAX_POLL_TIME_S)
            if last_too_small:
                # WB: idle counted as High so balancing can Force a poll.
                high_time_accum += wait
            if self._stop.wait(wait):
                break

            now = time.monotonic()
            force_poll = high_time_accum >= FMB_BALANCING_THRESHOLD_S
            do_events = (has_fmb and now >= next_events and not force_poll)

            if do_events:
                assert self._fmb is not None
                t0 = time.monotonic()
                try:
                    self._fmb.reconfigure_pending()
                    burst_silent = self._fmb.event_burst()
                except Exception as e:
                    self._log.debug("events: %s", e)
                    burst_silent = 1
                spent = time.monotonic() - t0
                high_time_accum += spent
                next_events = t0 + self._fmb.event_period_s
                last_too_small = False
                if burst_silent:
                    silent_polls += burst_silent
                    if silent_polls >= 10:
                        self._log.warning(
                            "FMB events: no responses — classic at full rate")
                        self._fmb.set_insurance(False)
                        # Treat as no FMB until answers return.
                        next_events = time.monotonic() + 2.0
                else:
                    if silent_polls >= 10:
                        self._fmb.set_insurance(True)
                    silent_polls = 0
            else:
                # POLLING task (Low) — Force if balancing threshold hit or no FMB.
                read_at_least_one = force_poll or not has_fmb
                t0 = time.monotonic()
                did = self._classic_slice(FMB_MAX_POLL_TIME_S, read_at_least_one)
                spent = time.monotonic() - t0
                if force_poll:
                    high_time_accum = 0.0
                if spent >= 0.005:
                    cyc_n += 1
                    cyc_busy_sum += spent
                    cyc_busy_max = max(cyc_busy_max, spent)
                if not did or spent < 0.005:
                    last_too_small = True
                    next_poll = now + 0.05
                else:
                    last_too_small = False
                    next_poll = now
                # Keep EVENTS scheduled when FMB live (WB Contains check).
                if has_fmb and next_events < now:
                    next_events = now

            if time.monotonic() - stats_t >= 60 and cyc_n:
                self._log.info(
                    "poll cycles: n=%d avg=%.0f ms max=%.0f ms fmb=%s",
                    cyc_n, cyc_busy_sum / cyc_n * 1000, cyc_busy_max * 1000,
                    "on" if has_fmb else "off")
                cyc_n = 0
                cyc_busy_max = cyc_busy_sum = 0.0
                stats_t = time.monotonic()


# Backward-compatible alias (tests / imports).
PortPollScheduler = PortCycleScheduler

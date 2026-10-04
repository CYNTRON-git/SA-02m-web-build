"""SPODES poller (`type: spodes`) for a Mercury meter.

HDLC on its own UART, HDLC through a transparent gateway, or an
IEC 62056-47 wrapper socket. Never
ModbusSerial and never Fast Modbus. A reader without keys stays on the
public client. Load disconnect is an ACTION only for a configurator;
any other role answers the MQTT write and does not touch the wire.

The protocol lives in sa02m_spodes (installed to /opt/sa02m-spodes).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta

import bridge_bus
from bridge_device import DevicePoller
from bridge_mqtt import LIVE_CACHE_DIR, DeviceLiveCache


def _import_spodes():
    """The package is not on the bridge's PYTHONPATH. Installed copy first."""
    try:
        from sa02m_spodes import mercury, obis_spodes, profiles
        from sa02m_spodes.session import ClientSession
        return mercury, obis_spodes, profiles, ClientSession
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.environ.get("SA02M_SPODES_DIR", "/opt/sa02m-spodes"),
        os.path.join(os.path.dirname(here), "sa02m-spodes"),
    ):
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    from sa02m_spodes import mercury, obis_spodes, profiles
    from sa02m_spodes.session import ClientSession
    return mercury, obis_spodes, profiles, ClientSession


# Bound on the first SpodesPoller, not at import. A board without the
# package (no Mercury meter) must still start the Modbus pollers.
mercury = obis = profiles = ClientSession = None


def _bind_spodes() -> None:
    global mercury, obis, profiles, ClientSession
    if mercury is None:
        mercury, obis, profiles, ClientSession = _import_spodes()

# One TCP client per gateway port. Two meters on that port share it; a
# second socket would hear the first meter's answer (the gateway copies
# every received byte to every client).
_GATEWAY_LINKS: dict = {}


def hold_gateway(host: str, port: int) -> bool:
    """Close the shared transparent socket for this host:port, if any.

    True when a link was open. Does not touch other gateways or a COM port.
    """
    key = (str(host), int(port))
    link = _GATEWAY_LINKS.pop(key, None)
    if link is None:
        return False
    try:
        link.close()
    except Exception:
        pass
    return True


def _shared_gateway_link(host: str, port: int, timeout_s: float):
    import bridge_rtu_tcp
    import bridge_serial
    key = (host, int(port))
    if bridge_rtu_tcp.endpoint_held(host, port):
        link = _GATEWAY_LINKS.pop(key, None)
        if link is not None:
            try:
                link.close()
            except Exception:
                pass
        raise bridge_serial.ScanHold()
    from sa02m_spodes.link import TransparentLink
    link = _GATEWAY_LINKS.get(key)
    if link is not None and not link.dead:
        return link
    if link is not None:
        link.close()
    link = TransparentLink(host, int(port), timeout_s)
    _GATEWAY_LINKS[key] = link
    return link

_UNITS = {
    "voltage_a": "V", "voltage_b": "V", "voltage_c": "V",
    "voltage_ab": "V", "voltage_bc": "V", "voltage_ca": "V",
    "current_a": "A", "current_b": "A", "current_c": "A",
    "power_a": "W", "power_b": "W", "power_c": "W", "power_total": "W",
    "reactive_a": "var", "reactive_b": "var", "reactive_c": "var",
    "reactive_total": "var",
    "apparent_a": "VA", "apparent_b": "VA", "apparent_c": "VA",
    "apparent_total": "VA",
    "frequency": "Hz",
    "energy_active_import": "Wh", "energy_active_export": "Wh",
    "energy_reactive_import": "varh", "energy_reactive_export": "varh",
    "energy_apparent": "VAh",
}


class SpodesPoller(DevicePoller):
    """One Mercury meter. ``get_port`` raises: this is not a Modbus slave."""

    def __init__(self, cfg: dict, pub):
        _bind_spodes()
        super().__init__(cfg, pub)
        # Role and keys are taken from the caller's dict, then sealed so a
        # log of self.cfg cannot print them. The caller's dict is not mutated.
        self.role = mercury.effective_role(cfg)
        self._security = mercury.security_for(cfg)
        self._password = mercury.password_for(cfg)
        self._sap = mercury.client_sap_for(cfg)
        self._physical = int(cfg.get("hdlc_address", cfg.get("address", 1)))
        self._logical = int(cfg.get("logical_device", 1))
        self.cfg = mercury.seal_cfg(cfg)
        self._poll_power_s = float(cfg.get("poll_power_s", 5))
        self._poll_energy_s = float(cfg.get("poll_energy_s", 60))
        # 0 disables the slow profile/journal read.
        self._poll_profile_s = float(cfg.get("poll_profile_s", 900))
        self._t_power = 0.0
        self._t_energy = 0.0
        self._t_profile = 0.0
        self._scalers: dict = {}
        self._session = None
        self._transport = None
        self.action_calls = 0

    def get_port(self):
        raise RuntimeError("spodes is not Modbus")

    def fmb_event_ranges(self):
        return []

    def setup(self) -> None:
        name = self.cfg.get("name") or (
            "Меркурий (%s addr=%s)" % (
                (self.port_path or self.bus.label), self._physical))
        if isinstance(name, mercury.Secret):
            name = "Меркурий"
        self.publish_device_meta(str(name), "spodes")
        self.pub.pub_control(self.device_id, "association", self.role)
        for cname, unit in _UNITS.items():
            readonly = "0" if cname == "load_disconnect" else "1"
            self.pub.pub_control_meta(self.device_id, cname, "readonly", readonly)
            self.pub.pub_control_units(self.device_id, cname, unit)
        self.pub.pub_control_meta(self.device_id, "load_disconnect", "readonly",
                                  "0" if self.role == "configurator" else "1")
        self.pub.subscribe_writeback(
            self.device_id, "load_disconnect", self._on_load_mqtt)

    def poll_io(self) -> None:
        now = time.monotonic()
        if now - self._t_power < self._poll_power_s:
            return
        self._t_power = now
        try:
            live = mercury.read_power(self._open(), self._scalers)
            if not live:
                raise IOError("no live registers")
            for cname, (mag, unit) in live.items():
                self.pub.pub_control(
                    self.device_id, cname, mercury.format_magnitude(mag))
                if unit:
                    self.pub.pub_control_units(self.device_id, cname, unit)
            self.mark_ok()
            DeviceLiveCache.flush_file(self.device_id)
        except Exception as e:
            self._drop()
            self.mark_fail()
            self.log.warning("power poll: %s", type(e).__name__)

    def poll_slow_if_due(self, now: float) -> None:
        if now - self._t_energy >= self._poll_energy_s:
            self._t_energy = now
            self._poll_energy()
        if self._poll_profile_s > 0 and now - self._t_profile >= self._poll_profile_s:
            self._t_profile = now
            self._poll_profiles()

    def _poll_energy(self) -> None:
        try:
            energy = mercury.read_energy(self._open(), self._scalers)
            for cname, (mag, unit) in energy.items():
                self.pub.pub_control(
                    self.device_id, cname, mercury.format_magnitude(mag))
                if unit:
                    self.pub.pub_control_units(self.device_id, cname, unit)
            if energy:
                self.mark_ok()
                DeviceLiveCache.flush_file(self.device_id)
        except Exception as e:
            self._drop()
            self.mark_fail()
            self.log.warning("energy poll: %s", type(e).__name__)

    def _poll_profiles(self) -> None:
        """Slow path. The stack caps each exchange at PROFILE_BUDGET_S."""
        end = datetime.now().replace(microsecond=0)
        start = end - timedelta(hours=1)
        try:
            session = self._open()
            load = profiles.read_load_profile(session, start, end)
            events = profiles.read_event_log(session, start, end)
        except Exception as e:
            self.log.warning("profile poll: %s", type(e).__name__)
            return
        self._write_rows("profile", load)
        self._write_rows("events", events)

    def _write_rows(self, kind: str, rows: list) -> None:
        payload = {
            "device": self.device_id,
            "kind": kind,
            "rows": [
                {
                    "timestamp": (
                        row["timestamp"].isoformat(sep=" ")
                        if row.get("timestamp") else None
                    ),
                    "values": row.get("values") or [],
                }
                for row in rows
            ],
            "ts": time.time(),
        }
        try:
            LIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = LIVE_CACHE_DIR / ("%s.%s.json" % (self.device_id, kind))
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            self.log.debug("profile cache %s: %s", kind, e)

    def _open(self):
        if self._session is not None and getattr(self._session, "associated", False):
            return self._session
        if self._transport is None:
            self._transport = self._connect()
        mode = "wrapper" if self.bus.transport == bridge_bus.TRANSPORT_WRAPPER else "hdlc"
        self._session = ClientSession(
            self._transport,
            client_sap=self._sap,
            logical=self._logical,
            physical=self._physical,
            timeout_s=5.0,
            ciphered=self._security is not None,
            password=self._password,
            security=self._security,
            mode=mode,
        )
        self._session.associate()
        return self._session

    def _drop(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.release()
            except Exception:
                pass

    def release_line(self) -> None:
        """Drop the session and the UART or gateway socket a scan must own."""
        self._drop()
        link = self._transport
        self._transport = None
        if link is not None and hasattr(link, "close"):
            try:
                link.close()
            except Exception:
                pass

    def _connect(self):
        from sa02m_spodes.link import HdlcLink, WrapperLink
        if self.bus.transport == bridge_bus.TRANSPORT_WRAPPER:
            return WrapperLink(self.bus.host, self.bus.tcp_port, self.bus.timeout_s)
        if self.bus.transport == bridge_bus.TRANSPORT_TRANSPARENT:
            return _shared_gateway_link(
                self.bus.host, self.bus.tcp_port, self.bus.timeout_s)
        return HdlcLink(self.port_path, self.baudrate or 9600)

    def _on_load_mqtt(self, client, userdata, msg) -> None:
        if getattr(msg, "retain", False):
            return
        try:
            payload = msg.payload.decode().strip()
        except Exception:
            return
        if self.role != "configurator":
            self.load_command(payload)
            return
        self._wb_submit("load_disconnect", lambda: self.load_command(payload))

    def load_command(self, payload: str) -> None:
        """MQTT /on for load_disconnect. Non-configurator: no wire bytes."""
        if self.role != "configurator":
            self.pub.pub_error(self.device_id, "load_disconnect", "w")
            return
        self._apply_load(payload)

    def _apply_load(self, payload: str) -> None:
        method = (obis.DISCONNECT_METHOD if str(payload).strip() in ("1", "on")
                  else obis.RECONNECT_METHOD)
        try:
            session = self._open()
            session.action(
                obis.CLASS_DISCONNECT, obis.obis_bytes(obis.DISCONNECT_OBIS), method)
            self.action_calls += 1
            state = session.get(
                obis.CLASS_DISCONNECT, obis.obis_bytes(obis.DISCONNECT_OBIS), 2)
            from sa02m_spodes.axdr import as_int
            self.pub.pub_control(self.device_id, "load_state", str(as_int(state)))
            self.pub.pub_error(self.device_id, "load_disconnect", "")
            self._wb_done("load_disconnect", "1" if method == obis.DISCONNECT_METHOD else "0")
        except Exception as e:
            self.pub.pub_error(self.device_id, "load_disconnect", "w")
            self.log.warning("load action: %s", type(e).__name__)

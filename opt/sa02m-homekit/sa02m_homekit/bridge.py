"""The only module that imports HAP-python (`pyhap`).

Four hooks, each a SUBCLASS or an override of a documented extension point —
the library is never patched (Phase 0 G5, docs/decisions/homekit-home-connect.md):

* `SafeEncoder` (AccessoryEncoder, the driver's `encoder=` parameter): the
  pairing store also keeps the setup code and setup id. HAP-python keeps
  neither across restarts, so a restart would otherwise print a NEW code
  while iOS still holds the old QR.
* `SafeDriver.persist` (AccessoryDriver): tmp `.hk-*.tmp` in the same dir,
  fsync(file), rename, fsync(dir). The library's own persist renames
  without fsync — a power cut then leaves a torn pairing file.
* `SafeBridge.setup_message` (Bridge): a no-op. The library prints the
  setup code and a terminal QR to stdout — the persistent journal.
* The pair-setup lockout: `SafeDriver` swaps in `SafeHAPServer` →
  `SafeHAPProtocol` → `LockoutHandler`, which counts failed SRP proofs and
  answers kTLVError_MaxTries after PAIR_SETUP_MAX_FAILS until restart (the
  HAP spec requirement the library does not implement).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pyhap import tlv
from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import (
    CATEGORY_BRIDGE,
    CATEGORY_FAN,
    CATEGORY_FAUCET,
    CATEGORY_LIGHTBULB,
    CATEGORY_OUTLET,
    CATEGORY_PROGRAMMABLE_SWITCH,
    CATEGORY_SENSOR,
    CATEGORY_SWITCH,
    CATEGORY_THERMOSTAT,
)
from pyhap.encoder import AccessoryEncoder
from pyhap.hap_handler import HAP_TLV_STATES, HAP_TLV_TAGS, HAPServerHandler
from pyhap.hap_protocol import HAPServerProtocol
from pyhap.hap_server import HAPServer

from . import constants as C
from .fsutil import atomic_write
from .netif import PortInUse
from .projection import RULE_MOMENTARY, AccessorySpec, CharBinding

log = logging.getLogger("sa02m_homekit.bridge")

# kTLVError_MaxTries (HAP spec, TLV error codes). Not defined by the library.
TLV_ERROR_MAX_TRIES = b"\x05"

_CATEGORY_BY_SERVICE = {
    "Lightbulb": CATEGORY_LIGHTBULB,
    "Outlet": CATEGORY_OUTLET,
    "Switch": CATEGORY_SWITCH,
    "Fanv2": CATEGORY_FAN,
    "Valve": CATEGORY_FAUCET,
    "StatelessProgrammableSwitch": CATEGORY_PROGRAMMABLE_SWITCH,
    "Thermostat": CATEGORY_THERMOSTAT,
}

# Characteristic property overrides (docs/contracts/homekit-bridge.md §Таблица).
_CHAR_PROPERTIES = {
    "CurrentTemperature": {"minValue": -100, "maxValue": 200, "minStep": 0.1},
}
# Characteristics a service does not carry by default (pyhap services.json
# "OptionalCharacteristics") that a mapping row binds: preloaded explicitly.
_OPTIONAL_CHARS = frozenset(("Brightness", "CarbonDioxideLevel"))
# HAP value → ValidValues name, for the enum characteristics a row restricts
# (`CharBinding.valid`): pyhap takes the restriction as {name: value}.
_VALID_NAMES = {
    "ProgrammableSwitchEvent": {0: "SinglePress", 1: "DoublePress", 2: "LongPress"},
    "TargetHeatingCoolingState": {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto"},
}

WriteCallback = Callable[[str, CharBinding, Any], None]


# ── Pairing store ──────────────────────────────────────────────────────────

class SafeEncoder(AccessoryEncoder):
    """The library's encoder plus `pincode` and `setup_id`."""

    @staticmethod
    def persist(fp, state) -> None:  # type: ignore[override]
        buf = io.StringIO()
        AccessoryEncoder.persist(buf, state)
        data = json.loads(buf.getvalue())
        pincode = state.pincode
        data["pincode"] = pincode.decode("ascii") if isinstance(pincode, bytes) else str(pincode)
        data["setup_id"] = state.setup_id
        json.dump(data, fp)

    @staticmethod
    def load_into(fp, state) -> None:  # type: ignore[override]
        data = json.load(fp)
        AccessoryEncoder.load_into(io.StringIO(json.dumps(data)), state)
        state.pincode = str(data["pincode"]).encode("ascii")
        state.setup_id = str(data["setup_id"])


# ── Pair-setup lockout ─────────────────────────────────────────────────────

class LockoutHandler(HAPServerHandler):
    def handle_pairing(self) -> None:
        driver = self.accessory_handler
        if not self.state.paired and getattr(driver, "pair_setup_locked", False):
            self._send_tlv_pairing_response(
                tlv.encode(
                    HAP_TLV_TAGS.SEQUENCE_NUM, HAP_TLV_STATES.M2,
                    HAP_TLV_TAGS.ERROR_CODE, TLV_ERROR_MAX_TRIES,
                )
            )
            return
        super().handle_pairing()

    def _pairing_two(self, tlv_objects) -> None:
        # Only the pair-setup SRP proof step (M3 → M4) counts. Pair-verify and
        # the pairings endpoint answer M4/M2 authentication errors too — a
        # stale controller retrying pair-verify must not lock pair-setup.
        self._in_srp_proof = True
        try:
            super()._pairing_two(tlv_objects)
        finally:
            self._in_srp_proof = False

    def _send_authentication_error_tlv_response(self, sequence: bytes) -> None:
        # An M4 authentication error inside _pairing_two = the SRP proof did
        # not verify: a wrong setup code. (M6 is a decryption failure after a
        # correct proof, not a guess.)
        if sequence == HAP_TLV_STATES.M4 and getattr(self, "_in_srp_proof", False):
            note = getattr(self.accessory_handler, "note_pair_setup_failure", None)
            if note is not None:
                note()
        super()._send_authentication_error_tlv_response(sequence)


class SafeHAPProtocol(HAPServerProtocol):
    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        self.handler = LockoutHandler(self.accessory_driver, self.peername)


class SafeHAPServer(HAPServer):
    async def async_start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.server = await loop.create_server(
            lambda: SafeHAPProtocol(loop, self.connections, self.accessory_handler),
            self._addr_port[0],
            self._addr_port[1],
        )
        self.async_cleanup_connections()


# ── Driver ─────────────────────────────────────────────────────────────────

class SafeDriver(AccessoryDriver):
    def __init__(
        self,
        *,
        address: str,
        port: int,
        persist_file: str,
        pincode: bytes,
        on_pairing_changed: Optional[Callable[[], None]] = None,
        max_pair_setup_fails: int = C.PAIR_SETUP_MAX_FAILS,
        pair_setup_failures: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            address=address,
            port=port,
            persist_file=persist_file,
            pincode=pincode,
            encoder=SafeEncoder(),
            listen_address=address,
            advertised_address=address,
            interface_choice=[address],
            **kwargs,
        )
        self.http_server = SafeHAPServer((address, self.state.port), self)
        # Carried across in-process rebuilds by the runner's owner: the
        # lockout lasts until the DAEMON restarts, not until the next
        # document edit rebuilds the driver.
        self.pair_setup_failures = max(0, int(pair_setup_failures))
        self._max_pair_setup_fails = max_pair_setup_fails
        self._on_pairing_changed = on_pairing_changed
        self._persist_lock = threading.Lock()

    @property
    def pair_setup_locked(self) -> bool:
        return self.pair_setup_failures >= self._max_pair_setup_fails

    def note_pair_setup_failure(self) -> None:
        self.pair_setup_failures += 1
        if self.pair_setup_failures == self._max_pair_setup_fails:
            log.warning("pair-setup locked after %d failed attempts — restart the bridge to retry",
                        self.pair_setup_failures)
        elif self.pair_setup_failures < self._max_pair_setup_fails:
            log.info("pair-setup attempt failed (%d/%d)",
                     self.pair_setup_failures, self._max_pair_setup_fails)
        self._notify_pairing()

    def persist(self) -> None:
        buf = io.StringIO()
        with self._persist_lock:
            self.encoder.persist(buf, self.state)
            atomic_write(self.persist_file, buf.getvalue(), mode=0o600)

    def pair(self, client_username_bytes, client_public, client_permissions) -> bool:
        ok = super().pair(client_username_bytes, client_public, client_permissions)
        self._notify_pairing()
        return ok

    def unpair(self, client_uuid) -> None:
        super().unpair(client_uuid)
        self._notify_pairing()

    def _notify_pairing(self) -> None:
        if self._on_pairing_changed is not None:
            try:
                self._on_pairing_changed()
            except Exception:
                log.exception("pairing-changed callback failed")


# ── Accessories ────────────────────────────────────────────────────────────

class SafeBridge(Bridge):
    category = CATEGORY_BRIDGE

    def setup_message(self) -> None:
        """No-op: the library prints the setup code to stdout (the journal).
        The code reaches only the authenticated web card (setup.json)."""
        return None


class HomeKitAccessory(Accessory):
    """One bridged accessory built from an AccessorySpec."""

    def __init__(self, driver: AccessoryDriver, spec: AccessorySpec, write_cb: WriteCallback,
                 firmware: str) -> None:
        super().__init__(driver, spec.name, aid=spec.aid)
        self.device_id = spec.device_id
        self._available = False
        self.category = _CATEGORY_BY_SERVICE.get(
            spec.services[0].service if spec.services else "", CATEGORY_SENSOR)
        self.set_info_service(
            firmware_revision=firmware,
            manufacturer=C.MANUFACTURER,
            model=spec.model[:64],
            serial_number=spec.device_id[:64],
        )
        # (service index, characteristic name) → (Characteristic, binding)
        self.chars: Dict[Tuple[int, str], Tuple[Any, CharBinding]] = {}
        for index, svc in enumerate(spec.services):
            optional = [b.char for b in svc.bindings if b.char in _OPTIONAL_CHARS]
            service = self.add_preload_service(svc.service, chars=optional or None)
            for binding in svc.bindings:
                kwargs: Dict[str, Any] = {}
                props = dict(_CHAR_PROPERTIES.get(binding.char) or {})
                if binding.char == "TargetTemperature" and len(binding.bounds) >= 3:
                    # The dial spans the setpoint item's own range and step
                    # (HAP's default 10–38 °C would refuse a 5 °C setpoint).
                    props.update({"minValue": binding.bounds[0], "maxValue": binding.bounds[1],
                                  "minStep": binding.bounds[2]})
                if props:
                    kwargs["properties"] = props
                names = _VALID_NAMES.get(binding.char)
                if binding.valid and names:
                    kwargs["valid_values"] = {names[v]: v for v in binding.valid if v in names}
                    current = service.get_characteristic(binding.char)
                    if current.value is not None and current.value not in binding.valid:
                        # Move the default into the restricted set first:
                        # pyhap logs an error for a value the new ValidValues
                        # exclude (TargetHeatingCoolingState 0 → {Heat: 1}).
                        current.set_value(binding.valid[0], should_notify=False)
                if binding.writable:
                    kwargs["setter_callback"] = self._setter(index, binding, write_cb)
                char = service.configure_char(binding.char, **kwargs)
                self.chars[(index, binding.char)] = (char, binding)

    def _setter(self, index: int, binding: CharBinding,
                write_cb: WriteCallback) -> Callable[[Any], None]:
        def setter(value: Any) -> None:
            # Raising here makes HAP-python answer -70402
            # SERVICE_COMMUNICATION_FAILURE — never a fake success.
            try:
                write_cb(self.device_id, binding, value)
            finally:
                if binding.rule == RULE_MOMENTARY and value:
                    # A scene switch is a pulse: back to off after the run
                    # (or after a refused one), never left claiming a state.
                    self._schedule_reset(index, binding.char)
        return setter

    def _schedule_reset(self, index: int, name: str) -> None:
        loop = self.driver.loop
        # Thread-safe whichever thread HAP-python ran the setter on.
        loop.call_soon_threadsafe(loop.call_later, C.SCENE_RESET_S, self._reset, index, name)

    def _reset(self, index: int, name: str) -> None:
        entry = self.chars.get((index, name))
        if entry is not None:
            entry[0].set_value(False)

    @property
    def available(self) -> bool:
        return self._available

    def apply(self, values: Sequence[Tuple[int, str, Any]], available: bool) -> None:
        """Set characteristic values (event loop thread only)."""
        self._available = available
        for index, name, value in values:
            entry = self.chars.get((index, name))
            if entry is None or value is None:
                continue
            char, _binding = entry
            try:
                char.set_value(value)
            except ValueError as exc:
                log.warning("%s: value %r refused for %s (%s)", self.device_id, value, name, exc)

    def fire(self, index: int, name: str, value: Any) -> None:
        """One press event (event loop thread only). ProgrammableSwitchEvent
        is always-null in HAP-python, so every set notifies — two single
        presses in a row are two events."""
        entry = self.chars.get((index, name))
        if entry is None:
            return
        char, _binding = entry
        try:
            char.set_value(value)
        except ValueError as exc:
            log.warning("%s: event %r refused for %s (%s)", self.device_id, value, name, exc)


# ── Runner: one driver on its own event-loop thread ───────────────────────

class BridgeRunner:
    """Owns one SafeDriver. A rebuild is stop() + a new runner on the same
    pairing store — the pairings survive, the accessory hash bumps `c#`."""

    def __init__(
        self,
        *,
        persist_file: str,
        address: str,
        port: int,
        bridge_name: str,
        specs: Sequence[AccessorySpec],
        write_cb: WriteCallback,
        firmware: str,
        new_code: str,
        new_setup_id: str,
        on_pairing_changed: Optional[Callable[[], None]] = None,
        pair_setup_failures: int = 0,
        driver_factory: Callable[..., SafeDriver] = SafeDriver,
    ) -> None:
        self._persist_file = persist_file
        self._address = address
        self._port = port
        self._bridge_name = bridge_name
        self._specs = list(specs)
        self._write_cb = write_cb
        self._firmware = firmware
        self._new_code = new_code
        self._new_setup_id = new_setup_id
        self._on_pairing_changed = on_pairing_changed
        self._driver_factory = driver_factory
        self._initial_failures = pair_setup_failures
        self.driver: Optional[SafeDriver] = None
        self.accessories: Dict[str, HomeKitAccessory] = {}
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._error: Optional[BaseException] = None
        self._running = False

    # lifecycle ----------------------------------------------------------
    def start(self, timeout: float = 60.0) -> None:
        self._thread = threading.Thread(target=self._run, name="hap-driver", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise RuntimeError("HAP driver did not start within %.0f s" % timeout)
        if self._error is not None:
            err = self._error
            self._thread.join(timeout=C.DRIVER_STOP_TIMEOUT_S)
            if isinstance(err, OSError) and getattr(err, "errno", None) in (98, 48):
                raise PortInUse(err.errno, "port %d in use" % self._port) from err
            raise err

    def _build(self) -> SafeDriver:
        fresh = not os.path.exists(self._persist_file)
        driver = self._driver_factory(
            address=self._address,
            port=self._port,
            persist_file=self._persist_file,
            pincode=self._new_code.encode("ascii"),
            on_pairing_changed=self._on_pairing_changed,
            pair_setup_failures=self._initial_failures,
        )
        if fresh:
            driver.state.setup_id = self._new_setup_id
        bridge = SafeBridge(driver, self._bridge_name)
        bridge.set_info_service(
            firmware_revision=self._firmware,
            manufacturer=C.MANUFACTURER,
            model="SA-02m HomeKit bridge",
            serial_number=self._bridge_name,
        )
        for spec in self._specs:
            acc = HomeKitAccessory(driver, spec, self._write_cb, self._firmware)
            bridge.add_accessory(acc)
            self.accessories[spec.device_id] = acc
        # Loads the pairing store (keys, pairings, code) or writes a new one.
        driver.add_accessory(bridge)
        return driver

    def _run(self) -> None:
        loop = None
        try:
            driver = self._build()
            self.driver = driver
            loop = driver.loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(driver.async_start())
        except BaseException as exc:  # bind failure, bad state — reported to start()
            self._error = exc
            self._started.set()
            if self.driver is not None and self.driver.executor is not None:
                self.driver.executor.shutdown(wait=False)
            if loop is not None and not loop.is_closed():
                loop.close()
            return
        self._running = True
        self._started.set()
        try:
            loop.run_forever()
        finally:
            self._running = False
            try:
                # async_stop ends with loop.stop(); zeroconf may still have a
                # broadcast task in flight — cancel and drain it so the loop
                # closes clean instead of destroying pending tasks.
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            except Exception:
                log.exception("event loop close failed")

    def stop(self) -> None:
        driver = self.driver
        thread = self._thread
        if driver is None or thread is None or not thread.is_alive() or not self._running:
            return
        # async_stop unregisters mDNS, closes the HAP server and ends with
        # loop.stop() — the loop thread then exits. Its concurrent future is
        # never resolved (the loop stops first), so the thread is the signal.
        asyncio.run_coroutine_threadsafe(driver.async_stop(), driver.loop)
        thread.join(timeout=C.DRIVER_STOP_TIMEOUT_S)
        if thread.is_alive():
            log.error("HAP driver did not stop within %.0f s", C.DRIVER_STOP_TIMEOUT_S)

    @property
    def running(self) -> bool:
        return self._running

    # state --------------------------------------------------------------
    @property
    def pairings(self) -> int:
        return len(self.driver.state.paired_clients) if self.driver else 0

    @property
    def paired(self) -> bool:
        return bool(self.driver and self.driver.state.paired)

    @property
    def pincode(self) -> str:
        return self.driver.state.pincode.decode("ascii") if self.driver else ""

    @property
    def setup_id(self) -> str:
        return self.driver.state.setup_id if self.driver else ""

    @property
    def pair_setup_locked(self) -> bool:
        return bool(self.driver and self.driver.pair_setup_locked)

    @property
    def pair_setup_failures(self) -> int:
        return self.driver.pair_setup_failures if self.driver else self._initial_failures

    # values -------------------------------------------------------------
    def push(self, device_id: str, values: List[Tuple[int, str, Any]], available: bool) -> None:
        """Queue values for one accessory onto the HAP loop (any thread)."""
        acc = self.accessories.get(device_id)
        if acc is None or self.driver is None or not self._running:
            return
        self.driver.loop.call_soon_threadsafe(acc.apply, values, available)

    def push_event(self, device_id: str, index: int, char: str, value: Any) -> None:
        """Queue one press event onto the HAP loop (any thread)."""
        acc = self.accessories.get(device_id)
        if acc is None or self.driver is None or not self._running:
            return
        self.driver.loop.call_soon_threadsafe(acc.fire, index, char, value)

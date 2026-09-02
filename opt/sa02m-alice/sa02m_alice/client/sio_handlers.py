"""Socket.IO event handlers for Alice gateway requests."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..common import constants as C
from .device_registry import DeviceRegistry

log = logging.getLogger("sa02m_alice.handlers")

PublishFn = Callable[[str, str], None]
EmitFn = Callable[[Dict[str, Any]], None]
UnlinkFn = Callable[[], None]


class SioHandlers:
    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        publish_mqtt: PublishFn,
        emit_response: EmitFn,
        on_unlink: Optional[UnlinkFn] = None,
    ) -> None:
        self.registry = registry
        self._publish = publish_mqtt
        self._emit_response = emit_response
        # Injected the same way as publish/emit: this class must not import
        # main, touch files, or know about threads. Optional so an existing
        # caller (and every test that builds a handler for the device events)
        # keeps working -- a handler without it logs and does nothing.
        self._on_unlink = on_unlink

    def handle(self, event: str, data: Any) -> None:
        # ANY receipt of controller_unlink is authoritative regardless of the
        # payload shape -- so this branch sits ABOVE the dict guard. The
        # gateway sends {"reason":"unlinked"} today and a pre-0.6.0 one sent
        # {}, but the delivery contract (repo `cloud`,
        # docs/contracts/alice-gateway.md, "controller_unlink delivery
        # semantics") makes the payload optional, and the event can only reach
        # us on the verified mTLS session -- authenticity is the channel's,
        # never the payload's. Below the guard, a null payload would be
        # silently dropped and the board would keep claiming it is bound.
        if event == C.EVT_CONTROLLER_UNLINK:
            self._on_controller_unlink()
            return
        if not isinstance(data, dict):
            log.warning("Ignoring non-object payload for %s", event)
            return
        request_id = data.get("request_id")
        if event == C.EVT_DEVICES_LIST:
            self._on_list(request_id)
        elif event == C.EVT_DEVICES_QUERY:
            self._on_query(request_id, data.get("devices") or [])
        elif event == C.EVT_DEVICES_ACTION:
            payload = data.get("payload") or data
            self._on_action(request_id, (payload or {}).get("devices") or [])
        else:
            log.debug("Unhandled event %s", event)

    def _on_controller_unlink(self) -> None:
        if self._on_unlink is None:
            log.warning(
                "Gateway requested controller unlink but no reset callback is wired"
            )
            return
        log.warning(
            "Gateway unlinked this controller -- erasing the local cloud binding "
            "(certificate, key, pending claim, gateway CA)"
        )
        self._on_unlink()

    def _on_list(self, request_id: Optional[str]) -> None:
        devices = self.registry.discovery_devices()
        self._emit_response({"request_id": request_id, "payload": {"devices": devices}})

    def _on_query(self, request_id: Optional[str], devices: List[Any]) -> None:
        ids = [str(d.get("id")) for d in devices if isinstance(d, dict) and d.get("id")]
        result = self.registry.query_devices(ids if ids else None)
        self._emit_response({"request_id": request_id, "payload": {"devices": result}})

    def _on_action(self, request_id: Optional[str], devices: List[Any]) -> None:
        results, publishes = self.registry.apply_actions(devices)
        for topic, payload in publishes:
            try:
                self._publish(topic, payload)
            except Exception as exc:
                log.error("MQTT publish failed %s: %s", topic, exc)
                # Mark matching capability as unreachable
                for dev in results:
                    for cap in dev.get("capabilities") or []:
                        if cap.get("status") == C.STATUS_DONE:
                            cap["status"] = C.STATUS_ERROR
                            cap["error_code"] = C.ERR_DEVICE_UNREACHABLE
        self._emit_response({"request_id": request_id, "payload": {"devices": results}})

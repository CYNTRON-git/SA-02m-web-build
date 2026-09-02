"""Socket.IO event handlers for Alice gateway requests."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..common import constants as C
from .device_registry import DeviceRegistry

log = logging.getLogger("sa02m_alice.handlers")

PublishFn = Callable[[str, str], None]
EmitFn = Callable[[Dict[str, Any]], None]


class SioHandlers:
    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        publish_mqtt: PublishFn,
        emit_response: EmitFn,
        profile: str = C.PROFILE_YANDEX,
    ) -> None:
        self.registry = registry
        self._publish = publish_mqtt
        self._emit_response = emit_response
        self._profile = profile

    def handle(self, event: str, data: Any) -> None:
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
        elif event == C.EVT_CONTROLLER_UNLINK:
            # Yandex-profile only: an unlink is about the mTLS enrollment, and
            # the cloud session has none to unlink — on the cloud profile the
            # event is not even registered (sio_connection), and if one ever
            # arrived it must not touch the Alice cert.
            if self._profile == C.PROFILE_CLOUD:
                log.info("controller_unlink ignored on the cloud profile")
                return
            log.info("Gateway requested controller unlink (local flag clear is UI/API concern)")
        else:
            log.debug("Unhandled event %s", event)

    def _on_list(self, request_id: Optional[str]) -> None:
        devices = self.registry.discovery_devices(profile=self._profile)
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

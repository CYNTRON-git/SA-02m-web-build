# -*- coding: utf-8 -*-
"""MR-02m LED strip (RGBW_WS2812, type 120) register map and MQTT control inventory.

Shared by the flasher daemon (/opt/sa02m-flasher) and the Modbus-MQTT bridge
(/opt/sa02m-modbus-mqtt), which run as different users from different trees —
hence a package of its own at /opt/sa02m-led rather than a copy in each.
Contract: docs/contracts/led-mb2ws.md. Stdlib only.
"""

__all__ = ["led_mb2ws", "led_mb2ws_map", "controls"]

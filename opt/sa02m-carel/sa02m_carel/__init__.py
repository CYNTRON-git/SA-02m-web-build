# -*- coding: utf-8 -*-
"""Carel AHU (c.pCOmini / uAria) register map and MQTT control inventory.

Shared by the flasher daemon (/opt/sa02m-flasher) and the Modbus-MQTT bridge
(/opt/sa02m-modbus-mqtt), which run as different users from different trees —
hence a package of its own at /opt/sa02m-carel rather than a copy in each.
Contract: docs/contracts/carel-ahu.md. Stdlib only.
"""

__all__ = ["carel_ahu", "carel_ahu_map", "controls"]

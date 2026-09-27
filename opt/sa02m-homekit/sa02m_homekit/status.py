"""The three /run files the web card reads (docs/contracts/homekit-bridge.md §Статус).

* `status.json` (0644) — state, counts, listener; NEVER the setup code and
  never a device name (counts only).
* `projection.json` (0640, group www-data) — accessory list and the skipped
  list with device names and reasons (capped at SKIPPED_CAP rows).
* `setup.json` (0640, group www-data) — the setup code, URI and QR matrix;
  exists ONLY while the bridge is running and unpaired. Every other state
  removes it.

Single writer: the daemon's main thread.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from . import __version__
from . import constants as C
from .fsutil import atomic_write, group_gid, remove_quietly

# The status.json key set — compared with the contract by the test suite.
STATUS_KEYS = (
    "state", "ts", "version", "reason", "message", "enabled", "interface",
    "address", "port", "bridge_name", "paired", "pairings", "accessories",
    "skipped", "pair_setup_locked",
)
SETUP_KEYS = ("code", "setup_uri", "setup_id", "qr", "ts")


class StatusWriter:
    def __init__(self, run_dir: Optional[str] = None) -> None:
        base = run_dir or C.RUN_DIR
        self.status_path = os.path.join(base, os.path.basename(C.STATUS_FILE))
        self.setup_path = os.path.join(base, os.path.basename(C.SETUP_FILE))
        self.projection_path = os.path.join(base, os.path.basename(C.PROJECTION_FILE))
        self._gid = group_gid()
        self.last: Dict[str, Any] = {}

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self.status_path) or ".", exist_ok=True)

    def write_status(
        self,
        state: str,
        *,
        reason: str = "",
        message: str = "",
        enabled: bool = False,
        interface: str = "",
        address: str = "",
        port: int = 0,
        bridge_name: str = "",
        paired: Optional[bool] = None,
        pairings: int = 0,
        accessories: int = 0,
        skipped: int = 0,
        pair_setup_locked: bool = False,
    ) -> Dict[str, Any]:
        if state not in C.STATES or state == C.STATE_NOT_INSTALLED:
            raise ValueError("daemon cannot write state %r" % state)
        payload: Dict[str, Any] = {
            "state": state,
            "ts": int(time.time()),
            "version": __version__,
            "reason": reason,
            "message": message,
            "enabled": bool(enabled),
            "interface": interface,
            "address": address,
            "port": int(port),
            "bridge_name": bridge_name,
            "paired": paired,
            "pairings": int(pairings),
            "accessories": int(accessories),
            "skipped": int(skipped),
            "pair_setup_locked": bool(pair_setup_locked),
        }
        self._ensure_dir()
        atomic_write(self.status_path, json.dumps(payload, ensure_ascii=False) + "\n",
                     mode=0o644, durable=False)
        self.last = payload
        if state != C.STATE_RUNNING or paired is not False:
            self.clear_setup()
        return payload

    def write_setup(self, code: str, setup_uri: str, setup_id: str, qr: Optional[list]) -> None:
        """Only the caller knows running ∧ unpaired; it calls this then."""
        payload = {
            "code": code,
            "setup_uri": setup_uri,
            "setup_id": setup_id,
            "qr": qr or [],
            "ts": int(time.time()),
        }
        self._ensure_dir()
        atomic_write(self.setup_path, json.dumps(payload) + "\n", mode=0o640,
                     gid=self._gid, durable=False)

    def clear_setup(self) -> None:
        remove_quietly(self.setup_path)

    def write_projection(self, body: Dict[str, Any]) -> None:
        payload = dict(body)
        payload["ts"] = int(time.time())
        self._ensure_dir()
        atomic_write(self.projection_path, json.dumps(payload, ensure_ascii=False) + "\n",
                     mode=0o640, gid=self._gid, durable=False)

    def clear_projection(self) -> None:
        remove_quietly(self.projection_path)

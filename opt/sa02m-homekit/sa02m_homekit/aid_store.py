"""Persistent accessory-id map: `/var/lib/sa02m-homekit/aids.json`.

iOS binds automations and room placement to the accessory id (aid), so an
aid must survive renames, document edits and restarts, and a DELETED
device's aid is retired, never reissued to another device — a reissued aid
would silently re-point a household's automation at a different relay.

Shape: {"version": 1, "next": N, "aids": {device_id: aid}}. `next` only
grows; allocation takes the first integer ≥ max(next, AID_FIRST) that is not
in AID_SKIP (1 = the bridge, 7 = never used by HAP-python) and not held.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, Iterable, Optional

from . import constants as C
from .fsutil import atomic_write

log = logging.getLogger("sa02m_homekit.aids")
# Values still legible in a damaged map (`"<key>": <int>`). Digits inside a
# device-id key are not values and never raise the floor.
_INT_RE = re.compile(rb'":\s*(\d{1,19})')


class AidStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or C.AIDS_FILE
        self.aids: Dict[str, int] = {}
        self.next_aid = C.AID_FIRST
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.error("aid map %s unreadable (%s) — starting a new map", self.path, exc)
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            # A torn/garbled map: start a fresh one, but never below any
            # number the damaged bytes still show — iOS may know those aids,
            # and reissuing one would re-point an automation.
            floor = max((int(n) for n in _INT_RE.findall(raw)), default=0)
            self.next_aid = max(self.next_aid, floor + 1)
            log.error("aid map %s unreadable (%s) — starting a new map from aid %d",
                      self.path, exc, self.next_aid)
            return
        if not isinstance(data, dict):
            return
        raw = data.get("aids")
        if isinstance(raw, dict):
            for did, aid in raw.items():
                if isinstance(did, str) and isinstance(aid, int) and not isinstance(aid, bool) \
                        and aid >= C.AID_FIRST and aid not in C.AID_SKIP:
                    self.aids[did] = aid
        nxt = data.get("next")
        if isinstance(nxt, int) and not isinstance(nxt, bool):
            self.next_aid = max(self.next_aid, nxt)
        if self.aids:
            self.next_aid = max(self.next_aid, max(self.aids.values()) + 1)

    def get(self, device_id: str) -> Optional[int]:
        return self.aids.get(device_id)

    def allocate(self, device_id: str) -> int:
        """The aid of `device_id`, allocating a never-used one if needed."""
        existing = self.aids.get(device_id)
        if existing is not None:
            return existing
        held = set(self.aids.values())
        aid = max(self.next_aid, C.AID_FIRST)
        while aid in C.AID_SKIP or aid in held:
            aid += 1
        self.aids[device_id] = aid
        self.next_aid = aid + 1
        return aid

    def retire_absent(self, present_ids: Iterable[str], keep: Iterable[str] = ()) -> int:
        """Drop devices no longer in the document; their aids are never
        reissued (`next` does not move back). Returns how many retired.

        Only a device ABSENT from the document is retired — a device merely
        hidden from HomeKit keeps its aid so re-ticking it restores the same
        accessory in the Home app. `keep` names ids that are not catalogue
        rows yet still exist (an unticked or disabled scene, a scene the store
        could not be read for) — they are not retired either."""
        present = set(present_ids) | set(keep)
        gone = [did for did in self.aids if did not in present]
        for did in gone:
            del self.aids[did]
        return len(gone)

    def save(self) -> None:
        body = json.dumps(
            {"version": 1, "next": self.next_aid, "aids": dict(sorted(self.aids.items()))},
            ensure_ascii=False, sort_keys=True,
        )
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        atomic_write(self.path, body + "\n", mode=0o600)

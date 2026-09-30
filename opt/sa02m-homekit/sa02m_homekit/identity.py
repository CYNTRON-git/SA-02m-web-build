"""Per-board accessory identity: machine-id binding and state-file sanity.

A cloned pairing store makes every clone the SAME HomeKit accessory, and the
donor household's iPhones would then control the clone's relays. Layer 1 of
the clone safety (docs/contracts/homekit-bridge.md §Жизненный цикл): on
start, `sha256(/etc/machine-id)` is compared with the recorded binding; a
mismatch or an absent binding deletes the pairing store so a fresh identity
is generated. Layer 2 is the imaging sites' clear-list
(docs/contracts/image-identity-reset.md §2).

Unlike Alice, the HomeKit identity is board-generated, so "regenerate on
first start" is legitimate here.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
from typing import Optional

from . import constants as C
from .fsutil import atomic_write, remove_quietly

log = logging.getLogger("sa02m_homekit.identity")

_CODE_RE = re.compile(C.SETUP_CODE_RE)
_SETUP_ID_RE = re.compile(r"^[0-9A-Z]{4}$")
# Keys HAP-python's encoder needs plus the two our encoder adds.
REQUIRED_STATE_KEYS = (
    "mac", "config_version", "paired_clients", "private_key", "public_key",
    "pincode", "setup_id",
)


def machine_id_digest(path: Optional[str] = None) -> Optional[str]:
    """sha256 hex of the machine-id, or None when there is none to bind to."""
    try:
        with open(path or C.MACHINE_ID_FILE, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def wipe_pairing_store(var_dir: Optional[str] = None) -> None:
    """Remove the pairing store and any torn-write sidecar. The aid map is
    kept: it is not a secret and iOS-side ids stay meaningful for a re-pair."""
    base = var_dir or C.VAR_DIR
    remove_quietly(os.path.join(base, os.path.basename(C.STATE_FILE)))
    for tmp in glob.glob(os.path.join(base, C.TMP_PREFIX + "*.tmp")):
        remove_quietly(tmp)


def check_machine_binding(
    var_dir: Optional[str] = None, machine_id_path: Optional[str] = None
) -> Optional[str]:
    """Enforce the binding. Returns REASON_IDENTITY_REGENERATED when the
    pairing store had to be discarded, else None.

    No machine-id ⇒ nothing to bind to: the store is kept as-is (a board
    without a machine-id is broken in bigger ways; wiping pairings on every
    start would make the bridge unusable instead of safer).
    """
    base = var_dir or C.VAR_DIR
    ident_path = os.path.join(base, os.path.basename(C.IDENTITY_FILE))
    state_path = os.path.join(base, os.path.basename(C.STATE_FILE))
    current = machine_id_digest(machine_id_path)
    if current is None:
        log.warning("no machine-id — HomeKit identity binding not enforced")
        return None
    recorded = None
    try:
        with open(ident_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("machine_id_sha256"), str):
            recorded = data["machine_id_sha256"]
    except (OSError, ValueError):
        recorded = None
    reason = None
    if recorded != current:
        had_state = os.path.exists(state_path)
        wipe_pairing_store(base)
        if had_state:
            log.warning("HomeKit identity regenerated (machine-id changed)")
            reason = C.REASON_IDENTITY_REGENERATED
        os.makedirs(base, exist_ok=True)
        atomic_write(ident_path, json.dumps({"machine_id_sha256": current}) + "\n", mode=0o600)
    return reason


def valid_setup_code(code: object) -> bool:
    return isinstance(code, str) and bool(_CODE_RE.match(code)) and code not in C.FORBIDDEN_SETUP_CODES


def check_state_file(path: Optional[str] = None) -> Optional[str]:
    """A pairing store that cannot be loaded whole is discarded — never a
    crash loop, never a silently stale or partial key set. Returns
    REASON_STATE_CORRUPT when it was discarded, else None (absent is fine:
    a first start generates it)."""
    state_path = path or C.STATE_FILE
    try:
        with open(state_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.error("HomeKit pairing store unreadable (%s) — regenerating identity", exc)
        wipe_pairing_store(os.path.dirname(state_path))
        return C.REASON_STATE_CORRUPT
    ok = isinstance(data, dict) and all(k in data for k in REQUIRED_STATE_KEYS)
    if ok:
        ok = (
            valid_setup_code(data.get("pincode"))
            and isinstance(data.get("setup_id"), str)
            and bool(_SETUP_ID_RE.match(data["setup_id"]))
            and isinstance(data.get("paired_clients"), dict)
            and isinstance(data.get("private_key"), str)
            and len(data["private_key"]) == 64
        )
    if not ok:
        log.error("HomeKit pairing store incomplete — regenerating identity")
        wipe_pairing_store(os.path.dirname(state_path))
        return C.REASON_STATE_CORRUPT
    return None

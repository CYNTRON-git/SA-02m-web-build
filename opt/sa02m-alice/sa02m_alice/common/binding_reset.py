"""The single home of the cloud-binding wipe (gateway unlink + local button).

Scope is the security promise of this feature, and it is deliberately narrow:
a compromised cloud account may erase **this board's cloud binding and nothing
else**. Enumerated, the wipe may touch only

  * ``VAR_DIR``: ``device.crt.pem``, ``device.key.pem``, ``pending_claim.json``,
    ``ca.crt.pem``, and the atomic-write sidecars ``*.tmp`` in that directory;
  * one key in the client INI -- ``unlinked_at``.

Never the bus/room document (``sa02m-alice-devices.conf`` -- a re-bound board
must find the same devices), ``sa02m-alice-server.conf``, the other client
keys (``mqtt_host`` / ``mqtt_port`` / ``log_level`` / ``client_enabled``),
MQTT or network config, MPLC4/CODESYS, RS-485, device accounts,
``/etc/sa02m-cloud/*`` or anything under ``/opt``. There is **no recursive
delete**: ``tmpfiles.d`` owns ``VAR_DIR`` and the directory itself survives.
``tests/test_binding_reset.py`` turns that paragraph into assertions.

``client_enabled`` is left ON on purpose. Stopping the reconnect loop does not
depend on the flag: once this wipe has run, ``client/main.py`` routes the loop
into its no-certificate soft wait instead of dialling -- on EVERY transport,
not only the wss:// one that needs a certificate to authenticate
(``_should_wait_for_cert``). So the loop stops at least as hard as switching
the client off would, and the card keeps its link button, which ``alice.js``
hides whenever the client is off. What the flag being ON does NOT mean: a
board that was never bound still dials a lab ws:// gateway, which is the
pre-existing behaviour and deliberately unchanged.

This module lives in ``common/`` because both ``client/`` and ``config/``
import it; the existing dependency direction is unchanged.
"""

from __future__ import annotations

import glob
import logging
import os
import time
from typing import Any, Dict, List, Optional

from . import constants as C
from .config_store import set_unlinked_at

log = logging.getLogger("sa02m_alice.binding_reset")

SOURCE_GATEWAY = "gateway"
SOURCE_LOCAL = "local"


def binding_files() -> List[str]:
    """The exact clear-list, resolved at call time.

    Read through ``C.*`` on every call rather than frozen at import, so a test
    (or an env override) that retargets the paths retargets the wipe with it.
    """
    return [C.CERT_FILE, C.KEY_FILE, C.PENDING_CLAIM_FILE, C.CA_FILE]


def _sidecars() -> List[str]:
    """Atomic-write leftovers, matched by SHAPE, not by name.

    ``_write_pem`` and ``_save_pending_claim`` write ``<path>.tmp`` and only
    then ``os.replace``; a crash mid-bind strands ``device.key.pem.tmp`` -- the
    same private key under a name one character off any literal list.
    ``*.tmp`` cannot reach ``ca.crt.pem``.
    """
    return sorted(glob.glob(os.path.join(C.VAR_DIR, "*.tmp")))


def reset_cloud_binding(source: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Erase this board's cloud binding. Idempotent; returns what it did.

    ``source`` is ``"gateway"`` (the authoritative ``controller_unlink`` event)
    or ``"local"`` (the device's own unlink button, after the cloud confirmed).
    It is reported, never branched on -- both paths must reach the same state.

    Missing files are a no-op reported as ``absent``, not hidden: that is the
    whole of idempotency, and a dead wipe must surface rather than look
    successful.
    """
    removed: List[str] = []
    absent: List[str] = []
    for path in binding_files() + _sidecars():
        name = os.path.basename(path)
        try:
            os.unlink(path)
            removed.append(name)
        except FileNotFoundError:
            absent.append(name)
        except OSError as exc:
            # Report, never swallow: a binding that could not be erased must
            # not read as erased.
            log.error("cloud unlink: could not remove %s: %s", path, exc)
            raise
    stamp = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now)
    )
    set_unlinked_at(stamp)
    summary = {
        "source": source,
        "removed": removed,
        "absent": absent,
        "unlinked_at": stamp,
    }
    log.warning(
        "Cloud binding erased (source=%s): removed %s; already absent %s",
        source,
        ", ".join(removed) or "nothing",
        ", ".join(absent) or "nothing",
    )
    return summary

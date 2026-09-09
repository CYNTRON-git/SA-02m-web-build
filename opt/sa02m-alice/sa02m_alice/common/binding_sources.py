"""This door's descriptor for the shared binding-reset core.

The behaviour — the counter, the fail-closed direction, the wipe loop, the
`unlink_failed` state and its retry, the durable marker, the reboot restore, the
status writes — lives ONCE, in `binding_core.py` (authoritative source
`opt/sa02m-cloud-agent/binding_core.py`, byte-identical copy beside this file,
kept equal by the `binding-reset-parity` quality row). What lives HERE is only
what is true of the Alice door:

* the erase-list, including the atomic-write sidecars matched by SHAPE;
* threshold 1 — this door receives a VERDICT (`controller_unlink`, delivered
  once on the verified mTLS session), not evidence. The gateway disconnects
  immediately after sending it, so waiting for a second event would mean
  waiting forever. The fleet-cloud door counts to three for the opposite
  reason; the number is the parameter, the counting is shared;
* there is no separate transport unit to stop — the client itself stops dialling
  once the certificate is gone (`client/main.py::_should_wait_for_cert`), so
  `stop_tunnel` is an honest no-op rather than a branch inside the core;
* `revoked` is N/A: the gateway's event does not distinguish an owner revoke
  from a detach and the handler deliberately reads no payload field, so this
  descriptor declares only the two states it may write and the core refuses any
  other by construction;
* the journal cursor is N/A: it exists to count re-readable text in a rotating
  journal exactly once. This door's evidence is a Socket.IO callback delivered
  once by the library. The invariant it buys — an event that arrives again on a
  later reconnect must not double-count — is satisfied by idempotence instead
  (the wipe reports `absent`, the marker write is a set, not an append).

Blast radius, enumerated: the wipe may touch VAR_DIR's `device.crt.pem`,
`device.key.pem`, `pending_claim.json`, `ca.crt.pem` and the `*.tmp` sidecars in
that directory, plus the three marker keys in the client INI. NEVER the device
document (`sa02m-alice-devices.conf` — a re-bound board must find the same
devices), `sa02m-alice-server.conf`, the other client keys (`mqtt_host` /
`mqtt_port` / `log_level` / `client_enabled`), MQTT or network config,
MPLC4/CODESYS, RS-485, device accounts, or anything under `/etc/sa02m-cloud`.
There is no recursive delete: `tmpfiles.d` owns VAR_DIR and the directory
itself survives. `tests/test_binding_reset.py` turns that paragraph into
byte-identity assertions.

`client_enabled` is deliberately left ON. Stopping the reconnect loop does not
depend on the flag: with no certificate `client/main.py` routes the loop into
its soft wait on EVERY transport instead of dialling, so the loop stops at
least as hard as switching the client off would — and the card keeps its
«Привязать» row, which `app/alice.js` hides whenever the client is off. That is
what makes «отвязать → привязать заново» work from the card, with no SSH.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Any, Callable, List, Optional

from . import binding_core
from . import constants as C
from .config_store import clear_unlink_marker, set_unlink_marker, unlink_marker

log = logging.getLogger("sa02m_alice.binding")

# What the durable marker records as `reason` on the LOCAL path — the board's
# own «Отвязать», after the gateway confirmed it. The gateway-driven path
# records the event the gateway actually sent (`C.UNLINK_REFUSAL`), so each
# path says who confirmed the unlink and neither invents a label for the other.
SOURCE_LOCAL = "local"


def binding_files() -> List[str]:
    """The exact clear-list, resolved at call time.

    Read through `C.*` on every call rather than frozen at import, so a test
    (or an env override) that retargets the paths retargets the wipe with it.
    """
    return [C.CERT_FILE, C.KEY_FILE, C.PENDING_CLAIM_FILE, C.CA_FILE] + _sidecars()


def _sidecars() -> List[str]:
    """Atomic-write leftovers, matched by SHAPE, not by name.

    `_write_pem` and `_save_pending_claim` write `<path>.tmp` and only then
    `os.replace`; a crash mid-bind strands `device.key.pem.tmp` — the same
    private key under a name one character off any literal list. `*.tmp` cannot
    reach a file that is not itself a sidecar; widening it is a gate failure.
    """
    return sorted(glob.glob(os.path.join(C.VAR_DIR, "*.tmp")))


def classify_unlink(evidence: Any) -> str:
    """The gateway's event → a refusal class, or "" for anything else.

    Receipt of `controller_unlink` is authoritative REGARDLESS of the payload:
    authenticity belongs to the verified mTLS channel, never to a payload field
    (the delivery contract makes the payload optional — the gateway sends
    `{"reason":"unlinked"}` today and a pre-0.6.0 one sent `{}`). Nothing else
    — not a disconnect, not a timeout, not a 403, not a gateway outage — is a
    refusal on this door.
    """
    return C.REFUSAL_CLASS_UNLINKED if evidence == C.EVT_CONTROLLER_UNLINK else ""


def _state_for_class(cls: str) -> str:
    return C.STATE_UNLINKED


def write_pending(cls: str, reason: str) -> None:
    """Record a CONFIRMED unlink whose local wipe failed, durably.

    Same three keys as the completed marker, with `unlinked_at` deliberately
    EMPTY: the gateway's verdict is in, the erase is not done. That empty stamp
    is the whole distinction — `_reconcile_unlink_state` keys the terminal
    «отвязано» flag on a NON-empty stamp, so a pending record can never make the
    card claim an erase that did not happen, and the completed marker overwrites
    it in one write (no separate clear, no key that can be left behind).

    Why durable at all: the local «Отвязать» runs inside `sa02m_alice_api.cgi`,
    a process that answers and exits. Its in-memory pending record dies with it,
    and `/run/sa02m-alice` is `0755 root root` (etc/tmpfiles.d/sa02m-alice.conf)
    so that process cannot even write the status file. `/etc/sa02m-alice` is
    `0770 root:www-data` — this INI is the ONE channel it has to the running
    client, which is the process that can actually retry.
    """
    set_unlink_marker("", cls, reason)


def read_pending():
    """(cls, reason) of a stand-down still owed a wipe, or ("", "")."""
    stamp, cls, reason = unlink_marker()
    if stamp or not cls:
        return ("", "")
    return (cls, reason)


def yandex_source(
    write_status: Optional[Callable[..., None]] = None,
) -> binding_core.SourceSpec:
    """The Alice door, as the core sees it.

    `write_status` is the client's status writer. The CGI path passes none: it
    runs as www-data and cannot write the root-owned `/run/sa02m-alice/status.json`,
    so it reports the outcome in its own JSON response instead, and the client
    picks the durable marker up on its next pass.
    """

    def no_status(state: str, **kw: Any) -> None:
        log.info("binding reset: %s (%s) — no status writer on this path", state,
                 ", ".join("%s=%s" % kv for kv in sorted(kw.items())))

    return binding_core.SourceSpec(
        name="alice",
        # A verdict, not evidence — see the module docstring.
        threshold=1,
        wipe=lambda: binding_core.wipe_binding(binding_files),
        # No separate transport unit exists on this door.
        stop_tunnel=lambda: None,
        read_marker=unlink_marker,
        write_marker=set_unlink_marker,
        clear_marker=clear_unlink_marker,
        # The retry is run by a DIFFERENT process than the one that may fail
        # (the CGI's button vs the long-running client), so this door's pending
        # record has to survive a process exit.
        write_pending=write_pending,
        read_pending=read_pending,
        write_status=write_status or no_status,
        state_for_class=_state_for_class,
        states=(C.STATE_UNLINKED, C.STATE_UNLINK_FAILED),
        classify=classify_unlink,
    )

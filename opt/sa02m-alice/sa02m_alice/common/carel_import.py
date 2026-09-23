"""Fail-soft access to the shared Carel package (`/opt/sa02m-carel`).

The Alice catalogue and the Alice converters need the cloud fan vocabulary
(`sa02m_carel.carel_fan`) and the catalogue needs the family setpoint bounds
(`sa02m_carel.controls.SETPOINT_RANGE`), which live with the register map
they are derived from — one home, shared with the flasher daemon and the Modbus-MQTT bridge
(docs/contracts/carel-ahu.md §2). That package is on no service's PYTHONPATH,
so the path dance is the `bridge_carel._import_carel()` one; what is different
here is that a failure must NOT be fatal.

WHY FAIL-SOFT. `/opt/sa02m-carel` is installed by `04-flasher.sh`,
`05-mqtt.sh`, `06-alice.sh` and `update-www-only.sh`; a board where none of
them has run since the package existed has no copy of it. Raising out of a
catalogue build would take down the whole Alice client — every device on the
account, for one missing optional control. So the import is tried once, the
failure is logged once at WARNING (loud enough to find in the journal, never
silent), the fan-speed control is simply not attached, and everything else —
including the family-true float rows, which need no package at all — keeps
working. It self-heals on the next catalogue build after the package lands.

One home for the shim because both consumers must degrade the same way: a
second copy would drift into one of them raising.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

log = logging.getLogger(__name__)

_CAREL_DIR_ENV = "SA02M_CAREL_DIR"
_CAREL_DIR_DEFAULT = "/opt/sa02m-carel"

_resolved = False
_carel_fan: Optional[Any] = None
_controls_resolved = False
_carel_controls: Optional[Any] = None


def _candidates() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    # …/opt/sa02m-alice/sa02m_alice/common → …/opt → …/opt/sa02m-carel
    repo_opt = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return [
        os.environ.get(_CAREL_DIR_ENV) or _CAREL_DIR_DEFAULT,
        os.path.join(repo_opt, "sa02m-carel"),
    ]


def carel_fan() -> Optional[Any]:
    """`sa02m_carel.carel_fan`, or None when the package is not installed.

    Resolved once per process: a board without the package must not pay an
    import attempt (or a log line) per catalogue build.
    """
    global _resolved, _carel_fan
    if _resolved:
        return _carel_fan
    _resolved = True
    try:
        from sa02m_carel import carel_fan as module  # noqa: F401
        _carel_fan = module
        return _carel_fan
    except ImportError:
        pass
    for path in _candidates():
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        from sa02m_carel import carel_fan as module  # noqa: F811
        _carel_fan = module
    except ImportError as exc:
        log.warning(
            "sa02m_carel not importable (%s) — the Carel fan-speed control is "
            "withheld until the package is installed; every other binding is "
            "unaffected", exc,
        )
        _carel_fan = None
    return _carel_fan


def carel_controls() -> Optional[Any]:
    """`sa02m_carel.controls`, or None when the package is not installed.

    Same resolve-once, fail-soft path as `carel_fan()` — which it goes
    through first, so the sys.path dance and the one WARNING for a missing
    package stay in one place.
    """
    global _controls_resolved, _carel_controls
    if _controls_resolved:
        return _carel_controls
    _controls_resolved = True
    if carel_fan() is None:
        _carel_controls = None
        return None
    try:
        from sa02m_carel import controls as module
        _carel_controls = module
    except ImportError as exc:
        log.warning("sa02m_carel.controls not importable (%s) — the Carel "
                    "setpoint keeps its stored range", exc)
        _carel_controls = None
    return _carel_controls


def _reset_for_tests() -> None:
    """Forget the resolution. Tests only — the one-shot cache is the point."""
    global _resolved, _carel_fan, _controls_resolved, _carel_controls
    _resolved = False
    _carel_fan = None
    _controls_resolved = False
    _carel_controls = None

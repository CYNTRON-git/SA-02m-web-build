"""Optional-dependency probes. A missing dependency is reported as a SKIP with
a loud reason (printed once to stderr too) — never as a pass."""

from __future__ import annotations

import importlib
import sys


def _have(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


HAVE_PYHAP = _have("pyhap")
HAVE_SEGNO = _have("segno")

PYHAP_SKIP = ("SKIPPED, NOT PASSED: HAP-python (pyhap) is not importable on this "
              "interpreter — the glue is only proven where it is installed (the venv / CI)")
SEGNO_SKIP = ("SKIPPED, NOT PASSED: segno is not importable on this interpreter — "
              "the QR matrix is only proven where it is installed (the venv / CI)")

_announced = set()


def announce(reason: str) -> None:
    if reason not in _announced:
        _announced.add(reason)
        sys.stderr.write("\n[py-unit-homekit] %s\n" % reason)

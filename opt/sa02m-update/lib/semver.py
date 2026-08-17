# -*- coding: utf-8 -*-
"""Four-segment SA-02m web semver (M.M.P[.S]), matching status.js compareSemver."""

from __future__ import annotations

import re
from typing import Optional, Tuple

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")

VersionTuple = Tuple[int, int, int, int]


def parse(version: str) -> Optional[VersionTuple]:
    """Return (major, minor, patch, build) or None if invalid.

    Missing fourth segment is treated as 0 (same as JS compareSemver).
    """
    if version is None:
        return None
    m = VERSION_RE.match(str(version).strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))


def is_valid(version: str) -> bool:
    return parse(version) is not None


def compare(a: str, b: str) -> Optional[int]:
    """Compare two versions: -1 if a<b, 0 if equal, 1 if a>b; None if either invalid."""
    pa = parse(a)
    pb = parse(b)
    if pa is None or pb is None:
        return None
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def ge(a: str, b: str) -> bool:
    """True if a >= b (both must be valid)."""
    c = compare(a, b)
    return c is not None and c >= 0


def gt(a: str, b: str) -> bool:
    """True if a > b (both must be valid)."""
    c = compare(a, b)
    return c is not None and c > 0


def eq(a: str, b: str) -> bool:
    """True if a == b (both must be valid)."""
    c = compare(a, b)
    return c is not None and c == 0


def lt(a: str, b: str) -> bool:
    """True if a < b (both must be valid)."""
    c = compare(a, b)
    return c is not None and c < 0


def cmp(a: str, b: str) -> int:
    """Strict compare; raises ValueError if either version is invalid."""
    c = compare(a, b)
    if c is None:
        raise ValueError(f"invalid semver compare: {a!r} vs {b!r}")
    return c


def check_update_gates(
    *,
    installed: str,
    target: str,
    min_version: str,
    runner_version: str,
    min_updater: str,
) -> Optional[str]:
    """Return None if gates pass, else a short reason string (E_COMPAT detail).

    Gates (plan §2.2):
      installed >= min_version
      runner_version >= min_updater
      target > installed
    """
    for label, ver in (
        ("installed", installed),
        ("target", target),
        ("min_version", min_version),
        ("runner_version", runner_version),
        ("min_updater", min_updater),
    ):
        if parse(ver) is None:
            return f"invalid semver: {label}={ver!r}"
    if not ge(installed, min_version):
        return f"installed {installed} < min_version {min_version}"
    if not ge(runner_version, min_updater):
        return f"runner {runner_version} < min_updater {min_updater}"
    if not gt(target, installed):
        return f"target {target} must be > installed {installed}"
    return None

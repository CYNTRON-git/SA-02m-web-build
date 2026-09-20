# -*- coding: utf-8 -*-
"""One home for the cloud/Alice fan vocabulary of a Carel AHU.

The two families control the same fan through different registers — a
c.pCOmini writes a percent (HR53, clamped 20..100), a uAria writes a step
(HR197, 1..10) — and the cloud needs ONE control with ONE set of words for
both. That ladder lives here, and nowhere else: the bridge, the Alice
catalogue and the Alice converters all read it from this module.

ONE LADDER, WRITTEN ONCE, ON THE STEP SCALE. The four rung positions are the
settled vocabulary (the platform's recommended `fan_speed` set, 2026-09-20);
the percent scale is DERIVED from them through the two family maxima, so
`_STEP_RUNGS` is the only place a position appears:

    low 2 -> 20 %   medium 4 -> 40 %   high 7 -> 70 %   turbo 10 -> 100 %

Those four numbers are a PRODUCT decision, not register-map facts, and this
is deliberately not pretending otherwise — a fifths-of-the-maximum formula
would be a fiction now that the ladder is uneven. What ties them to the map
is `ladder_fits_map()`, asserted in `tests/test_carel_fan.py`: every rung must
be inside the family's clamp and the TOP rung must BE the family maximum. Move
`UARIA_FAN_STEP_MAX` or `FAN_PCT_MAX` and the fit fails loudly instead of the
vocabulary silently addressing registers the controller no longer has. The map
limits themselves stay in `carel_ahu` and are read through the module (below);
re-typing one of their VALUES here opens a second home of the register map and
`carel-shared-home` fails the build on it. (It fails on this docstring too if
the assignment is spelled out — the sweep reads Python prose, since only whole
`#` comment lines are stripped. Point at the constant, never quote its value:
the same rule `no-retired-session-token` records for its own token.)

THE LADDER IS UNEVEN ON PURPOSE (2/4/7/10), and that is a trap worth naming:
**nearest must MEASURE DISTANCE, never divide or index**. Step 6 is 2 away
from `medium` and 1 from `high`, so it is `high`; every shortcut that assumes
equal spacing answers `medium`. `tests/test_carel_fan.py` pins that case, the
now-reachable tie at 55 %, and the bottom clamp at step 1.

READ AND WRITE ARE DELIBERATELY ASYMMETRIC. Reading snaps the raw value to
the NEAREST rung (ties round up) so an off-vocabulary 75 % still shows a
name; writing is unconditional, so choosing the name a unit is already shown
at moves it to that name's exact value. The float rows keep reporting the raw
value untouched — the snapping is a presentation rule, not a measurement.
Customer-facing home of that trade: `docs/contracts/carel-ahu.md` §6.

An unrecognised family answers None everywhere rather than falling back to a
default: writing a percent to a uAria because the family word drifted is the
one failure this module exists to prevent.
"""
from __future__ import annotations

from typing import Optional, Tuple, Union

from . import carel_ahu
from .controls import FAMILY_CRST, FAMILY_UARIA

# Read through the module, never bound by value at import: a `from … import
# FAN_PCT_MAX` would make a moved limit indistinguishable from a literal
# pasted here, and the derivation proof in tests/test_carel_fan.py — the only
# check that can tell a CONSUMED constant from a coincidentally equal one —
# would pass on a hard-coded table.

# Ascending. The Yandex `fan_speed` mode values we declare. `auto` is in the
# platform's recommended set and is still NOT declared: that is a product
# choice, not a schema limit — a c.pCOmini has no auto fan register, and we do
# not declare a mode we cannot honour. `quiet` was ours until 1.0.6.50 and was
# dropped as the one value outside the recommended set.
MODES: Tuple[str, ...] = ("low", "medium", "high", "turbo")

# The rung positions, on the uAria step scale. ONE home for a position: the
# percent ladder is this scaled by the two maxima (see the module header).
_STEP_RUNGS: Tuple[int, ...] = (2, 4, 7, 10)

# The MQTT control each family drives. Names owned by `controls.CONTROLS`;
# the test asserts each one belongs to its family there and to no other.
_CONTROL_BY_FAMILY = {
    FAMILY_CRST: "fan_supply",
    FAMILY_UARIA: "fan_step",
}

Number = Union[int, float]


def _family(family: Optional[str]) -> Optional[str]:
    text = str(family or "")
    return text if text in _CONTROL_BY_FAMILY else None


def limits(family: Optional[str]) -> Optional[Tuple[Number, Number]]:
    """The (min, max) the bridge clamps this family's fan write to."""
    fam = _family(family)
    if fam == FAMILY_UARIA:
        return (carel_ahu.UARIA_FAN_STEP_MIN, carel_ahu.UARIA_FAN_STEP_MAX)
    if fam == FAMILY_CRST:
        return (carel_ahu.FAN_PCT_MIN, carel_ahu.FAN_PCT_MAX)
    return None


def rungs(family: Optional[str]) -> Tuple[Number, ...]:
    """The values of `MODES`, ascending — `()` for an unknown family.

    The uAria answers the settled step positions as they stand; the
    c.pCOmini answers them rescaled onto its percent register, so the two
    families are the SAME ladder addressed through each one's own maximum.
    """
    fam = _family(family)
    if fam is None:
        return ()
    if fam == FAMILY_UARIA:
        return _STEP_RUNGS
    factor = carel_ahu.FAN_PCT_MAX / carel_ahu.UARIA_FAN_STEP_MAX
    return tuple(step * factor for step in _STEP_RUNGS)


def ladder_fits_map(family: Optional[str]) -> bool:
    """The settled ladder still addresses this family's real register.

    Two conditions, both real — this is the ONLY thing tying four settled
    numbers to a register map that can move under them:

      * every rung is inside the clamp the bridge applies, so a declared
        mode is written unchanged. Raise `FAN_PCT_MIN` above 20 and `low`
        would be silently re-clamped, and the reported name would never
        match the one that was chosen.
      * the TOP rung IS the family maximum. Move `UARIA_FAN_STEP_MAX` and
        `turbo` stops meaning «as fast as this unit goes».

    The test asserts True for the shipped map and False for a moved one.
    """
    bounds = limits(family)
    ladder = rungs(family)
    if bounds is None or not ladder:
        return False
    lo, hi = bounds
    if not all(lo <= value <= hi for value in ladder):
        return False
    return ladder[-1] == hi


def mqtt_control(family: Optional[str]) -> Optional[str]:
    """`"fan_supply"` | `"fan_step"` — the control name, one home."""
    return _CONTROL_BY_FAMILY.get(_family(family))


def mode_from_value(family: Optional[str], raw: object) -> Optional[str]:
    """Raw register value → the nearest mode name, ties rounding UP.

    Out of range snaps to the end rung (a uAria reporting 12 is `turbo`,
    a 1 is `low`): the vocabulary is the whole answer space, and refusing
    to name an out-of-range reading would blank the control instead.
    `None` only when the value cannot be read as a number at all — the
    module-wide "omit rather than fabricate" rule.

    DISTANCE, not arithmetic on the index. The ladder is uneven (2/4/7/10),
    so `round(value / span)` and friends are wrong by a whole name at step 6.
    """
    ladder = rungs(family)
    if not ladder:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    best = 0
    for index in range(len(ladder)):
        # `<=`, not `<`: an equal distance picks the HIGHER rung, so 55 %
        # (15 from both 40 and 70) reads «high» rather than «medium».
        if abs(value - ladder[index]) <= abs(value - ladder[best]):
            best = index
    return MODES[best]


def value_from_mode(family: Optional[str], mode: object) -> Optional[Number]:
    """Mode name → the value to write. `None` on an unknown word."""
    ladder = rungs(family)
    if not ladder:
        return None
    try:
        index = MODES.index(str(mode))
    except ValueError:
        return None
    return ladder[index]


def modes_for(family: Optional[str]) -> Tuple[str, ...]:
    """`MODES` when the family is one we map, else `()`."""
    return MODES if _family(family) else ()


def payload_for_mode(family: Optional[str], mode: object) -> Optional[str]:
    """The MQTT payload for a mode — int when whole (`"80"`, `"6"`).

    Same convention as `converters.yandex_to_range`: the bridge parses with
    `float()` either way, and an integer register reads better in a journal
    line than `80.0`.
    """
    value = value_from_mode(family, mode)
    if value is None:
        return None
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return "%g" % value


# -*- coding: utf-8 -*-
"""The cloud fan vocabulary: one ladder, both families, both directions.

THE LADDER IS UNEVEN — 20/40/70/100 % on the c.pCOmini, steps 2/4/7/10 on the
uAria (the platform's recommended `fan_speed` set, settled 2026-09-20). That
is the trap these cases exist for: nearest must MEASURE DISTANCE, never divide
or index. Three cases are named for it — step 6 -> `high` (a divide-by-two
rule answers `medium`), 55 % -> `high` (a tie, now reachable from a panel, and
ties round UP), and step 1 -> `low` (the bottom clamp, which moved when the
fifth rung was dropped).

Proven RED against the five-rung tree that shipped these functions first: the
whole expectation table below FAILED (`'quiet' != 'low'`, `'medium' != 'high'`
on step 6, `'medium' != 'high'` on 55 %) before the ladder moved.

Proven RED by six mutations of `carel_fan.py`, each run on the real tree:
  * the tie comparison `<=` -> `<`
  * nearest replaced by a bucket floor
  * nearest replaced by an EVEN-SPACING rule (index = round(value / span)) —
    the mutation the old even ladder could not distinguish from nearest
  * an out-of-range guard returning None
  * the derived percent ladder replaced by its literal tuple
  * the top-rung condition dropped from `ladder_fits_map` — the one that was
    GREEN at first, and is why `test_a_raised_ceiling_…` exists: without it
    that half of the fit was decoration.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sa02m_carel import carel_ahu as ca  # noqa: E402
from sa02m_carel import carel_fan as cf  # noqa: E402
from sa02m_carel import controls as cc  # noqa: E402

CRST = cc.FAMILY_CRST
UARIA = cc.FAMILY_UARIA


class TestTheSettledLadder(unittest.TestCase):
    """The table the cloud side settled. Pinned as literals HERE — and only
    here — so the derivation in the module can never quietly drift away from
    it while still looking derived."""

    def test_the_four_names_in_order(self):
        self.assertEqual(cf.MODES, ("low", "medium", "high", "turbo"))

    def test_crst_rungs_are_twenty_forty_seventy_a_hundred_percent(self):
        self.assertEqual(cf.rungs(CRST), (20.0, 40.0, 70.0, 100.0))

    def test_uaria_rungs_are_steps_two_four_seven_ten(self):
        self.assertEqual(cf.rungs(UARIA), (2, 4, 7, 10))

    def test_the_ladder_is_uneven_and_that_is_deliberate(self):
        """Pinned as a PROPERTY, not as prose: an implementation that spaces
        the rungs evenly (or indexes them) cannot satisfy this and the
        distance cases below at the same time."""
        for ladder in (cf.rungs(CRST), cf.rungs(UARIA)):
            gaps = [b - a for a, b in zip(ladder, ladder[1:])]
            self.assertNotEqual(len(set(gaps)), 1,
                                "the ladder is expected to be uneven: %s" % (ladder,))

    def test_the_ladder_fits_the_register_map_it_is_built_from(self):
        """Every rung must be writable as-is, and the TOP rung must be the
        family maximum — that equality is what ties a settled vocabulary to
        a register map that could move under it."""
        self.assertTrue(cf.ladder_fits_map(CRST))
        self.assertTrue(cf.ladder_fits_map(UARIA))
        self.assertEqual(cf.rungs(CRST)[-1], ca.FAN_PCT_MAX)
        self.assertEqual(cf.rungs(UARIA)[-1], ca.UARIA_FAN_STEP_MAX)
        # `low` = 20 % is exactly the c.pCOmini clamp floor, which is why the
        # shared scale starts there: step 1 (=10 %) is below anything it takes.
        self.assertEqual(cf.rungs(CRST)[0], ca.FAN_PCT_MIN)
        self.assertGreater(cf.rungs(UARIA)[0], ca.UARIA_FAN_STEP_MIN)


class TestTheLadderIsDerivedNotRestated(unittest.TestCase):
    """A grep cannot tell a consumed constant from a coincidentally equal
    literal; moving the constant can. `carel-shared-home` forbids the second
    home textually, this proves the FIRST one is actually read."""

    def test_the_percent_rungs_follow_the_map_maxima(self):
        """The percent scale is the step scale through the two maxima, so
        halving the percent ceiling halves every percent rung."""
        with mock.patch.object(ca, "FAN_PCT_MAX", 50.0):
            self.assertEqual(cf.rungs(CRST), (10.0, 20.0, 35.0, 50.0))
            # …and a ladder that no longer fits the clamp says so, instead of
            # declaring a `low` the bridge would silently raise to 20 %.
            self.assertFalse(cf.ladder_fits_map(CRST))

    def test_a_map_whose_step_ceiling_moved_fails_the_fit_instead_of_lying(self):
        """The four step positions are the settled VOCABULARY, not map
        constants — so the thing that must not drift silently is their fit.
        Move the step ceiling and both families stop fitting: the uAria rungs
        leave the range, and the percent rungs stop landing on FAN_PCT_MAX."""
        with mock.patch.object(ca, "UARIA_FAN_STEP_MAX", 5):
            self.assertFalse(cf.ladder_fits_map(UARIA))
            self.assertFalse(cf.ladder_fits_map(CRST))

    def test_a_raised_ceiling_fails_the_fit_even_though_every_rung_still_fits(self):
        """The top-rung condition, on the one map change containment cannot
        see. A ceiling raised to 12 leaves all four steps inside the clamp —
        and leaves `turbo` meaning 10, which is no longer «as fast as this
        unit goes». Without this case that half of `ladder_fits_map` is
        decoration: deleting it leaves the suite green.
        """
        with mock.patch.object(ca, "UARIA_FAN_STEP_MAX", 12):
            lo, hi = cf.limits(UARIA)
            self.assertTrue(all(lo <= rung <= hi for rung in cf.rungs(UARIA)))
            self.assertFalse(cf.ladder_fits_map(UARIA))

    def test_the_rung_count_follows_the_vocabulary(self):
        self.assertEqual(len(cf.rungs(CRST)), len(cf.MODES))
        self.assertEqual(len(cf.rungs(UARIA)), len(cf.MODES))


class TestReadNearestTiesUp(unittest.TestCase):
    def test_uaria_steps(self):
        for raw, mode in ((1, "low"), (2, "low"), (3, "medium"), (4, "medium"),
                          (5, "medium"), (6, "high"), (7, "high"),
                          (8, "high"), (9, "turbo"), (10, "turbo")):
            self.assertEqual(cf.mode_from_value(UARIA, raw), mode, raw)

    def test_crst_percents(self):
        for raw, mode in ((5, "low"), (20, "low"), (30, "medium"),
                          (40, "medium"), (50, "medium"), (60, "high"),
                          (70, "high"), (73, "high"), (75, "high"),
                          (80, "high"), (100, "turbo")):
            self.assertEqual(cf.mode_from_value(CRST, raw), mode, raw)

    def test_step_six_is_high_because_nearest_measures_distance(self):
        """THE named case for the uneven ladder. Step 6 sits 2 from `medium`
        (4) and 1 from `high` (7), so it is `high`. Every shortcut that skips
        the distance — dividing by the span, indexing, bucketing by floor —
        answers `medium` here."""
        self.assertEqual(cf.mode_from_value(UARIA, 6), "high")
        self.assertEqual(cf.mode_from_value(UARIA, 5), "medium")

    def test_fifty_five_percent_is_a_reachable_tie_and_rounds_up(self):
        """15 from `medium` (40) and 15 from `high` (70). On the old even
        ladder no tie fell on a value a human would dial; on this one it
        does, so the tie rule is pinned at a value from the panel."""
        self.assertEqual(cf.mode_from_value(CRST, 55), "high")

    def test_a_tie_picks_the_higher_name(self):
        # Equidistant between two rungs: step 3 is 2|4, 30 % is 20|40,
        # 85 % is 70|100.
        self.assertEqual(cf.mode_from_value(UARIA, 3), "medium")
        self.assertEqual(cf.mode_from_value(CRST, 30), "medium")
        self.assertEqual(cf.mode_from_value(CRST, 85), "turbo")

    def test_step_one_is_low_the_bottom_clamp_that_moved(self):
        """It used to read «below `quiet`»; with the fifth rung dropped it
        reads «below `low`». Step 1 is the value a panel most plausibly
        leaves behind, so the clamp is pinned at it by name."""
        self.assertEqual(cf.mode_from_value(UARIA, 1), "low")

    def test_out_of_range_snaps_to_the_end_rung(self):
        self.assertEqual(cf.mode_from_value(UARIA, 12), "turbo")
        self.assertEqual(cf.mode_from_value(UARIA, 0), "low")
        self.assertEqual(cf.mode_from_value(CRST, 120), "turbo")
        self.assertEqual(cf.mode_from_value(CRST, 0), "low")

    def test_a_string_payload_reads_like_the_number_it_is(self):
        self.assertEqual(cf.mode_from_value(UARIA, "6"), "high")
        self.assertEqual(cf.mode_from_value(CRST, " 75.0 "), "high")

    def test_an_unreadable_value_names_nothing(self):
        for raw in ("", "   ", "n/a", None, object()):
            self.assertIsNone(cf.mode_from_value(UARIA, raw), raw)


class TestWriteIsUnconditional(unittest.TestCase):
    def test_every_mode_writes_its_own_rung(self):
        self.assertEqual([cf.value_from_mode(CRST, m) for m in cf.MODES],
                         [20.0, 40.0, 70.0, 100.0])
        self.assertEqual([cf.value_from_mode(UARIA, m) for m in cf.MODES],
                         [2, 4, 7, 10])

    def test_the_payload_is_an_integer_when_the_value_is_whole(self):
        self.assertEqual(cf.payload_for_mode(CRST, "high"), "70")
        self.assertEqual(cf.payload_for_mode(UARIA, "medium"), "4")

    def test_choosing_the_name_a_unit_already_shows_still_writes(self):
        """The accepted asymmetry: 75 % reads «high», and «high» writes 70 —
        and a uAria at step 1 reads «low», while «low» writes step 2."""
        self.assertEqual(cf.mode_from_value(CRST, 75), "high")
        self.assertEqual(cf.payload_for_mode(CRST, "high"), "70")
        self.assertEqual(cf.mode_from_value(UARIA, 1), "low")
        self.assertEqual(cf.payload_for_mode(UARIA, "low"), "2")

    def test_a_dropped_or_unknown_word_writes_nothing(self):
        # `quiet` was in our vocabulary until 1.0.6.50 and is now outside it:
        # it must be refused like any other unknown word, never guessed at.
        for mode in ("quiet", "auto", "hurricane", "", None, 3):
            self.assertIsNone(cf.value_from_mode(CRST, mode), mode)
            self.assertIsNone(cf.payload_for_mode(UARIA, mode), mode)


class TestTheControlNameHasOneHome(unittest.TestCase):
    def test_each_family_drives_its_own_control(self):
        self.assertEqual(cf.mqtt_control(CRST), "fan_supply")
        self.assertEqual(cf.mqtt_control(UARIA), "fan_step")

    def test_the_names_are_the_ones_the_inventory_gives_that_family(self):
        """Pinned against `controls.CONTROLS`, the one home of the names: a
        rename there must not leave this module publishing to a dead topic."""
        for family in (CRST, UARIA):
            other = UARIA if family == CRST else CRST
            name = cf.mqtt_control(family)
            self.assertIn(name, cc.control_names(family))
            self.assertNotIn(name, cc.control_names(other))
            self.assertIn(name, cc.writable_names(family))


class TestAnUnknownFamilyAnswersNothing(unittest.TestCase):
    """Fail-safe direction: a drifted family word withholds the control
    rather than writing a percent into a step register."""

    def test_every_entry_point_declines(self):
        for family in ("", None, "CRST", "pcomini", "uaria2"):
            self.assertEqual(cf.rungs(family), (), family)
            self.assertIsNone(cf.mqtt_control(family), family)
            self.assertIsNone(cf.mode_from_value(family, 6), family)
            self.assertIsNone(cf.value_from_mode(family, "high"), family)
            self.assertIsNone(cf.payload_for_mode(family, "high"), family)
            self.assertEqual(cf.modes_for(family), (), family)
            self.assertFalse(cf.ladder_fits_map(family), family)
            self.assertIsNone(cf.limits(family), family)


if __name__ == "__main__":
    unittest.main()

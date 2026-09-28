"""A bridge newer than the Alice package it imports (bench 1.135, 2026-09-28:
a www-only update left /opt/sa02m-alice at the previous release, and the
daemon crash-looped on `DeviceRegistry.catalogue_items`). The daemon must stop
in a standby — `missing_deps` + `peer_package_outdated`, exit 0 so
`Restart=on-failure` leaves it alone, the symbol named in `message` — never a
raw traceback and a restart loop (docs/contracts/homekit-bridge.md §10).

These tests run the PRODUCTION peer check (nothing injected): the symbol is
removed from the real DeviceRegistry class for the duration of one test."""

from __future__ import annotations

import unittest

from sa02m_alice.client.device_registry import DeviceRegistry
from sa02m_alice.client.reload_watch import RulesExposureWatcher

from sa02m_homekit import constants as C
from sa02m_homekit import main

from ._harness import Harness


class _Drop:
    """Remove a class attribute for one test; the cleanup restores it."""

    def drop(self, cls, name):
        original = cls.__dict__[name]
        delattr(cls, name)
        self.addCleanup(setattr, cls, name, original)


class PeerOutdatedStandbyTests(_Drop, unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.conf(enabled=True)

    def _run_expecting_standby(self):
        with self.assertLogs("sa02m_homekit", "ERROR") as logs:
            self.h.start()
            self.assertEqual(self.h.join(), main.EXIT_OK)
        # A standby exit, not a crash: no record carries a traceback.
        self.assertFalse([r for r in logs.records if r.exc_info],
                         "the outdated-peer standby logged a traceback")
        return self.h.status_json()

    def test_absent_registry_method_is_peer_package_outdated_not_a_crash(self):
        self.drop(DeviceRegistry, "catalogue_items")
        st = self._run_expecting_standby()
        self.assertEqual(st["state"], C.STATE_MISSING_DEPS)
        self.assertEqual(st["reason"], "peer_package_outdated")
        self.assertIn("DeviceRegistry.catalogue_items", st["message"])
        self.assertTrue(st["enabled"])
        self.assertNotIn("fatal", st["message"])
        self.assertEqual(self.h.runners, [])
        self.assertIsNone(self.h.mqtt)

    def test_a_constructor_without_the_needed_keyword_is_outdated(self):
        # Phase 3: the bridge builds RulesExposureWatcher(fingerprint=…); an
        # Alice package from before that keyword raises TypeError at start.
        original = RulesExposureWatcher.__init__

        def old_init(self, path=None, load=None):  # the pre-Phase-3 signature
            original(self, path=path, load=load)

        RulesExposureWatcher.__init__ = old_init
        self.addCleanup(setattr, RulesExposureWatcher, "__init__", original)
        st = self._run_expecting_standby()
        self.assertEqual(st["state"], C.STATE_MISSING_DEPS)
        self.assertEqual(st["reason"], "peer_package_outdated")
        self.assertIn("RulesExposureWatcher(fingerprint)", st["message"])

    def test_a_current_peer_package_starts_normally(self):
        # The production check against the repo's own Alice tree: no false
        # positive (a probe that always fires would pass the tests above).
        self.h.start()
        self.assertTrue(self.h.wait_for(lambda: self.h.state() == C.STATE_RUNNING))
        self.assertEqual(self.h.status_json()["reason"], "")
        self.assertEqual(self.h.finish(), main.EXIT_OK)


if __name__ == "__main__":
    unittest.main()

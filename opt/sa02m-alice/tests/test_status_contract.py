"""docs/contracts/alice-mqtt-mapping.md — the client status-file state enum, end to end.

The twin of the cloud agent's `tests/test_status_contract.py`, and it exists for
the class that half already paid for: a state the client WRITES that no card
renders falls through to «Нет данных», so the Operator gets a blank where the
explanation should be — and nothing in the tree noticed. `unlinked` /
`unlink_failed` (1.0.6.32) are exactly that shape of addition.

Four homes, compared AS SETS, so drift in any direction fails:

  * the contract's one `state ∈ …` line;
  * the `STATE_*` string constants in `sa02m_alice/common/constants.py`;
  * every state the client code actually writes — via `_write_status(C.STATE_…)`
    in the client sources AND the literals in the shared binding-reset core,
    which is a SECOND home of status writes since the extraction (the
    enumeration must name every home, not the first one found);
  * the labels on the two cards that render this file: `ALICE_STATE_MAP`
    (`app/alice.js`, the Yandex profile) and `CLOUD_CTRL_STATE_MAP`
    (`cloud.js`, the cloud profile). `unknown` is a card-side fallback the
    client never writes and is declared as such.

Non-vacuity: a missing contract line, an unparseable map, or a zero-length set
FAILS. A check that passes because it read nothing is the defect.
"""

from __future__ import annotations

import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REPO = os.path.abspath(os.path.join(ROOT, "..", ".."))

from sa02m_alice.common import constants as C  # noqa: E402

CONTRACT = os.path.join(REPO, "docs", "contracts", "alice-mqtt-mapping.md")
# Every home a status state can be written from. `covers` logic applied to a
# test: name the files that can BREAK the guarantee, not only where it started.
WRITER_SOURCES = (
    os.path.join(ROOT, "sa02m_alice", "client", "main.py"),
    os.path.join(ROOT, "sa02m_alice", "client", "fleet_token.py"),
    os.path.join(ROOT, "sa02m_alice", "common", "binding_sources.py"),
    os.path.join(ROOT, "sa02m_alice", "common", "binding_core.py"),
)
CARD_SOURCES = (
    (os.path.join(REPO, "www", "network_config", "static", "js", "app", "alice.js"),
     "const ALICE_STATE_MAP = {"),
    (os.path.join(REPO, "www", "network_config", "static", "js", "cloud.js"),
     "const CLOUD_CTRL_STATE_MAP = {"),
)
# Labels the cards carry that the client never writes: `unknown` is the
# render-time fallback for an older backend or an unparsed file.
CARD_ONLY = {"unknown"}


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


class StatusEnumContractTest(unittest.TestCase):
    def constants(self):
        """The enum's home in code: the STRING `STATE_*` constants.

        The isinstance filter is load-bearing, not decoration — `STATE_SNAPSHOT_S`
        is a cadence in seconds, not a state, and a name-only sweep would drag
        it in and make this test lie about what the client writes.
        """
        names = [n for n in dir(C)
                 if n.startswith("STATE_") and isinstance(getattr(C, n), str)]
        self.assertTrue(names, "no STATE_* string constants found")
        return names, {getattr(C, n) for n in names}

    def documented(self):
        self.assertTrue(os.path.isfile(CONTRACT), "contract not found: %s" % CONTRACT)
        m = re.search(r"`state ∈ ([^`]+)`", _read(CONTRACT))
        self.assertIsNotNone(m, "the contract has no `state ∈ …` enum line")
        states = {x.strip() for x in m.group(1).split("|") if x.strip()}
        self.assertTrue(states, "the contract's `state ∈ …` line is empty")
        return states

    def written(self, names):
        """States the client code really writes.

        Two shapes, because there are two homes: the client sources pass the
        constants (`_write_status(C.STATE_…)`, `FleetTokenError(…, C.STATE_…)`,
        the wait-state return), and the shared core — which cannot import the
        constants module, being stdlib-only — passes a literal.
        """
        known = set(names)
        found = set()
        for path in WRITER_SOURCES:
            self.assertTrue(os.path.isfile(path), "writer source missing: %s" % path)
            src = _read(path)
            for name in re.findall(r"C\.(STATE_[A-Z_]+)", src):
                if name in known:
                    found.add(getattr(C, name))
            found |= set(re.findall(r'write_status\("([a-z_]+)"', src))
        self.assertTrue(found, "no status write found in any writer source")
        return found

    def cards(self):
        labels = {}
        for path, anchor in CARD_SOURCES:
            self.assertTrue(os.path.isfile(path), "card source missing: %s" % path)
            js = _read(path)
            self.assertIn(anchor, js, "%s has no %s" % (path, anchor))
            block = js[js.index(anchor):]
            block = block[:block.index("};")]
            keys = set(re.findall(r"^\s*([a-z_]+):\s*\[", block, re.M))
            self.assertTrue(keys, "%s parsed to an EMPTY state map" % path)
            labels[os.path.basename(path)] = keys
        return labels

    def test_status_state_enum_matches_contract_and_cards(self):
        names, constants = self.constants()
        documented = self.documented()
        self.assertEqual(
            documented, constants,
            "contract enum != the STATE_* constants: %s"
            % sorted(documented ^ constants))

        written = self.written(names)
        self.assertEqual(
            documented, written,
            "states the client writes != the documented enum: %s"
            % sorted(documented ^ written))

        labels = self.cards()
        union = set()
        for keys in labels.values():
            union |= keys
        self.assertLessEqual(
            documented, union,
            "no card labels: %s" % sorted(documented - union))
        self.assertLessEqual(
            union - documented, CARD_ONLY,
            "the cards label states the client never writes: %s"
            % sorted(union - documented - CARD_ONLY))

    def test_the_two_stand_down_states_reach_the_alice_card(self):
        """The specific class this test was added for: `unlinked` /
        `unlink_failed` are written on the YANDEX profile, so they must be
        labelled on the card that renders that profile's status file — a label
        on the other card would not save the Operator from «Нет данных»."""
        alice = self.cards()["alice.js"]
        for state in (C.STATE_UNLINKED, C.STATE_UNLINK_FAILED):
            self.assertIn(state, alice)

    def test_the_stand_down_reason_vocabulary_points_at_its_one_home(self):
        doc = _read(CONTRACT)
        self.assertIn("cloud-agent-status.md", doc)
        for key in C.UNLINK_MARKER_KEYS:
            self.assertIn("`%s`" % key, doc, key)
        # The marker is IDENTITY: the contract must point at where it is cleared.
        self.assertIn("image-identity-reset.md", doc)


if __name__ == "__main__":
    unittest.main()

"""The package imports only the standard library and paho (no pip: OTA alone
delivers a code update), and paho only from mqttio.py — so config, media and
store (which the panel CGI may import) stay importable without it."""

from __future__ import annotations

import ast
import os
import sys
import unittest

from . import AUDIO_ROOT

PACKAGE = os.path.join(AUDIO_ROOT, "sa02m_audio")
THIRD_PARTY_ALLOWED = {"paho": {"mqttio.py"}}


def imports_of(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


class StdlibOnlyTest(unittest.TestCase):
    def test_only_stdlib_and_paho(self):
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        modules = sorted(n for n in os.listdir(PACKAGE) if n.endswith(".py"))
        self.assertGreaterEqual(len(modules), 11)
        offenders = []
        for name in modules:
            for top in imports_of(os.path.join(PACKAGE, name)):
                if top in stdlib or top == "sa02m_audio":
                    continue
                if name in THIRD_PARTY_ALLOWED.get(top, set()):
                    continue
                offenders.append("%s imports %s" % (name, top))
        self.assertEqual(offenders, [])

    def test_paho_only_in_mqttio(self):
        users = sorted(n for n in os.listdir(PACKAGE) if n.endswith(".py")
                       and "paho" in set(imports_of(os.path.join(PACKAGE, n))))
        self.assertEqual(users, ["mqttio.py"])


if __name__ == "__main__":
    unittest.main()

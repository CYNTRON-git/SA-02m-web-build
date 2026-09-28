"""The package imports only the standard library and paho (no pip — plan D9:
OTA alone can deliver it). paho is imported only by mqtt_link.py and probed by
name in main.py; the CGI dispatch (api.py) and everything it imports stay
paho-free, so a board without paho still gets an honest card."""

from __future__ import annotations

import ast
import os
import sys
import unittest

from . import HOMECONNECT_ROOT

PACKAGE = os.path.join(HOMECONNECT_ROOT, "sa02m_homeconnect")
THIRD_PARTY_ALLOWED = {"paho": {"mqtt_link.py"}}


def imports_of(path: str):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def local_imports_of(path: str):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                yield node.module.split(".")[0]
            else:
                for alias in node.names:
                    yield alias.name


class StdlibOnlyTest(unittest.TestCase):
    def test_only_stdlib_and_paho(self) -> None:
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        modules = sorted(n for n in os.listdir(PACKAGE) if n.endswith(".py"))
        self.assertGreaterEqual(len(modules), 15)
        offenders = []
        for name in modules:
            for top in imports_of(os.path.join(PACKAGE, name)):
                if top in stdlib or top == "sa02m_homeconnect":
                    continue
                if name in THIRD_PARTY_ALLOWED.get(top, set()):
                    continue
                offenders.append("%s imports %s" % (name, top))
        self.assertEqual(offenders, [])

    def test_dispatch_closure_is_paho_free(self) -> None:
        seen, todo = set(), ["api"]
        while todo:
            mod = todo.pop()
            if mod in seen:
                continue
            seen.add(mod)
            path = os.path.join(PACKAGE, mod + ".py")
            if not os.path.isfile(path):  # `from . import __version__` names an attribute
                continue
            self.assertNotIn("paho", set(imports_of(path)), mod)
            todo.extend(local_imports_of(path))
        self.assertIn("config", seen)
        self.assertNotIn("mqtt_link", seen)


if __name__ == "__main__":
    unittest.main()

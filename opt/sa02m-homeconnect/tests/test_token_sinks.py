"""Token values have two homes: oauth.py and token_store.py (P4).

The allow-list shape (quality-gate-rigor.md shape (b)): the identifiers
`access_token` / `refresh_token` / `device_code` may appear in the package
ONLY in the modules that must handle them — not a denylist of the sinks one
thought of. A new module that names a token (to log it, write it into status,
hand it to the CGI) fails here. Non-vacuous: the sweep must see every package
module, and the two sanctioned homes must really contain the identifiers.
"""

from __future__ import annotations

import os
import re
import unittest

from . import HOMECONNECT_ROOT

PACKAGE = os.path.join(HOMECONNECT_ROOT, "sa02m_homeconnect")
ALLOWED = {
    "access_token": {"oauth.py", "token_store.py"},
    "refresh_token": {"oauth.py", "token_store.py"},
    "device_code": {"oauth.py"},
}
IDENT = re.compile(r"\b(access_token|refresh_token|device_code)\b")


class TokenSinksTest(unittest.TestCase):
    def test_identifiers_only_in_their_homes(self) -> None:
        modules = sorted(n for n in os.listdir(PACKAGE) if n.endswith(".py"))
        self.assertGreaterEqual(len(modules), 15, modules)  # non-vacuous sweep
        seen = {k: set() for k in ALLOWED}
        offenders = []
        for name in modules:
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    for ident in IDENT.findall(line):
                        seen[ident].add(name)
                        if name not in ALLOWED[ident]:
                            offenders.append("%s:%d %s" % (name, lineno, ident))
        self.assertEqual(offenders, [])
        for ident, homes in ALLOWED.items():
            self.assertEqual(seen[ident], homes, ident)  # the pins match live lines


if __name__ == "__main__":
    unittest.main()

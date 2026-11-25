#!/usr/bin/env python3
# comment-mutation-proof-exempt: Behavioural stdlib unittest runner: executes immutable released GitHub manifest generation, real signed offline packing, delivered startup commands and updater locks in a private temporary root. No source needle; omitted hooks or changed delivery paths fail executable assertions. Existing tests are retained when separating privileged composition from unprivileged unit fixtures.
"""Run production-root journal delivery composition in an isolated temporary root."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def main():
    if os.name != "posix":
        print("event-journal-delivery: native Linux with root or sudo -n is required", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        try:
            status = subprocess.call(["sudo", "-n", sys.executable, "-I", str(Path(__file__).resolve())])
            return 0 if status == 0 else 1
        except OSError:
            print("event-journal-delivery: root or sudo -n is required", file=sys.stderr)
            return 1
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "opt/sa02m-devices"))
    os.environ["PYTHONPATH"] = str(root / "opt/sa02m-devices")
    assigned = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="sa02m-journal-delivery-") as folder:
        os.chmod(folder, 0o700)
        os.environ["TMPDIR"] = folder
        tempfile.tempdir = folder
        os.environ["SA02M_JOURNAL_TEST_SCRATCH"] = str(assigned if assigned != Path("/tmp") else Path(folder))
        path = root / "scripts/dev/journal_delivery_privileged_tests.py"
        spec = importlib.util.spec_from_file_location("_journal_delivery_privileged_tests", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        if suite.countTestCases() == 0:
            print("event-journal-delivery: no tests collected", file=sys.stderr)
            return 1
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())  # exit-status: main maps child/signal/prerequisite and unittest outcomes to 0 or 1.

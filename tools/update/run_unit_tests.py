#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discover and run unittest suites for offline-update / Alice packages (Tier A).

Searches (if present):
  opt/sa02m-update/tests
  opt/sa02m-alice/tests

No device required. Exit 0 when all discovered tests pass, or when no test
packages exist yet (bootstrap). Exit 1 on failures / import errors.

Examples:
  py -3 tools/update/run_unit_tests.py
  py -3 tools/update/run_unit_tests.py -v
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

TEST_ROOTS = (
    REPO_ROOT / "opt" / "sa02m-update" / "tests",
    REPO_ROOT / "opt" / "sa02m-alice" / "tests",
)


def _ensure_import_paths() -> None:
    """Allow package-local modules from opt/ trees."""
    candidates = [
        REPO_ROOT / "opt" / "sa02m-update",
        REPO_ROOT / "opt" / "sa02m-update" / "lib",
        REPO_ROOT / "opt" / "sa02m-alice",
        REPO_ROOT,
    ]
    for path in candidates:
        if path.is_dir():
            s = str(path)
            if s not in sys.path:
                sys.path.insert(0, s)


def _ensure_importable(dir_path: Path) -> None:
    """unittest.discover requires start_dir to be a package (have __init__.py)."""
    init = dir_path / "__init__.py"
    if dir_path.is_dir() and not init.is_file():
        init.write_text("# auto-created for unittest discovery\n", encoding="utf-8")


def _load_by_files(loader: unittest.TestLoader, root: Path) -> unittest.TestSuite:
    """Load each test_*.py by file path (works without a full package layout)."""
    suite = unittest.TestSuite()
    for path in sorted(root.glob("test_*.py")):
        mod_name = f"_sa02m_ut_{root.parent.name}_{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            print(f"[warn] cannot load {path}", flush=True)
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001 — report and continue other files
            print(f"[error] import {path.relative_to(REPO_ROOT)}: {exc}", flush=True)
            # Represent import failure as an error test so exit code is non-zero.
            suite.addTest(
                unittest.FunctionTestCase(
                    lambda e=exc, p=path: (_ for _ in ()).throw(
                        AssertionError(f"import failed for {p}: {e}")
                    )
                )
            )
            continue
        suite.addTests(loader.loadTestsFromModule(mod))
    return suite


def _load_tests_from_dir(loader: unittest.TestLoader, root: Path) -> unittest.TestSuite:
    # Load by file path — avoids colliding top-level package name "tests"
    # when both opt/sa02m-update/tests and opt/sa02m-alice/tests are present.
    _ensure_importable(root.parent)
    _ensure_importable(root)
    return _load_by_files(loader, root)


def discover(verbosity: int) -> unittest.TestResult:
    _ensure_import_paths()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    found_dirs: list[Path] = []

    for root in TEST_ROOTS:
        if not root.is_dir():
            print(f"[skip] no tests dir: {root.relative_to(REPO_ROOT)}", flush=True)
            continue
        found_dirs.append(root)
        discovered = _load_tests_from_dir(loader, root)
        count = discovered.countTestCases()
        print(
            f"[load] {root.relative_to(REPO_ROOT)} → {count} test case(s)",
            flush=True,
        )
        suite.addTests(discovered)

    if not found_dirs:
        print(
            "No Tier-A test packages present yet "
            "(opt/sa02m-update/tests, opt/sa02m-alice/tests). Nothing to run.",
            flush=True,
        )
        return unittest.TestResult()

    runner = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stdout)
    return runner.run(suite)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run SA-02m update/Alice unit tests (Tier A)")
    ap.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=1,
        help="Increase unittest verbosity (default 1; -vv → 2)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    verbosity = 2 if args.verbose and args.verbose >= 2 else max(1, args.verbose or 1)
    result = discover(verbosity=verbosity)
    if result.testsRun == 0 and not any(r.is_dir() for r in TEST_ROOTS):
        return 0
    print(
        f"Ran {result.testsRun} test(s): "
        f"failures={len(result.failures)} errors={len(result.errors)} "
        f"skipped={len(getattr(result, 'skipped', []) or [])}",
        flush=True,
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

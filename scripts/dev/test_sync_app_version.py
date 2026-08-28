#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# comment-mutation-proof-exempt: unit test - it calls the shipped sync-app-version functions on fixtures and asserts their return values, pinning no source line by text.
"""
test_sync_app_version.py — regression test for the ES-module cache-bust half of
scripts/sync-app-version.py (docs/decisions/es-modules.md П2).

Why this exists: index.html's `?v=` cannot bust a module the browser fetches
itself via an `import` specifier, and nginx serves /static/ with `expires 1h`.
So the sync script must patch `?v=` INSIDE import/export specifiers and
`--check` (quality row version-consistency) must FAIL on a stale one — otherwise
the first module cluster ships a "UI broken right after «Обновление веб», works
locally" defect that no other gate can see. This pins:

  - a stale specifier is FLAGGED by the check and PATCHED by the sync (static
    import ... from, export ... from, bare import '...', dynamic import(...),
    both quote styles, an `&r=` suffix preserved, multi-line import bodies);
  - a `?v=` that is NOT an import specifier (flasher.js-style runtime string,
    Array.from('...') look-alike, a comment) is left byte-identical — the patch
    is scoped to import syntax, not to every ?v= in a bundle;
  - the current shipped tree is a no-op: patching a copy of static/js changes
    nothing and flags nothing (byte-identical behaviour before modules land);
  - main(['--check']) returns 1 with a stale module under JS_DIR and 0 once
    it is patched (the end-to-end path the quality row runs).

Runs against mktemp copies only — nothing under www/ is written.

Run: python3 scripts/dev/test_sync_app_version.py   (stdlib only, no deps)
"""
from __future__ import annotations

import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sync-app-version.py"


def load_sync_module():
    # The script's file name carries hyphens, so it cannot be a plain `import`.
    spec = importlib.util.spec_from_file_location("sync_app_version", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


sync = load_sync_module()

NEW = "9.9.9.9"
OLD = "1.0.0.1"

STALE_MODULE = f"""// mqtt/index.js — fixture
import {{ scanBus }} from './scan.js?v={OLD}';
import * as bridge from "./bridge.js?v={OLD}&r=abc1";
import './side-effect.js?v={OLD}';
import {{
  a,
  b,
}} from './multi.js?v={OLD}';
export {{ helper }} from './helper.js?v={OLD}';
export * from './reexport.js?v={OLD}';
const lazy = () => import('./lazy.js?v={OLD}');
const lazy2 = () => import(
  "./lazy2.js?v={OLD}"
);
// not specifiers — must stay untouched:
const q = ver ? ('?v=' + encodeURIComponent(ver)) : '';
const runtime = '/cgi-bin/x.cgi?v={OLD}';
const arr = Array.from('?v={OLD}');
// import x from './commented.js?v={OLD}';
export function f() {{ return [scanBus, bridge, a, b, lazy, lazy2, q, runtime, arr]; }}
"""

# The comment line IS an import-shaped specifier textually; the patcher does
# not parse comments (a regex over the file), so it is rewritten too — harmless
# and documented here so a future reader is not surprised. Everything else in
# the "not specifiers" block must be byte-identical after the patch.
UNTOUCHED_LINES = (
    "const q = ver ? ('?v=' + encodeURIComponent(ver)) : '';",
    f"const runtime = '/cgi-bin/x.cgi?v={OLD}';",
    f"const arr = Array.from('?v={OLD}');",
)


class ImportSpecPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sa02m-sync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.js = self.tmp / "js"
        (self.js / "mqtt").mkdir(parents=True)
        self.mod = self.js / "mqtt" / "index.js"
        self.mod.write_text(STALE_MODULE, encoding="utf-8")
        # a classic bundle beside it — no specifiers, must never be rewritten
        self.classic = self.js / "app.js"
        self.classic.write_text(
            f"const APP_VERSION = '{OLD}';\nvar u = 'x.js?v={OLD}';\n(function(){{}})();\n",
            encoding="utf-8",
        )

    # ── flag ────────────────────────────────────────────────────────────────
    def test_stale_specifiers_are_flagged(self) -> None:
        bad = sync.js_import_spec_mismatches(NEW, js_dir=self.js)
        # 8 specifiers + the commented one = 9 hits (see UNTOUCHED_LINES note)
        self.assertEqual(len(bad), 9, bad)
        self.assertTrue(all("static/js/mqtt/index.js" in b for b in bad), bad)
        self.assertTrue(all(f"?v={OLD!r}" in b and f"expected {NEW!r}" in b for b in bad), bad)
        # the classic bundle contributes nothing (its ?v= is not an import)
        self.assertFalse(any("app.js" in b for b in bad), bad)

    def test_up_to_date_specifiers_are_not_flagged(self) -> None:
        self.assertEqual(sync.js_import_spec_mismatches(OLD, js_dir=self.js), [])

    # ── patch ───────────────────────────────────────────────────────────────
    def test_patch_rewrites_only_specifiers(self) -> None:
        before_classic = self.classic.read_bytes()
        self.assertTrue(sync.patch_js_import_specs(NEW, js_dir=self.js))
        after = self.mod.read_text(encoding="utf-8")
        for spec in (
            f"from './scan.js?v={NEW}'",
            f'from "./bridge.js?v={NEW}&r=abc1"',
            f"import './side-effect.js?v={NEW}'",
            f"}} from './multi.js?v={NEW}'",
            f"export {{ helper }} from './helper.js?v={NEW}'",
            f"export * from './reexport.js?v={NEW}'",
            f"import('./lazy.js?v={NEW}')",
            f'"./lazy2.js?v={NEW}"',
        ):
            self.assertIn(spec, after, spec)
        for line in UNTOUCHED_LINES:
            self.assertIn(line, after, f"non-specifier rewritten: {line}")
        self.assertNotIn(f"from './scan.js?v={OLD}'", after)
        # the classic bundle is byte-identical
        self.assertEqual(self.classic.read_bytes(), before_classic)
        # and the check is now clean
        self.assertEqual(sync.js_import_spec_mismatches(NEW, js_dir=self.js), [])

    def test_patch_is_idempotent(self) -> None:
        self.assertTrue(sync.patch_js_import_specs(NEW, js_dir=self.js))
        snapshot = self.mod.read_bytes()
        self.assertFalse(sync.patch_js_import_specs(NEW, js_dir=self.js))
        self.assertEqual(self.mod.read_bytes(), snapshot)

    def test_missing_dir_is_a_noop(self) -> None:
        ghost = self.tmp / "nope"
        self.assertEqual(sync.js_module_files(ghost), [])
        self.assertFalse(sync.patch_js_import_specs(NEW, js_dir=ghost))
        self.assertEqual(sync.js_import_spec_mismatches(NEW, js_dir=ghost), [])


class ShippedTreeTests(unittest.TestCase):
    """The current tree carries no import specifiers yet: the new code path must
    be a byte-identical no-op on it (the plan's before/after guarantee)."""

    def test_shipped_static_js_is_untouched(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sa02m-sync-tree-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        copy = tmp / "js"
        shutil.copytree(sync.JS_DIR, copy)
        before = {p.relative_to(copy): p.read_bytes() for p in copy.rglob("*.js")}
        self.assertGreater(len(before), 0, "sweep is dead — no *.js copied")
        version = sync.resolve_version()
        self.assertEqual(sync.js_import_spec_mismatches(version, js_dir=copy), [])
        self.assertFalse(sync.patch_js_import_specs(version, js_dir=copy))
        after = {p.relative_to(copy): p.read_bytes() for p in copy.rglob("*.js")}
        self.assertEqual(before, after)


class MainCheckTests(unittest.TestCase):
    """End to end: `--check` (what the version-consistency row runs) fails on a
    stale module under JS_DIR and passes once it is synced. The rest of the tree
    (VERSION / APP_VERSION / HTML / README) is the real one and must already be
    green — if it is not, version-consistency itself is red, truthfully."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sa02m-sync-main-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.js = self.tmp / "js"
        (self.js / "mqtt").mkdir(parents=True)
        (self.js / "mqtt" / "index.js").write_text(
            f"import {{ a }} from './scan.js?v={OLD}';\nexport const b = a;\n", encoding="utf-8"
        )
        self._orig = sync.JS_DIR
        sync.JS_DIR = self.js
        self.addCleanup(setattr, sync, "JS_DIR", self._orig)

    def _run_check(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = sync.main(["--check"])
        return rc, out.getvalue(), err.getvalue()

    def test_check_fails_on_stale_import_then_passes(self) -> None:
        version = sync.resolve_version()
        self.assertNotEqual(version, OLD, "fixture must be stale against the tree version")
        rc, _out, err = self._run_check()
        self.assertEqual(rc, 1, err)
        self.assertIn("static/js/mqtt/index.js", err)
        self.assertIn(f"?v={OLD!r}", err)
        # sync the fixture (function level, so the real tree is never written)
        self.assertTrue(sync.patch_js_import_specs(version, js_dir=self.js))
        rc, out, err = self._run_check()
        self.assertEqual(rc, 0, err)
        self.assertIn(f"OK: web version {version}", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)

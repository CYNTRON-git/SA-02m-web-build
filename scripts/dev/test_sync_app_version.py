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

And the line-ending half (LineEndingTests, 1.0.6.60): LF stays LF on EVERY
host; the regex-patched homes (app.js, index.html, login.html, modules,
README.md) keep whatever line endings they had, CRLF included, while VERSION
is rebuilt line by line and always comes back LF (write_version_file()).
Python's text-mode default (newline=None) writes os.linesep, so a sync run from a Windows checkout left VERSION, index.html, login.html and the
bundles CRLF in the working tree (backlog 2026-09-23; 1.0.6.51 converted them
back by hand) while .gitattributes `eol=lf` promises LF on disk for the device
overlay, and a local gate or a pscp-style delivery reads the working tree, not
the index. The Windows default is EMULATED (io.open wrapped so a text-mode
write with newline=None gets '\\r\\n' — what CPython's TextIOWrapper does on
Windows), so the RED is observable on any host and the emulation proves
itself live with a control write before anything is asserted.

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
from unittest import mock

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


_REAL_OPEN = io.open


def _crlf_default_open(file, mode="r", buffering=-1, encoding=None, errors=None,
                       newline=None, closefd=True, opener=None):
    """io.open as Windows CPython behaves: a text-mode WRITE with newline=None
    translates every '\\n' to os.linesep ('\\r\\n'). Reads are untouched (universal
    newlines are the same on every host); an explicit newline passes through."""
    if "b" not in mode and newline is None and any(c in mode for c in "wax+"):
        newline = "\r\n"
    return _REAL_OPEN(file, mode, buffering, encoding, errors, newline, closefd, opener)


class LineEndingTests(unittest.TestCase):
    """The syncer never changes a file's line endings — only the version
    substrings (module docstring, the line-ending half)."""

    # every home the syncer writes, LF, all at OLD
    FIXTURE = {
        "VERSION": f"# comment\n{OLD}\n",
        "app.js": f"const APP_VERSION = '{OLD}';\nvar u = 1;\n",
        "index.html": f'<script src="static/js/app.js?v={OLD}&r=abc1"></script>\n<link href="static/css/main.css?v={OLD}">\n',
        "login.html": f'<script src="static/js/login.js?v={OLD}"></script>\n',
        "js/mqtt/index.js": f"import {{ a }} from './scan.js?v={OLD}';\nexport const b = a;\n",
        "README.md": f"# SA-02m\n![v](https://img.shields.io/badge/version-{OLD}-cyan?style=flat)\n",
    }
    HOMES = ("VERSION_FILE", "APP_JS", "INDEX_HTML", "LOGIN_HTML", "README_MD")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sa02m-sync-eol-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for rel, text in self.FIXTURE.items():
            p = self.tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))   # bytes: LF regardless of host
        self.files = {rel: self.tmp / rel for rel in self.FIXTURE}
        for attr, rel in zip(self.HOMES, ("VERSION", "app.js", "index.html", "login.html", "README.md")):
            self.addCleanup(setattr, sync, attr, getattr(sync, attr))
            setattr(sync, attr, self.files[rel])
        self.addCleanup(setattr, sync, "JS_DIR", sync.JS_DIR)
        sync.JS_DIR = self.tmp / "js"

    def _sync_all(self) -> None:
        # Every writer of the sync path, at function level (the real tree is
        # never written). Each must report a change — a writer that did not
        # write would make the byte assertions below vacuous.
        sync.write_version_file(NEW)
        self.assertTrue(sync.patch_app_js(NEW))
        self.assertTrue(sync.patch_html_cache_bust(sync.INDEX_HTML, NEW))
        self.assertTrue(sync.patch_html_cache_bust(sync.LOGIN_HTML, NEW))
        self.assertTrue(sync.patch_js_import_specs(NEW, js_dir=sync.JS_DIR))
        self.assertTrue(sync.patch_readme_badge(NEW))

    def _assert_lf_and_patched(self) -> None:
        for rel, p in self.files.items():
            data = p.read_bytes()
            self.assertNotIn(b"\r", data, f"{rel} was written with CRLF: {data!r}")
            self.assertIn(NEW.encode(), data, f"{rel} was not patched to {NEW}: {data!r}")
            self.assertNotIn(OLD.encode(), data, f"{rel} still carries {OLD}: {data!r}")

    def test_lf_is_kept_under_a_crlf_default_open(self) -> None:
        """RED on the pre-1.0.6.60 syncer on ANY host: with the Windows text-mode
        default emulated, every home it rewrote came back CRLF."""
        with mock.patch("io.open", _crlf_default_open):
            control = self.tmp / "control.txt"
            control.write_text("a\nb\n", encoding="utf-8")
            self.assertEqual(control.read_bytes(), b"a\r\nb\r\n",
                             "the CRLF-default emulation is not live — the test below would prove nothing")
            self._sync_all()
        self._assert_lf_and_patched()

    def test_lf_is_kept_on_this_host(self) -> None:
        """The real io on the host running the suite: RED on Windows against the
        pre-1.0.6.60 syncer (observed 2026-09-28, CPython 3.11 and 3.14 — every
        home CRLF); on Linux it cannot distinguish old from new (os.linesep is
        LF) — the emulated case above is the host-independent proof."""
        self._sync_all()
        self._assert_lf_and_patched()

    def test_crlf_input_stays_crlf(self) -> None:
        """A CRLF regex-patched home (app.js, index.html) is patched
        byte-faithfully: reading with newline='' and writing with newline='\\n'
        means neither direction translates, so it keeps its CRLF. VERSION is the
        one exception, by design: write_version_file() rebuilds it from
        splitlines() and joins with '\\n', so a CRLF VERSION comes back LF —
        pinned below so the scoped claim is measured, not asserted."""
        crlf_js = self.tmp / "app.js"
        crlf_js.write_bytes(f"const APP_VERSION = '{OLD}';\r\nvar u = 1;\r\n".encode())
        crlf_html = self.tmp / "index.html"
        crlf_html.write_bytes(f'<script src="a.js?v={OLD}"></script>\r\n'.encode())
        self.assertTrue(sync.patch_app_js(NEW))
        self.assertTrue(sync.patch_html_cache_bust(crlf_html, NEW))
        self.assertEqual(crlf_js.read_bytes(), f"const APP_VERSION = '{NEW}';\r\nvar u = 1;\r\n".encode())
        self.assertEqual(crlf_html.read_bytes(), f'<script src="a.js?v={NEW}"></script>\r\n'.encode())
        crlf_version = self.tmp / "VERSION"
        crlf_version.write_bytes(f"# comment\r\n{OLD}\r\n".encode())
        sync.write_version_file(NEW)
        self.assertEqual(crlf_version.read_bytes(), f"# comment\n{NEW}\n".encode())


if __name__ == "__main__":
    unittest.main(verbosity=1)

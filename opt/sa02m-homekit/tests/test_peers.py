"""peers.py — the one list of sibling-package symbols the bridge uses, its
probe, and the scripts/06c-homekit.sh block that runs it before the bridge
lands (docs/contracts/homekit-bridge.md §1, §10).

* ProbeTests — outdated vs broken vs present on fake packages; an empty or
  malformed list raises (a probe that checks nothing proves nothing).
* CompletenessTests — the list is DERIVED from the package's own imports and
  attribute uses (AST), so a new peer symbol the list forgot goes RED here
  (quality-gate-rigor.md shape (b): every home, not the first one found).
* SixCBlockTests — the `# >>> peer-probe` block of 06c, extracted and run in
  bash against copies of the real trees: current ⇒ continue, stale ⇒ refresh
  through the peer's OWN module or stop with the Russian fix line, an empty
  list ⇒ stop.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from sa02m_homekit import peers

from . import ALICE_ROOT, HOMEKIT_ROOT, REPO_ROOT

RULES_ROOT = os.path.join(REPO_ROOT, "opt", "sa02m-rules")
SIX_C = os.path.join(REPO_ROOT, "scripts", "06c-homekit.sh")
PKG_DIR = os.path.join(HOMEKIT_ROOT, "sa02m_homekit")

_FAKE = {
    "hkpeer_zz/__init__.py": "",
    "hkpeer_zz/mod.py": textwrap.dedent("""\
        CONST = 1

        class Reg:
            def __init__(self, doc, profile="yandex"):
                self.doc = doc

            def items(self):
                return []

        class Loose:
            def __init__(self, *args, **kwargs):
                pass

        def func():
            return None
        """),
    "hkpeer_zz/needs_other.py": "import hk_not_a_peer_zz9  # noqa: F401\n",
    "hkpeer_zz/raises.py": "raise RuntimeError('wheel built for another ABI')\n",
}


class _FakePeers:
    def _install(self):
        self.fake_dir = tempfile.mkdtemp()
        for rel, body in _FAKE.items():
            path = os.path.join(self.fake_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        sys.path.insert(0, self.fake_dir)
        self.addCleanup(self._remove)

    def _remove(self):
        sys.path.remove(self.fake_dir)
        for name in list(sys.modules):
            if name.startswith("hkpeer_zz") or name.startswith("hk_not_a_peer"):
                del sys.modules[name]
        shutil.rmtree(self.fake_dir, ignore_errors=True)


class ProbeTests(_FakePeers, unittest.TestCase):
    def setUp(self):
        self._install()

    def test_present_symbols_are_neither_outdated_nor_broken(self):
        self.assertEqual(peers.probe((
            "hkpeer_zz.mod:CONST", "hkpeer_zz.mod:func", "hkpeer_zz.mod:Reg.items",
            "hkpeer_zz.mod:Reg(profile)", "hkpeer_zz.mod:Loose(anything)",
        )), ([], []))

    def test_each_kind_of_absence_is_outdated(self):
        specs = (
            "hkpeer_zz.mod:GONE",                 # a module attribute
            "hkpeer_zz.mod:Reg.catalogue_items",  # a method (the bench defect)
            "hkpeer_zz.mod:Reg(fingerprint)",     # a keyword the call site passes
            "hkpeer_zz.nope:anything",            # a submodule
        )
        self.assertEqual(peers.probe(specs), (list(specs), []))

    def test_an_absent_package_or_a_foreign_failure_is_broken_not_outdated(self):
        outdated, broken = peers.probe((
            "hk_absent_pkg_zz9.mod:x",       # the peer is not installed at all
            "hkpeer_zz.needs_other:x",       # a peer module fails on a non-peer
            "hkpeer_zz.raises:x",            # a peer module raises at import
        ))
        self.assertEqual(outdated, [])
        self.assertEqual(len(broken), 3)
        self.assertIn("hk_not_a_peer_zz9", broken[1])
        self.assertIn("RuntimeError", broken[2])

    def test_an_empty_or_malformed_list_raises(self):
        with self.assertRaises(ValueError):
            peers.probe(())
        for bad in ("no_colon", "mod:", ":attr", "mod:a..b", "mod:a(k", "mod:a(1x)"):
            with self.assertRaises(ValueError, msg=bad):
                peers.probe((bad,))

    def test_cli_exit_codes(self):
        cases = [
            (dict(required=("hkpeer_zz.mod:CONST",), optional=()), peers.RC_OK, []),
            (dict(required=("hkpeer_zz.mod:Reg.catalogue_items",), optional=()),
             peers.RC_OUTDATED, ["outdated hkpeer_zz.mod:Reg.catalogue_items"]),
            (dict(required=("hkpeer_zz.raises:x",), optional=()), peers.RC_BROKEN, ["broken hkpeer_zz.raises:x"]),
            (dict(required=("hkpeer_zz.mod:CONST",), optional=("hkpeer_zz.mod:GONE",)),
             peers.RC_OPTIONAL_OUTDATED, ["outdated-optional hkpeer_zz.mod:GONE"]),
            # an optional package that is not installed is «no scenes», not a finding
            (dict(required=("hkpeer_zz.mod:CONST",), optional=("hk_absent_pkg_zz9.store:load",)),
             peers.RC_OK, []),
            (dict(required=(), optional=()), peers.RC_BAD_LIST, ["FAIL empty peer symbol list"]),
            (dict(required=("hkpeer_zz.mod:CONST",), optional=("bad spec",)), peers.RC_BAD_LIST, ["FAIL"]),
        ]
        for kw, rc, lines in cases:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(peers.cli([], **kw), rc, kw)
            for line in lines:
                self.assertIn(line, out.getvalue(), kw)


class RealTreeTests(unittest.TestCase):
    """The repo's own trees carry every listed symbol — else the daemon would
    refuse to start on a board that is exactly current."""

    def test_the_repo_alice_tree_satisfies_the_required_list(self):
        self.assertEqual(peers.probe(peers.REQUIRED_PEER_SYMBOLS), ([], []))

    def test_the_repo_rules_tree_satisfies_the_optional_list(self):
        sys.path.insert(0, RULES_ROOT)
        self.addCleanup(sys.path.remove, RULES_ROOT)
        self.assertEqual(peers.probe(peers.OPTIONAL_PEER_SYMBOLS), ([], []))

    def test_the_lists_are_sorted_unique_and_well_formed(self):
        for lst in (peers.REQUIRED_PEER_SYMBOLS, peers.OPTIONAL_PEER_SYMBOLS):
            self.assertTrue(lst)
            self.assertEqual(list(lst), sorted(set(lst)))
            for spec in lst:
                peers.parse_spec(spec)


# Instance attributes that hold a peer object (their method calls are peer
# symbols too): the engine's registry, the daemon's two watchers.
_INSTANCE_OF = {
    "registry": "sa02m_alice.client.device_registry:DeviceRegistry",
    "_watcher": "sa02m_alice.client.reload_watch:DevicesWatcher",
    "_rules_watcher": "sa02m_alice.client.reload_watch:RulesExposureWatcher",
}
# Injectable factories the daemon calls in place of a peer class
# (main.Daemon: the production default IS that class).
_FACTORY_OF = {
    "_registry_factory": "sa02m_alice.client.device_registry:DeviceRegistry",
    "_watcher_factory": "sa02m_alice.client.reload_watch:DevicesWatcher",
}
_PEER_ROOTS = ("sa02m_alice", "sa02m_rules")


def _is_module(dotted: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError):
        return False


def derive_peer_uses() -> set:
    """Every peer symbol the package's code uses, as spec bases plus the
    keyword forms of peer-class constructor calls."""
    uses = set()
    for name in sorted(os.listdir(PKG_DIR)):
        if not name.endswith(".py") or name == "peers.py":
            continue
        with open(os.path.join(PKG_DIR, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        bound = {}  # local name → ("module", dotted) | ("object", spec base)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                    and node.module.split(".")[0] in _PEER_ROOTS:
                for alias in node.names:
                    local = alias.asname or alias.name
                    full = "%s.%s" % (node.module, alias.name)
                    if _is_module(full):
                        bound[local] = ("module", full)
                    else:
                        base = "%s:%s" % (node.module, alias.name)
                        bound[local] = ("object", base)
                        uses.add(base)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _PEER_ROOTS:
                        raise AssertionError("%s: plain `import %s` — use `from … import`"
                                             % (name, alias.name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                owner = node.value
                if isinstance(owner, ast.Name) and owner.id in bound:
                    kind, what = bound[owner.id]
                    uses.add("%s:%s" % (what, node.attr) if kind == "module"
                             else "%s.%s" % (what, node.attr))
                elif isinstance(owner, ast.Attribute) and owner.attr in _INSTANCE_OF:
                    uses.add("%s.%s" % (_INSTANCE_OF[owner.attr], node.attr))
            elif isinstance(node, ast.Call) and node.keywords:
                func = node.func
                base = None
                if isinstance(func, ast.Name) and bound.get(func.id, ("", ""))[0] == "object":
                    base = bound[func.id][1]
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                        and bound.get(func.value.id, ("", ""))[0] == "module":
                    base = "%s:%s" % (bound[func.value.id][1], func.attr)
                elif isinstance(func, ast.Attribute) and func.attr in _FACTORY_OF:
                    base = _FACTORY_OF[func.attr]
                if base:
                    kws = sorted(k.arg for k in node.keywords if k.arg)
                    if kws:
                        uses.add("%s(%s)" % (base, ", ".join(kws)))
    return uses


def _covered(use: str, listed: set) -> bool:
    if use in listed:
        return True
    base, _, kw = use.partition("(")
    if not kw:
        # A bare constructor use is covered by its keyword form.
        return any(s.split("(")[0] == base for s in listed)
    want = set(k.strip() for k in kw.rstrip(")").split(","))
    return any(s.split("(")[0] == base and want <= set(peers.parse_spec(s)[2]) for s in listed)


class CompletenessTests(unittest.TestCase):
    def test_every_peer_symbol_the_package_uses_is_listed(self):
        uses = derive_peer_uses()
        # Non-vacuity: the derivation must see the bridge's real surface.
        self.assertGreaterEqual(len(uses), 15, sorted(uses))
        self.assertIn("sa02m_alice.client.device_registry:DeviceRegistry.catalogue_items", uses)
        self.assertIn("sa02m_alice.client.reload_watch:RulesExposureWatcher(fingerprint)", uses)
        self.assertIn("sa02m_alice.client.device_registry:DeviceRegistry(profile)", uses)
        listed = set(peers.REQUIRED_PEER_SYMBOLS)
        missing = sorted(u for u in uses if not _covered(u, listed))
        self.assertEqual(missing, [], "peer symbols used but not in REQUIRED_PEER_SYMBOLS")

    def test_the_rules_list_is_what_scene_devices_reads_from_the_store(self):
        # The bridge never imports sa02m_rules itself; scene_devices does,
        # through `store.<name>` on the module rules_store() returns.
        with open(os.path.join(ALICE_ROOT, "sa02m_alice", "config", "scene_devices.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        used = set(re.findall(r"\bstore\.([A-Za-z_]\w*)", src))
        used |= set(re.findall(r'getattr\(store,\s*"([A-Za-z_]\w*)"', src))
        self.assertTrue(used, "scene_devices.py: no store.<name> use found")
        self.assertEqual({s.split(":")[1] for s in peers.OPTIONAL_PEER_SYMBOLS}, used)


def _extract_block() -> str:
    with open(SIX_C, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"^# >>> peer-probe.*?^# <<< peer-probe$", src, re.S | re.M)
    if not m:
        raise AssertionError("scripts/06c-homekit.sh has no `# >>> peer-probe` … `# <<< peer-probe` block")
    return m.group(0)


def _ignore(_d, names):
    return [n for n in names if n in ("__pycache__", "tests")]


@unittest.skipUnless(shutil.which("bash") and shutil.which("timeout"),
                     "SKIPPED, NOT PASSED: bash/timeout not on PATH")
class SixCBlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hk_src = HOMEKIT_ROOT
        self.alice = os.path.join(self.tmp, "opt-alice")
        self.rules = os.path.join(self.tmp, "opt-rules")
        shutil.copytree(ALICE_ROOT, self.alice, ignore=_ignore)
        shutil.copytree(RULES_ROOT, self.rules, ignore=_ignore)
        self.base = os.path.join(self.tmp, "tree")      # the repo tree 06c runs from
        self.scripts = os.path.join(self.base, "scripts")
        os.makedirs(self.scripts)
        venv_bin = os.path.join(self.tmp, "venv", "bin")
        os.makedirs(venv_bin)
        os.symlink(sys.executable, os.path.join(venv_bin, "python"))
        self.env_extra = {}

    # fixtures
    def _stale(self, rel, old, new):
        path = os.path.join(rel, *old[0].split("/"))
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(old[1], src)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace(old[1], new))

    def _stale_alice(self):
        self._stale(self.alice, ("sa02m_alice/client/device_registry.py", "def catalogue_items("),
                    "def catalogue_items_not_yet(")

    def _stale_rules(self):
        self._stale(self.rules, ("sa02m_rules/store.py", "\ndef load("), "\ndef load_not_yet(")

    def _peer_sources(self, pkg, src_root, module, marker):
        """The tree carries `pkg` sources and its installer module; the fake
        module installs them (what the real one's rsync does) and leaves a mark."""
        shutil.copytree(src_root, os.path.join(self.base, "opt", pkg), ignore=_ignore)
        dest = self.alice if pkg == "sa02m-alice" else self.rules
        with open(os.path.join(self.scripts, module), "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\nset -e\nrm -rf '%s'\ncp -r '%s' '%s'\ntouch '%s'\n"
                     % (dest, os.path.join(self.base, "opt", pkg), dest, marker))

    def _run(self, hk_src=None):
        script = "\n".join([
            "set -euo pipefail",
            'log() { printf "[%s] %s\\n" "$1" "$2"; }',
            "OPT_SRC=%s" % shlex_quote(hk_src or self.hk_src),
            "VENV_DIR=%s" % shlex_quote(os.path.join(self.tmp, "venv")),
            "SCRIPT_DIR=%s" % shlex_quote(self.scripts),
            "BASE_DIR=%s" % shlex_quote(self.base),
            "HK_ALICE_DIR=%s" % shlex_quote(self.alice),
            "HK_RULES_DIR=%s" % shlex_quote(self.rules),
            _extract_block(),
            "echo BLOCK-CONTINUED",
        ])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"}
        env.update(self.env_extra)
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=env, timeout=120)
        return proc.returncode, proc.stdout + proc.stderr

    # cases
    def test_current_peers_continue(self):
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("совместимы с мостом", out)
        self.assertIn("BLOCK-CONTINUED", out)

    def test_stale_alice_without_sources_stops_with_the_russian_fix_line(self):
        self._stale_alice()
        rc, out = self._run()
        self.assertEqual(rc, 1, out)
        self.assertIn("DeviceRegistry.catalogue_items", out)
        self.assertIn("обновите пакет Алисы: запустите 06-alice.sh или полную установку", out)
        self.assertNotIn("BLOCK-CONTINUED", out)

    def test_stale_alice_with_sources_is_refreshed_through_its_own_module(self):
        self._stale_alice()
        mark = os.path.join(self.tmp, "alice-module-ran")
        self._peer_sources("sa02m-alice", ALICE_ROOT, "06-alice.sh", mark)
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(mark), out)
        self.assertIn("обновляю пакет Алисы из этого дерева (06-alice.sh)", out)
        self.assertIn("совместимы с мостом", out)

    def test_an_operator_skip_of_the_alice_module_is_honoured(self):
        self._stale_alice()
        mark = os.path.join(self.tmp, "alice-module-ran")
        self._peer_sources("sa02m-alice", ALICE_ROOT, "06-alice.sh", mark)
        self.env_extra["SA02M_SKIP_ALICE"] = "1"
        rc, out = self._run()
        self.assertEqual(rc, 1, out)
        self.assertFalse(os.path.exists(mark), out)

    def test_stale_rules_only_warns_and_continues(self):
        self._stale_rules()
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("outdated-optional sa02m_rules.store:load", out)
        self.assertIn("сцены в HomeKit недоступны", out)
        self.assertIn("BLOCK-CONTINUED", out)

    def test_stale_rules_with_sources_is_refreshed_through_its_own_module(self):
        self._stale_rules()
        mark = os.path.join(self.tmp, "rules-module-ran")
        self._peer_sources("sa02m-rules", RULES_ROOT, "06b-rules.sh", mark)
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(mark), out)
        self.assertIn("совместимы с мостом", out)

    def test_an_empty_symbol_list_stops_the_install(self):
        hk = os.path.join(self.tmp, "hk-src")
        shutil.copytree(HOMEKIT_ROOT, hk, ignore=_ignore)
        path = os.path.join(hk, "sa02m_homekit", "peers.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        emptied, n = re.subn(r"(REQUIRED_PEER_SYMBOLS: Tuple\[str, \.\.\.\] = )\(\n.*?\n\)\n",
                             r"\1()\n", src, count=1, flags=re.S)
        self.assertEqual(n, 1, "could not empty REQUIRED_PEER_SYMBOLS in the copy")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(emptied)
        rc, out = self._run(hk_src=hk)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL empty peer symbol list", out)
        self.assertIn("список символов моста пуст", out)
        self.assertNotIn("BLOCK-CONTINUED", out)


class SixCOrderTests(unittest.TestCase):
    """The probe runs before anything of the new bridge lands on the board."""

    def test_the_probe_precedes_the_venv_the_package_and_the_unit(self):
        with open(SIX_C, encoding="utf-8") as fh:
            lines = [ln if not ln.lstrip().startswith("#") else "" for ln in fh.read().splitlines()]

        def first(pattern):
            for i, ln in enumerate(lines):
                if re.search(pattern, ln):
                    return i
            self.fail("06c: no live line matches %r" % pattern)

        probe = first(r"^hk_peer_probe \|\| hk_peer_rc=")
        for later in (r"sa02m_venv_install_locked ", r"^rsync -a --delete", r'sa02m_svc_apply "\$UNIT"'):
            self.assertLess(probe, first(later), later)


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


if __name__ == "__main__":
    unittest.main()
